import datetime as dt
import logging
import os
import pathlib
import queue
import sys
import threading

import pytest

import flameo.logger  # noqa: F401  (ensures sys.modules entry exists)
from flameo.logger import FlameoLogger, _resolve_log_dir, _writer_process_main

logger_module = sys.modules["flameo.logger"]


_DAY = dt.date(2024, 5, 6)
_DAY_TS = dt.datetime(2024, 5, 6, 12, 0, 0).timestamp()


class _FakeQueue:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()
        self.closed = False
        self.cancelled = False
        self.fail_put = False
        self.fail_close = False
        self.fail_cancel = False

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
        if self.fail_close:
            raise OSError("close failed")

    def cancel_join_thread(self):
        self.cancelled = True
        if self.fail_cancel:
            raise OSError("cancel failed")

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


class _BrokenProcess(_FakeProcess):
    def is_alive(self):
        raise RuntimeError("process is broken")


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
        self.put_items = []

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
        self.put_items.append(item)


class _BrokenQueue:
    def put_nowait(self, item):
        raise OSError("queue is full")

    def get_nowait(self):
        raise queue.Empty()

    def get(self, timeout=None):
        raise queue.Empty()


class _PathShim:
    def __init__(self, real):
        self._real = real
        self.getsize_error = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    def getsize(self, path):
        if self.getsize_error is not None:
            raise self.getsize_error
        return self._real.getsize(path)


class _OsShim:
    def __init__(self, real):
        self._real = real
        self.path = _PathShim(real.path)
        self.listdir_error = None
        self.fstat_error = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    def listdir(self, path):
        if self.listdir_error is not None:
            raise self.listdir_error
        return self._real.listdir(path)

    def fstat(self, fd):
        if self.fstat_error is not None:
            raise self.fstat_error
        return self._real.fstat(fd)


class _LoggingShim:
    def __init__(self, real, file_handler=None):
        self._real = real
        if file_handler is not None:
            self.FileHandler = file_handler

    def __getattr__(self, name):
        return getattr(self._real, name)


class _LoggerEnv:
    def __init__(self, mp_shim, threading_shim):
        self.mp = mp_shim
        self.threading = threading_shim


@pytest.fixture
def logger_env(monkeypatch):
    mp_shim = _MpShim()
    threading_shim = _ThreadingShim()
    monkeypatch.setattr(logger_module, "mp", mp_shim)
    monkeypatch.setattr(logger_module, "threading", threading_shim)
    return _LoggerEnv(mp_shim, threading_shim)


def _make_logger(tmp_path, **kwargs):
    kwargs.setdefault("log_dir", str(tmp_path / "logs"))
    kwargs.setdefault("batch_size", 5)
    kwargs.setdefault("batch_timeout", 0.05)
    return FlameoLogger(**kwargs)


def _payload(msg="hello", level=logging.INFO, created=_DAY_TS, fn=None, lineno=0,
             func_name=None, args=(), kwargs=None):
    return {
        "v": 1,
        "level": level,
        "msg": msg,
        "args": args,
        "kwargs": kwargs if kwargs is not None else {},
        "created": created,
        "fn": fn,
        "lineno": lineno,
        "func_name": func_name,
    }


def test_resolve_log_dir_handles_default_blank_relative_and_home(tmp_path, monkeypatch):
    cwd = pathlib.Path.cwd()
    assert _resolve_log_dir() == cwd / "logs"
    assert _resolve_log_dir("") == cwd / "logs"
    assert _resolve_log_dir("   ") == cwd / "logs"
    assert _resolve_log_dir("nested/relative") == (cwd / "nested" / "relative").resolve()
    assert _resolve_log_dir(str(tmp_path)) == tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert _resolve_log_dir("~/logs") == (tmp_path / "logs").resolve()


def test_writer_process_writes_records_to_daily_file(tmp_path):
    log_dir = tmp_path / "logs"
    records = queue.Queue()
    records.put(_payload("hello %s", args=("world",), fn="/srv/app/service.py",
                          lineno=42, func_name="handle"))
    records.put(_payload("boom", level=logging.ERROR))
    records.put(None)

    _writer_process_main(str(log_dir), records, max_bytes=1024 * 1024)

    files = sorted(log_dir.glob("*.log"))
    assert [item.name for item in files] == [f"flameo-{_DAY.isoformat()}.log"]
    text = files[0].read_text(encoding="utf-8")
    assert "hello world" in text
    assert "[INFO]" in text
    assert "service.handle:42" in text
    assert "[ERROR]" in text
    assert "flameo.<unknown>:0" in text


def test_writer_process_resumes_the_last_numbered_file(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    prefix = f"flameo-{_DAY.isoformat()}"
    (log_dir / f"{prefix}.log").write_text("older\n", encoding="utf-8")
    (log_dir / f"{prefix}-2.log").write_text("newest\n", encoding="utf-8")
    (log_dir / f"{prefix}-x.log").write_text("ignored\n", encoding="utf-8")
    (log_dir / "unrelated.log").write_text("ignored\n", encoding="utf-8")

    records = queue.Queue()
    records.put(_payload("resumed"))
    records.put(None)

    _writer_process_main(str(log_dir), records, max_bytes=1024 * 1024)

    assert "resumed" in (log_dir / f"{prefix}-2.log").read_text(encoding="utf-8")
    assert "resumed" not in (log_dir / f"{prefix}.log").read_text(encoding="utf-8")
    assert (log_dir / f"{prefix}-x.log").read_text(encoding="utf-8") == "ignored\n"


def test_writer_process_rolls_over_into_numbered_files(tmp_path):
    log_dir = tmp_path / "logs"
    records = queue.Queue()
    for index in range(3):
        records.put(_payload(f"payload {index} " + "x" * 200))
    records.put(None)

    _writer_process_main(str(log_dir), records, max_bytes=100)

    prefix = f"flameo-{_DAY.isoformat()}"
    assert {item.name for item in log_dir.glob("*.log")} == {
        f"{prefix}.log", f"{prefix}-1.log", f"{prefix}-2.log",
    }


def test_writer_process_skips_numbered_files_that_are_already_full(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    prefix = f"flameo-{_DAY.isoformat()}"
    (log_dir / f"{prefix}.log").write_text("x" * 500, encoding="utf-8")
    (log_dir / f"{prefix}-1.log").write_text("y" * 500, encoding="utf-8")

    records = queue.Queue()
    records.put(_payload("fresh"))
    records.put(None)

    _writer_process_main(str(log_dir), records, max_bytes=100)

    assert (log_dir / f"{prefix}-1.log").read_text(encoding="utf-8") == "y" * 500
    assert "fresh" in (log_dir / f"{prefix}-2.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("error", [EOFError("parent gone"), OSError("parent gone")])
def test_writer_process_closes_handlers_when_the_parent_dies(tmp_path, error):
    log_dir = tmp_path / "logs"

    class _DyingQueue:
        def __init__(self):
            self.sent = False

        def get(self, timeout=None):
            if not self.sent:
                self.sent = True
                return _payload("survivor")
            raise error

    _writer_process_main(str(log_dir), _DyingQueue(), max_bytes=1024 * 1024)

    files = sorted(log_dir.glob("*.log"))
    assert [item.name for item in files] == [f"flameo-{_DAY.isoformat()}.log"]
    assert "survivor" in files[0].read_text(encoding="utf-8")


def test_writer_process_falls_back_when_file_stats_fail(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    os_shim = _OsShim(os)
    os_shim.listdir_error = OSError("listdir unavailable")
    os_shim.path.getsize_error = OSError("getsize unavailable")
    os_shim.fstat_error = ValueError("stream closed")
    monkeypatch.setattr(logger_module, "os", os_shim)

    records = queue.Queue()
    records.put(_payload("first " + "x" * 100))
    records.put(_payload("second " + "y" * 100))
    records.put(None)

    _writer_process_main(str(log_dir), records, max_bytes=100)

    prefix = f"flameo-{_DAY.isoformat()}"
    assert {item.name for item in log_dir.glob("*.log")} == {
        f"{prefix}.log", f"{prefix}-1.log",
    }


def test_writer_process_survives_handler_close_failures(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"

    class _FailingCloseHandler(logging.FileHandler):
        def close(self):
            try:
                super().close()
            finally:
                raise OSError("close failed")

    monkeypatch.setattr(logger_module, "logging", _LoggingShim(logging, _FailingCloseHandler))

    records = queue.Queue()
    records.put(_payload("x" * 200))
    records.put(_payload("y" * 200))
    records.put(None)

    _writer_process_main(str(log_dir), records, max_bytes=100)

    prefix = f"flameo-{_DAY.isoformat()}"
    assert {item.name for item in log_dir.glob("*.log")} == {
        f"{prefix}.log", f"{prefix}-1.log",
    }


def test_enqueue_captures_level_arguments_and_caller_info(logger_env, tmp_path):
    logger = _make_logger(tmp_path)

    logger.info("hello %s", "world")

    payload = logger._local_queue.get_nowait()
    assert payload["level"] == logging.INFO
    assert payload["msg"] == "hello %s"
    assert payload["args"] == ("world",)
    assert payload["kwargs"] == {}
    assert payload["v"] == logger._PAYLOAD_VERSION
    assert payload["fn"].endswith("test_logger.py")
    assert payload["func_name"] == "test_enqueue_captures_level_arguments_and_caller_info"
    assert payload["lineno"] > 0
    assert payload["created"] > 0


def test_log_level_helpers_use_the_expected_levels(logger_env, tmp_path):
    logger = _make_logger(tmp_path)

    logger.warning("w")
    assert logger._local_queue.get_nowait()["level"] == logging.WARNING
    logger.debug("d")
    assert logger._local_queue.get_nowait()["level"] == logging.DEBUG
    logger.error("e")
    assert logger._local_queue.get_nowait()["level"] == logging.ERROR
    assert logger._immediate_evt.is_set()
    logger._immediate_evt.clear()
    logger.critical("c")
    assert logger._local_queue.get_nowait()["level"] == logging.CRITICAL
    assert logger._immediate_evt.is_set()
    logger._immediate_evt.clear()
    logger.Info("i")
    assert logger._local_queue.get_nowait()["level"] == logging.INFO
    logger.Warning("w2")
    assert logger._local_queue.get_nowait()["level"] == logging.WARNING
    logger.Debug("d2")
    assert logger._local_queue.get_nowait()["level"] == logging.DEBUG
    logger.Error("e2")
    assert logger._local_queue.get_nowait()["level"] == logging.ERROR
    logger.Critical("c2")
    assert logger._local_queue.get_nowait()["level"] == logging.CRITICAL


def test_enqueue_drops_the_payload_when_the_local_queue_fails(logger_env, tmp_path):
    logger = _make_logger(tmp_path)

    class _BrokenQueue:
        def put_nowait(self, item):
            raise OSError("queue is full")

        def get_nowait(self):
            raise queue.Empty()

        def get(self, timeout=None):
            raise queue.Empty()

    logger._local_queue = _BrokenQueue()
    logger.info("dropped")
    logger.error("dropped too")

    assert not logger._immediate_evt.is_set()


def test_flush_marks_the_batch_for_immediate_forwarding(logger_env, tmp_path):
    logger = _make_logger(tmp_path)
    logger.flush()
    assert logger._immediate_evt.is_set()


def test_log_dir_and_max_log_bytes_properties_follow_the_setters(logger_env, tmp_path):
    logger = _make_logger(tmp_path, max_log_bytes=2048)
    assert logger.log_dir == (tmp_path / "logs").resolve()
    assert logger.max_log_bytes == 2048

    first_queue = logger._mp_queue
    new_dir = logger.set_log_dir(str(tmp_path / "other"))
    assert new_dir == (tmp_path / "other").resolve()
    assert logger.log_dir == new_dir
    assert logger._mp_queue is not first_queue

    assert logger.set_max_log_bytes(4096) == 4096
    assert logger.max_log_bytes == 4096
    assert logger.set_max_log_bytes(0) == 1
    assert logger.max_log_bytes == 1
    assert logger.set_max_log_bytes(-10) == 1


def test_set_log_dir_restarts_a_running_writer(logger_env, tmp_path):
    logger = _make_logger(tmp_path)
    process = _FakeProcess()
    process.start()
    logger._writer_proc = process
    old_queue = logger._mp_queue

    logger.set_log_dir(str(tmp_path / "moved"))

    assert logger._writer_proc is None
    assert process.terminated
    assert process.joined == [2, 1]
    assert old_queue.snapshot()[-1] is None
    assert old_queue.cancelled
    assert old_queue.closed


def test_set_max_log_bytes_restarts_a_running_writer(logger_env, tmp_path):
    logger = _make_logger(tmp_path)
    process = _FakeProcess()
    process.start()
    logger._writer_proc = process
    old_queue = logger._mp_queue

    assert logger.set_max_log_bytes(512) == 512

    assert logger._writer_proc is None
    assert process.terminated
    assert old_queue.snapshot()[-1] is None
    assert logger.max_log_bytes == 512


def test_shutdown_writer_handles_missing_broken_and_dead_processes(logger_env, tmp_path):
    logger = _make_logger(tmp_path)

    unused_queue = _FakeQueue()
    logger._shutdown_writer(None, unused_queue)
    assert not unused_queue.closed
    assert not unused_queue.cancelled

    dead_process = _FakeProcess()
    logger._shutdown_writer(dead_process, unused_queue)
    assert unused_queue.closed
    assert unused_queue.cancelled

    unused_queue.fail_close = True
    unused_queue.fail_cancel = True
    logger._shutdown_writer(dead_process, unused_queue)
    assert unused_queue.closed
    assert unused_queue.cancelled

    broken = _BrokenProcess()
    broken._popen = object()
    broken_queue = _FakeQueue()
    logger._shutdown_writer(broken, broken_queue)
    assert broken_queue.closed
    assert broken_queue.cancelled


def test_ensure_writer_starts_one_process_and_reuses_a_live_one(logger_env, tmp_path):
    logger = _make_logger(tmp_path, max_log_bytes=123)

    logger._ensure_writer()
    assert len(logger_env.mp.processes) == 1
    process = logger_env.mp.processes[0]
    assert process.started
    assert process.name == "flameo-logger-writer"
    assert process.daemon is True
    assert process.args[0] == str(logger.log_dir)
    assert process.args[2] == 123

    logger._ensure_writer()
    assert len(logger_env.mp.processes) == 1

    process.alive = False
    logger._ensure_writer()
    assert len(logger_env.mp.processes) == 2


def test_stop_drains_pending_records_and_terminates_the_writer(logger_env, tmp_path):
    logger = _make_logger(tmp_path)
    process = _FakeProcess()
    process.start()
    logger._writer_proc = process
    logger._local_queue.put_nowait({"msg": "pending"})

    logger.stop()

    assert logger._stop_bridge.is_set()
    forwarded = logger._mp_queue.snapshot()
    assert forwarded[0] == {"msg": "pending"}
    assert forwarded[-1] is None
    assert process.terminated
    assert process.joined == [5, 2]

    logger.stop()
    assert len(logger_env.mp.processes) == 0


def test_stop_tolerates_a_missing_writer_and_broken_queues(logger_env, tmp_path):
    logger = _make_logger(tmp_path)
    logger._writer_proc = _FakeProcess()
    logger.stop()
    assert logger._stop_bridge.is_set()

    other = _make_logger(tmp_path)
    other._local_queue = _BrokenQueue()
    other._mp_queue.fail_put = True
    other.stop()
    assert other._stop_bridge.is_set()


def test_bridge_forwards_a_batch_to_the_writer_queue(logger_env, tmp_path):
    logger = _make_logger(tmp_path, batch_size=3, batch_timeout=10.0)

    def on_get(source, calls):
        if calls >= 3:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}, {"msg": "two"}], on_get)

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "one"}, {"msg": "two"}]


def test_bridge_flushes_pending_records_on_the_stop_sentinel(logger_env, tmp_path):
    logger = _make_logger(tmp_path)
    logger._local_queue = _ScriptedQueue([None, {"msg": "late"}])

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "late"}]


def test_bridge_exits_when_the_queue_is_empty_and_stop_is_requested(logger_env, tmp_path):
    logger = _make_logger(tmp_path, batch_timeout=0.01)

    def on_get(source, calls):
        logger._stop_bridge.set()

    scripted = _ScriptedQueue([], on_get)
    logger._local_queue = scripted

    logger._bridge_main()

    assert scripted.get_calls == 1


def test_bridge_breaks_the_batch_on_an_immediate_flush(logger_env, tmp_path):
    logger = _make_logger(tmp_path, batch_size=5, batch_timeout=10.0)

    def on_get(source, calls):
        if calls >= 3:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}], on_get)
    logger._immediate_evt.set()

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == [{"msg": "one"}]
    assert not logger._immediate_evt.is_set()


def test_bridge_drops_the_batch_when_the_writer_queue_rejects_it(logger_env, tmp_path):
    logger = _make_logger(tmp_path, batch_size=2, batch_timeout=10.0)

    def on_get(source, calls):
        if calls >= 2:
            logger._stop_bridge.set()

    logger._local_queue = _ScriptedQueue([{"msg": "one"}], on_get)
    logger._mp_queue.fail_put = True

    logger._bridge_main()

    assert logger._mp_queue.snapshot() == []


def test_flush_pending_to_writer_moves_records_and_stops_on_failure(logger_env, tmp_path):
    logger = _make_logger(tmp_path)
    logger._local_queue = _ScriptedQueue([{"msg": "a"}, {"msg": "b"}])

    logger._flush_pending_to_writer()
    assert logger._mp_queue.snapshot() == [{"msg": "a"}, {"msg": "b"}]

    logger._mp_queue.fail_put = True
    logger._local_queue = _ScriptedQueue([{"msg": "c"}])
    logger._flush_pending_to_writer()
    assert logger._mp_queue.snapshot() == [{"msg": "a"}, {"msg": "b"}]


def test_module_level_helpers_delegate_to_the_shared_logger(monkeypatch, tmp_path):
    calls = []

    class _StubLogger:
        def set_log_dir(self, directory):
            calls.append(("dir", directory))
            return pathlib.Path(directory)

        def set_max_log_bytes(self, size):
            calls.append(("bytes", size))
            return size

    monkeypatch.setattr(logger_module, "logger", _StubLogger())

    assert logger_module.set_log_dir(str(tmp_path)) == pathlib.Path(str(tmp_path))
    assert logger_module.set_max_log_bytes(4096) == 4096
    assert calls == [("dir", str(tmp_path)), ("bytes", 4096)]


def test_flameo_app_applies_log_directory_and_size(monkeypatch, app_factory, tmp_path):
    calls = {}

    class _StubLogger:
        def set_log_dir(self, directory):
            calls["log_dir"] = directory
            return pathlib.Path(directory)

        def set_max_log_bytes(self, size):
            calls["max_log_bytes"] = size
            return size

    monkeypatch.setattr(logger_module, "logger", _StubLogger())

    app_factory(log_dir=str(tmp_path / "logs"), max_log_bytes=8192)

    assert calls == {"log_dir": str(tmp_path / "logs"), "max_log_bytes": 8192}
