"""Micro-tail tests for nearly-unreachable defensive branches."""

import datetime as dt
import logging
import os
import queue
import sys
from types import SimpleNamespace

import flameo.logger  # noqa: F401  (ensures sys.modules entry exists)
from flameo.logger import _writer_process_main
from flameo.sessions import _decode_session

logger_module = sys.modules["flameo.logger"]

_DAY = dt.date(2024, 5, 6)
_DAY_TS = dt.datetime(2024, 5, 6, 12, 0, 0).timestamp()


def _payload(msg="hello", level=logging.INFO, created=_DAY_TS):
    return {
        "v": 1,
        "level": level,
        "msg": msg,
        "args": (),
        "kwargs": {},
        "created": created,
        "fn": None,
        "lineno": 0,
        "func_name": None,
    }


def test_writer_process_skips_full_candidate_on_rollover(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    prefix = f"flameo-{_DAY.isoformat()}"
    (log_dir / f"{prefix}.log").write_text("x" * 10, encoding="utf-8")
    (log_dir / f"{prefix}-1.log").write_text("y" * 500, encoding="utf-8")

    real_listdir = os.listdir

    def fake_listdir(path):
        return [n for n in real_listdir(path) if n != f"{prefix}-1.log"]

    monkeypatch.setattr(os, "listdir", fake_listdir)

    records = queue.Queue()
    records.put(_payload("first " + "a" * 200))
    records.put(_payload("second " + "b" * 200))
    records.put(None)

    _writer_process_main(str(log_dir), records, max_bytes=100)

    assert "second" in (log_dir / f"{prefix}-2.log").read_text(encoding="utf-8")
    assert "second" not in (log_dir / f"{prefix}-1.log").read_text(encoding="utf-8")


def test_decode_session_returns_none_when_b64_raises():
    assert _decode_session("long-enough-secret-for-tests", "a.a", 60) is None


def test_resolve_safe_path_falls_back_when_no_user_frame(monkeypatch):
    import flameo.cycles as cycles

    frames = [
        SimpleNamespace(filename="tests/unit/test_x.py"),
        SimpleNamespace(filename="/app/flameo/cycles.py"),
        SimpleNamespace(filename=""),
        SimpleNamespace(filename="/app/flameo/flameo.py"),
    ]
    monkeypatch.setattr(cycles.inspect, "stack", lambda: frames)
    out = cycles._resolve_safe_path("a.txt")
    assert out[0].endswith("a.txt")


def test_resolve_safe_path_falls_back_when_stack_raises(monkeypatch):
    import flameo.cycles as cycles

    def boom():
        raise RuntimeError("no stack")

    monkeypatch.setattr(cycles.inspect, "stack", boom)
    out = cycles._resolve_safe_path("a.txt")
    assert out[0].endswith("a.txt")
