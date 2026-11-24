import asyncio
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

import flameo.cleanup as cleanup


def _write(path):
    path.write_text("x", encoding="utf-8")
    return path


def _make_stale(path, age_seconds=100):
    _write(path)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


class _BrokenDir:
    def exists(self):
        return True

    def is_dir(self):
        return True

    def iterdir(self):
        raise OSError("cannot list directory")


class _VanishingFile:
    name = "upload_vanishing.part"

    def stat(self):
        raise OSError("file vanished")

    def unlink(self):
        raise AssertionError("unlink must not be called")


class _LockedFile:
    name = "upload_locked.part"

    def stat(self):
        return SimpleNamespace(st_mtime=0.0)

    def unlink(self):
        raise PermissionError("file in use")


def test_iter_spill_files_returns_empty_for_missing_or_non_dir(tmp_path):
    missing = tmp_path / "missing"
    regular = _write(tmp_path / "regular.txt")
    assert list(cleanup._iter_spill_files(missing)) == []
    assert list(cleanup._iter_spill_files(regular)) == []


def test_iter_spill_files_handles_iterdir_oserror():
    assert list(cleanup._iter_spill_files(_BrokenDir())) == []


def test_iter_spill_files_filters_dirs_and_non_spill_names(tmp_path):
    _write(tmp_path / "upload_ok.part")
    _write(tmp_path / "body_ok.part")
    _write(tmp_path / "keep.txt")
    _write(tmp_path / ".gitkeep")
    (tmp_path / "upload_subdir").mkdir()
    found = sorted(entry.name for entry in cleanup._iter_spill_files(tmp_path))
    assert found == ["body_ok.part", "upload_ok.part"]


def test_sweep_once_skips_files_that_vanish_before_stat(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cleanup, "_iter_spill_files", lambda _directory: iter([_VanishingFile()])
    )
    assert cleanup._sweep_once(10) == 0


def test_sweep_once_logs_and_skips_locked_files(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cleanup, "_iter_spill_files", lambda _directory: iter([_LockedFile()])
    )
    with caplog.at_level(logging.DEBUG, logger="flameo.cleanup"):
        assert cleanup._sweep_once(10) == 0
    assert "could not delete" in caplog.text


def test_temp_sweeper_rejects_non_positive_arguments():
    with pytest.raises(ValueError, match="interval_seconds"):
        cleanup.TempSweeper(interval_seconds=0)
    with pytest.raises(ValueError, match="interval_seconds"):
        cleanup.TempSweeper(interval_seconds=-1)
    with pytest.raises(ValueError, match="threshold_seconds"):
        cleanup.TempSweeper(threshold_seconds=0)
    with pytest.raises(ValueError, match="threshold_seconds"):
        cleanup.TempSweeper(threshold_seconds=-5)


def test_temp_sweeper_initial_state_uses_defaults():
    sweeper = cleanup.TempSweeper()
    assert sweeper.interval_seconds == cleanup._DEFAULT_INTERVAL == 30 * 60
    assert sweeper.threshold_seconds == cleanup._DEFAULT_THRESHOLD == 30 * 60
    assert sweeper._task is None
    assert sweeper._started is False
    assert not sweeper._stopping.is_set()


def test_sweep_once_default_threshold_removes_only_files_older_than_30_minutes(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    old = _make_stale(tmp_path / "upload_old.part", age_seconds=31 * 60)
    recent = _make_stale(tmp_path / "upload_recent.part", age_seconds=29 * 60)

    assert cleanup._sweep_once(cleanup._DEFAULT_THRESHOLD) == 1
    assert not old.exists()
    assert recent.exists()


@pytest.mark.asyncio
async def test_start_sweeps_stale_files_and_spawns_periodic_task(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    _make_stale(tmp_path / "upload_stale.part")
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)
    with caplog.at_level(logging.INFO, logger="flameo.cleanup"):
        await sweeper.start()
    assert not (tmp_path / "upload_stale.part").exists()
    assert sweeper._started is True
    assert sweeper._task is not None
    assert sweeper._task.get_name() == "flameo-temp-sweeper"
    assert "on startup" in caplog.text
    await sweeper.stop()
    assert sweeper._task is None


@pytest.mark.asyncio
async def test_start_creates_missing_temp_dir_without_logging(
    monkeypatch, tmp_path, caplog
):
    target = tmp_path / "nested" / "spills"
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: target)
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)
    with caplog.at_level(logging.INFO, logger="flameo.cleanup"):
        await sweeper.start()
    assert target.is_dir()
    assert sweeper._task is not None
    assert "removed" not in caplog.text
    await sweeper.stop()


@pytest.mark.asyncio
async def test_start_survives_startup_sweep_failure(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)

    def explode(_threshold):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(cleanup, "_sweep_once", explode)
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)
    with caplog.at_level(logging.WARNING, logger="flameo.cleanup"):
        await sweeper.start()
    assert "startup sweep failed" in caplog.text
    assert sweeper._task is not None
    await sweeper.stop()


@pytest.mark.asyncio
async def test_start_returns_early_when_periodic_task_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(
        cleanup, "_sweep_once", lambda threshold: calls.append(threshold) or 0
    )
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)

    async def noop():
        return None

    task = asyncio.create_task(noop())
    await task
    sweeper._task = task
    await sweeper.start()
    assert sweeper._started is False
    assert calls == []
    await sweeper.stop()


@pytest.mark.asyncio
async def test_start_returns_early_when_started_flag_set(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(
        cleanup, "_sweep_once", lambda threshold: calls.append(threshold) or 0
    )
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)
    sweeper._started = True
    await sweeper.start()
    assert sweeper._task is None
    assert calls == []


@pytest.mark.asyncio
async def test_stop_is_idempotent_without_task():
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)
    await sweeper.stop()
    assert sweeper._stopping.is_set()
    assert sweeper._task is None
    await sweeper.stop()
    assert sweeper._task is None


@pytest.mark.asyncio
async def test_stop_skips_cancelling_finished_task():
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)

    async def noop():
        return None

    task = asyncio.create_task(noop())
    await task
    sweeper._task = task
    await sweeper.stop()
    assert sweeper._task is None
    assert task.done()


@pytest.mark.asyncio
async def test_stop_cancels_running_periodic_task_during_sweep(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_sweep(_threshold):
        calls.append(1)
        if len(calls) == 1:
            return 0
        entered.set()
        release.wait(2)
        return 0

    monkeypatch.setattr(cleanup, "_sweep_once", blocking_sweep)
    sweeper = cleanup.TempSweeper(interval_seconds=0.02, threshold_seconds=10)
    try:
        await sweeper.start()
        for _ in range(400):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        await sweeper.stop()
        assert sweeper._task is None
        assert sweeper._stopping.is_set()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_run_periodic_breaks_when_stopped_while_waiting():
    sweeper = cleanup.TempSweeper(interval_seconds=30, threshold_seconds=10)
    task = asyncio.create_task(sweeper._run_periodic())
    await asyncio.sleep(0.01)
    sweeper._stopping.set()
    await asyncio.wait_for(task, timeout=1)
    assert task.done()


@pytest.mark.asyncio
async def test_run_periodic_sweeps_after_interval_timeout(monkeypatch, caplog):
    sweeper = cleanup.TempSweeper(interval_seconds=0.05, threshold_seconds=10)
    loop = asyncio.get_running_loop()
    calls = []

    def sweep(threshold):
        calls.append(threshold)
        loop.call_soon_threadsafe(sweeper._stopping.set)
        return 2

    monkeypatch.setattr(cleanup, "_sweep_once", sweep)
    task = asyncio.create_task(sweeper._run_periodic())
    with caplog.at_level(logging.INFO, logger="flameo.cleanup"):
        await asyncio.wait_for(task, timeout=2)
    assert calls == [10]
    assert "removed 2 stale" in caplog.text


@pytest.mark.asyncio
async def test_run_periodic_logs_sweep_failure_and_continues(monkeypatch, caplog):
    sweeper = cleanup.TempSweeper(interval_seconds=0.05, threshold_seconds=10)
    loop = asyncio.get_running_loop()
    calls = []

    def flaky(threshold):
        calls.append(threshold)
        if len(calls) == 1:
            raise RuntimeError("transient failure")
        loop.call_soon_threadsafe(sweeper._stopping.set)
        return 0

    monkeypatch.setattr(cleanup, "_sweep_once", flaky)
    task = asyncio.create_task(sweeper._run_periodic())
    with caplog.at_level(logging.WARNING, logger="flameo.cleanup"):
        await asyncio.wait_for(task, timeout=2)
    assert len(calls) == 2
    assert "periodic sweep failed" in caplog.text


@pytest.mark.asyncio
async def test_start_sweeper_schedules_start_on_running_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    sweeper = cleanup.start_sweeper(interval_seconds=30, threshold_seconds=10)
    assert isinstance(sweeper, cleanup.TempSweeper)
    assert sweeper.interval_seconds == 30
    assert sweeper.threshold_seconds == 10
    for _ in range(400):
        if sweeper._task is not None:
            break
        await asyncio.sleep(0.005)
    assert sweeper._started is True
    assert sweeper._task is not None
    await sweeper.stop()
    assert sweeper._task is None


def test_start_sweeper_returns_unstarted_instance_without_running_loop():
    created = asyncio.new_event_loop()
    asyncio.set_event_loop(created)
    try:
        sweeper = cleanup.start_sweeper(interval_seconds=30, threshold_seconds=10)
        assert isinstance(sweeper, cleanup.TempSweeper)
        assert sweeper.interval_seconds == 30
        assert sweeper.threshold_seconds == 10
        assert sweeper._started is False
        assert sweeper._task is None
    finally:
        asyncio.set_event_loop(None)
        created.close()
