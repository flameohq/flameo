"""Small coverage-tail tests (tests only, no ``flameo/`` source changes).

Covers:
* ``flameo/templates.py`` -- ``get_last_template_filters`` plus a proof
  that app-wide filters apply to ``template_to_string()`` and per-call
  filters do not leak into the global filter store.
* ``flameo/cleanup.py`` -- ``TempSweeper.start`` early return when the
  temp dir cannot be created.
* ``flameo/logger.py`` -- rollover skip over several full files, writer
  shutdown/stop edges, ``_enqueue`` without caller frames, and the
  remaining ``_bridge_main`` batch-window branches.
* ``flameo/cycles.py`` -- ``_resolve_safe_path`` empty-frame skip,
  ``_response_header`` plain-value coercion, ``_is_fresh`` naive/missing
  datetimes.
"""

import datetime as dt
import logging
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

import flameo.cleanup as cleanup
import flameo.logger  # noqa: F401  (ensures sys.modules entry exists)
import flameo.cycles as cycles
from flameo import templates
from flameo.logger import FlameoLogger, _writer_process_main

logger_module = sys.modules["flameo.logger"]

_DAY = dt.date(2024, 5, 6)
_DAY_TS = dt.datetime(2024, 5, 6, 12, 0, 0).timestamp()


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

def _shout(value):
    return str(value).upper()


_shout.__name__ = "shout"


def _exclaim(value):
    return str(value) + "!"


_exclaim.__name__ = "exclaim"


@pytest.fixture
def isolated_templates():
    saved = (
        templates._last_template_directory,
        dict(templates._last_template_filter_map),
        dict(templates._STRING_ENV_CACHE),
        dict(templates._CALL_ENV_CACHE),
    )
    templates._last_template_directory = None
    templates._last_template_filter_map.clear()
    templates._STRING_ENV_CACHE.clear()
    templates._CALL_ENV_CACHE.clear()
    try:
        yield templates
    finally:
        (templates._last_template_directory,
         filter_map,
         string_cache,
         call_cache) = saved
        templates._last_template_filter_map.clear()
        templates._last_template_filter_map.update(filter_map)
        templates._STRING_ENV_CACHE.clear()
        templates._STRING_ENV_CACHE.update(string_cache)
        templates._CALL_ENV_CACHE.clear()
        templates._CALL_ENV_CACHE.update(call_cache)


def test_get_last_template_filters_returns_app_wide_filters(tmp_path, isolated_templates):
    assert templates.get_last_template_filters() == {}

    templates.build_environment(str(tmp_path), filters=[_shout])

    assert templates.get_last_template_directory() == str(tmp_path)
    assert templates.get_last_template_filters() == {"shout": _shout}


def test_template_to_string_applies_app_filters_without_leaking_per_call_ones(
    app_factory, tmp_path, isolated_templates
):
    from flameo import template_to_string

    directory = tmp_path / "templates"
    directory.mkdir()
    (directory / "mail.html").write_text("Hello {{ name | shout }}", encoding="utf-8")
    (directory / "loud.html").write_text("Hi {{ name | exclaim }}", encoding="utf-8")

    app_factory().use_template(str(directory), filters=[_shout])

    assert template_to_string("mail.html", {"name": "Ada"}) == "Hello ADA"

    assert (
        template_to_string("loud.html", {"name": "Ada"}, filters=[_exclaim])
        == "Hi Ada!"
    )

    assert templates.get_last_template_filters() == {"shout": _shout}
    assert template_to_string("mail.html", {"name": "Ada"}) == "Hello ADA"

    with pytest.raises(Exception, match="No filter named 'exclaim'"):
        template_to_string("loud.html", {"name": "Ada"})


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

class _UncreatableDir:
    def mkdir(self, *args, **kwargs):
        raise OSError("read-only filesystem")


async def test_start_returns_early_when_temp_dir_cannot_be_created(
    monkeypatch, caplog
):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: _UncreatableDir())
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)

    with caplog.at_level(logging.WARNING, logger="flameo.cleanup"):
        await sweeper.start()

    assert "cannot create temp dir" in caplog.text
    assert sweeper._started is False
    assert sweeper._task is None


# --------------------------------------------------------------------------
# cycles (low-hanging lines)
# --------------------------------------------------------------------------

def test_resolve_safe_path_skips_frames_without_a_filename(monkeypatch, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "file.txt"
    target.write_text("hi", encoding="utf-8")
    caller = tmp_path / "handler.py"
    caller.write_text("x", encoding="utf-8")

    def fake_stack():
        return [
            SimpleNamespace(filename=__file__),
            SimpleNamespace(filename=""),
            SimpleNamespace(filename=str(caller)),
        ]

    monkeypatch.setattr(cycles.inspect, "stack", fake_stack)

    resolved, caller_dir, err = cycles._resolve_safe_path("sub/file.txt")

    assert err is None
    assert resolved == str(target)
    assert caller_dir == str(tmp_path)


def test_response_header_coerces_plain_values_to_str():
    res = SimpleNamespace(_header_map={b"etag": 123})
    assert cycles._response_header(res, "ETag") == "123"


def test_is_fresh_applies_utc_to_naive_datetimes():
    res = SimpleNamespace(
        _header_map=None,
        _hdr=[("last-modified", "06 Nov 1994 08:49:37")],
    )
    assert (
        cycles._is_fresh({"if-modified-since": "06 Nov 1994 08:49:37"}, res)
        is True
    )
    assert (
        cycles._is_fresh({"if-modified-since": "07 Nov 1994 08:49:37"}, res)
        is True
    )
    assert (
        cycles._is_fresh({"if-modified-since": "05 Nov 1994 08:49:37"}, res)
        is False
    )


def test_is_fresh_rejects_unparseable_conditional_dates(monkeypatch):
    monkeypatch.setattr(
        cycles.email.utils, "parsedate_to_datetime", lambda value: None
    )
    res = SimpleNamespace(
        _header_map=None,
        _hdr=[("last-modified", "06 Nov 1994 08:49:37")],
    )
    assert (
        cycles._is_fresh({"if-modified-since": "06 Nov 1994 08:49:37"}, res)
        is False
    )


# --------------------------------------------------------------------------
# logger tails (local fakes mirroring tests/unit/test_logger.py)
# --------------------------------------------------------------------------

class _FakeQueue:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()
        self.closed = False
        self.cancelled = False
        self.fail_put = False

    def put_nowait(self, item):
        if self.fail_put:
            raise OSError("queue is closed")
        with self.lock:
            self.items.append(item)

    def get(self, timeout=None):
        with self.lock:
            if self.items:
                return self.items.pop(0)
        raise queue.Empty()

    def get_nowait(self):
        with self.lock:
            if self.items:
                return self.items.pop(0)
        raise queue.Empty()

    def close(self):
        self.closed = True

    def cancel_join_thread(self):
        self.cancelled = True

    def snapshot(self):
        with self.lock:
            return list(self.items)


class _FakeProcess:
    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.name = name
        self.daemon = daemon
        self.started = False
        self.alive = False
        self.terminated = False
        self.joined = []
        self._popen = None

    def start(self):
        self.started = True
        self.alive = True
        self._popen = object()

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.joined.append(timeout)

    def terminate(self):
        self.terminated = True
        self.alive = False


class _FailingTerminateProcess(_FakeProcess):
    def terminate(self):
        raise OSError("cannot terminate")


class _AlwaysAliveProcess(_FakeProcess):
    def is_alive(self):
        return True


class _FakeThread:
    def __init__(self, target=None, name=None, daemon=None, **kwargs):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.joined = []

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.joined.append(timeout)

    def is_alive(self):
        return False


class _FakeContext:
    def __init__(self, processes):
        self.processes = processes

    def Process(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        process = _FakeProcess(target, args, kwargs, name, daemon)
        self.processes.append(process)
        return process


class _MpShim:
    def __init__(self):
        self.queues = []
        self.processes = []
        self._context = _FakeContext(self.processes)

    def Queue(self):
        fake = _FakeQueue()
        self.queues.append(fake)
        return fake

    def get_context(self):
        return self._context


class _ThreadingShim:
    def __init__(self):
        self.Lock = threading.Lock
        self.Event = threading.Event
        self.Thread = _FakeThread


class _ScriptedQueue:
    def __init__(self, items=(), on_get=None):
        self.items = list(items)
        self.on_get = on_get
        self.get_calls = 0

    def get(self, timeout=None):
        self.get_calls += 1
        if self.on_get is not None:
            self.on_get(self, self.get_calls)
        if self.items:
            return self.items.pop(0)
        raise queue.Empty()

    def get_nowait(self):
        if self.items:
            return self.items.pop(0)
        raise queue.Empty()

    def put_nowait(self, item):
        self.items.append(item)


@pytest.fixture
def tail_logger_env(monkeypatch):
    mp_shim = _MpShim()
    monkeypatch.setattr(logger_module, "mp", mp_shim)
    monkeypatch.setattr(logger_module, "threading", _ThreadingShim())
    return mp_shim


def _make_tail_logger(tmp_path, **kwargs):
    kwargs.setdefault("log_dir", str(tmp_path / "logs"))
    kwargs.setdefault("batch_size", 5)
    kwargs.setdefault("batch_timeout", 0.05)
    return FlameoLogger(**kwargs)


def _log_payload(msg="hello"):
    return {
        "v": 1,
        "level": logging.INFO,
        "msg": msg,
        "args": (),
        "kwargs": {},
        "created": _DAY_TS,
        "fn": None,
        "lineno": 0,
        "func_name": None,
    }


def test_writer_skips_a_numbered_file_that_fills_mid_run(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    prefix = f"flameo-{_DAY.isoformat()}"
    (log_dir / f"{prefix}.log").write_text("x" * 500, encoding="utf-8")
    (log_dir / f"{prefix}-1.log").write_text("y" * 500, encoding="utf-8")

    class _PlantingQueue:
        def __init__(self):
            self.calls = 0

        def get(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return _log_payload("first " + "a" * 300)
            if self.calls == 2:
                (log_dir / f"{prefix}-3.log").write_text("z" * 500, encoding="utf-8")
                return _log_payload("second " + "b" * 300)
            return None

    _writer_process_main(str(log_dir), _PlantingQueue(), max_bytes=100)

    assert (log_dir / f"{prefix}-3.log").read_text(encoding="utf-8") == "z" * 500
    assert "second" in (log_dir / f"{prefix}-4.log").read_text(encoding="utf-8")


def test_stop_skips_terminate_when_writer_exited_cleanly(tail_logger_env, tmp_path):
    logger = _make_tail_logger(tmp_path)
    process = _FakeProcess()
    process.start()
    process.alive = False
    logger._writer_proc = process

    logger.stop()

    assert process.joined == [5]
    assert process.terminated is False


def test_stop_tolerates_terminate_failure(tail_logger_env, tmp_path):
    logger = _make_tail_logger(tmp_path)
    process = _FailingTerminateProcess()
    process.start()
    logger._writer_proc = process

    logger.stop()

    assert process.joined == [5, 2]


def test_shutdown_writer_skips_drain_when_process_already_dead(
    tail_logger_env, tmp_path
):
    logger = _make_tail_logger(tmp_path)
    process = _FakeProcess()
    process.start()
    process.alive = False
    finished = _FakeQueue()

    logger._shutdown_writer(process, finished)

    assert finished.snapshot() == []
    assert finished.cancelled
    assert finished.closed


def test_shutdown_writer_tolerates_sentinel_and_terminate_failures(
    tail_logger_env, tmp_path
):
    logger = _make_tail_logger(tmp_path)

    live = _AlwaysAliveProcess()
    live.start()
    live._popen = object()
    broken_pipe = _FakeQueue()
    broken_pipe.fail_put = True
    logger._shutdown_writer(live, broken_pipe)
    assert broken_pipe.snapshot() == []
    assert broken_pipe.cancelled
    assert broken_pipe.closed

    doomed = _FailingTerminateProcess()
    doomed.start()
    logger._shutdown_writer(doomed, _FakeQueue())
    assert doomed.joined == [2, 1]


@pytest.mark.parametrize("frame", [None, SimpleNamespace(f_back=None)])
def test_enqueue_tolerates_missing_caller_frames(
    tail_logger_env, tmp_path, monkeypatch, frame
):
    monkeypatch.setattr(logger_module.inspect, "currentframe", lambda: frame)
    logger = _make_tail_logger(tmp_path)

    logger.info("hello")

    payload = logger._local_queue.get_nowait()
    assert payload["msg"] == "hello"
    assert payload["fn"] is None
    assert payload["lineno"] == 0
    assert payload["func_name"] is None


def test_enqueue_tolerates_missing_grandparent_frame(
    tail_logger_env, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        logger_module.inspect,
        "currentframe",
        lambda: SimpleNamespace(f_back=SimpleNamespace(f_back=None)),
    )
    logger = _make_tail_logger(tmp_path)

    logger.info("hello")

    payload = logger._local_queue.get_nowait()
    assert payload["fn"] is None


def test_bridge_forwards_a_single_record_batch(tail_logger_env, tmp_path):
    logger = _make_tail_logger(tmp_path, batch_size=1, batch_timeout=10.0)

    def on_get(source, calls):
        if calls >= 1:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}], on_get)

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "one"}]


def test_bridge_breaks_the_batch_when_the_window_expires(tail_logger_env, tmp_path):
    logger = _make_tail_logger(tmp_path, batch_size=5, batch_timeout=0.0)

    def on_get(source, calls):
        if calls >= 2:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}], on_get)

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "one"}]


def test_bridge_breaks_when_an_empty_poll_outlasts_the_window(
    tail_logger_env, tmp_path, monkeypatch
):
    logger = _make_tail_logger(tmp_path, batch_size=5, batch_timeout=10.0)
    ticks = iter([100.0, 100.0, 110.0])

    monkeypatch.setattr(
        logger_module.time, "time", lambda: next(ticks, 200.0)
    )

    def on_get(source, calls):
        if calls >= 3:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}], on_get)

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "one"}]


def test_bridge_breaks_when_the_batch_fills_during_a_poll(
    tail_logger_env, tmp_path
):
    logger = _make_tail_logger(tmp_path, batch_size=5, batch_timeout=10.0)

    def on_get(source, calls):
        if calls == 2:
            logger._batch_size = 1
        if calls >= 3:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}], on_get)

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "one"}]


def test_bridge_keeps_polling_when_stop_is_not_requested(tail_logger_env, tmp_path):
    logger = _make_tail_logger(tmp_path, batch_size=5, batch_timeout=10.0)

    def on_get(source, calls):
        if calls >= 4:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}, {"msg": "two"}], on_get)

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "one"}, {"msg": "two"}]
