import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .parser import get_temp_dir

_log = logging.getLogger("flameo.cleanup")

# Filename prefixes the spill files use. Anything else under the temp
# dir is left alone (a deploy script might drop a sentinel file there
# intentionally, e.g. ``.gitkeep``).
_SPILL_PREFIXES = ("upload_", "body_")

# The first pass runs when ``start()`` is scheduled so a fresh process
# never carries old junk. The periodic timer kicks in afterwards.
_DEFAULT_INTERVAL = 30 * 60        # 30 minutes
_DEFAULT_THRESHOLD = 30 * 60       # 30 minutes


def _iter_spill_files(directory: Path):
    """Yield every spill file inside ``directory``.

    Skips directories, hidden files, and anything whose name does not
    start with one of the known spill prefixes. The check is purely
    lexical -- we don't stat the contents, just hand the Path objects
    to the caller.
    """
    if not directory.exists() or not directory.is_dir():
        return
    try:
        entries = directory.iterdir()
    except OSError:
        return
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith("."):
            continue
        if not name.startswith(_SPILL_PREFIXES):
            continue
        yield entry


def _sweep_once(threshold_seconds: float) -> int:
    """Synchronous one-shot sweep. Returns number of files removed.

    Run on a thread via ``asyncio.to_thread`` from the asyncio side
    because ``Path.stat`` and ``Path.unlink`` can block (especially
    on slow disks or large directories). The work is tiny -- a
    single ``iterdir`` + a ``stat`` per file -- but on Windows
    ``iterdir`` can briefly stall, so we keep it off the loop.
    """
    now = time.time()
    threshold_cutoff = now - threshold_seconds
    removed = 0
    for path in _iter_spill_files(get_temp_dir()):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # File vanished between ``iterdir`` and ``stat`` -- a
            # concurrent request just uploaded it. Move on.
            continue
        if mtime >= threshold_cutoff:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            # Permission denied / file in use / etc. Don't crash the
            # sweeper for one stray file -- log and continue. We'll
            # re-evaluate it next interval.
            _log.debug("cleanup: could not delete %s: %s", path, e)
    return removed


class TempSweeper:
    """Background task that periodically cleans stale spill files.
    """

    def __init__(
        self,
        interval_seconds: float = _DEFAULT_INTERVAL,
        threshold_seconds: float = _DEFAULT_THRESHOLD,
    ):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if threshold_seconds <= 0:
            raise ValueError("threshold_seconds must be > 0")
        self.interval_seconds = interval_seconds
        self.threshold_seconds = threshold_seconds
        self._task: Optional[asyncio.Task] = None
        self._started = False
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Run the startup sweep, then start the periodic timer.

        Called once during app startup. Returns immediately -- the
        background timer is fire-and-forget until ``stop()``.
        """
        if self._task is not None:
            return
        
        if self._started:
            return

        try:
            get_temp_dir().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _log.warning("cleanup: cannot create temp dir: %s", e)
            return
        self._started = True

        try:
            removed = await asyncio.to_thread(_sweep_once, self.threshold_seconds)
            if removed:
                _log.info(
                    "cleanup: removed %d stale spill file(s) on startup", removed
                )
        except Exception as e:
            _log.warning("cleanup: startup sweep failed: %s", e)

        # Periodic timer.
        self._task = asyncio.create_task(
            self._run_periodic(), name="flameo-temp-sweeper"
        )

    async def stop(self) -> None:
        """Cancel the periodic task. Idempotent."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _run_periodic(self) -> None:
        """Sleep loop: wait ``interval_seconds``, sweep, repeat.

        We sleep on ``self._stopping.wait()`` rather than
        ``asyncio.sleep`` so ``stop()`` unblocks immediately instead
        of waiting up to ``interval_seconds`` for the next tick.
        """
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.interval_seconds
                )
                
                break
            except asyncio.TimeoutError:
                pass  # normal timer expiry -- run a sweep

            try:
                removed = await asyncio.to_thread(
                    _sweep_once, self.threshold_seconds
                )
                if removed:
                    _log.info(
                        "cleanup: removed %d stale spill file(s)", removed
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log.warning("cleanup: periodic sweep failed: %s", e)


def start_sweeper(
    interval_seconds: float = _DEFAULT_INTERVAL,
    threshold_seconds: float = _DEFAULT_THRESHOLD,
) -> TempSweeper:
    """Convenience factory used by :class:`Flameo`.

    Creates a ``TempSweeper`` and calls ``start()`` against the
    currently-running event loop. Returns the started instance so the
    caller can stash it for ``stop()`` on shutdown.
    """
    sweeper = TempSweeper(
        interval_seconds=interval_seconds,
        threshold_seconds=threshold_seconds
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return sweeper
    loop.create_task(sweeper.start())
    return sweeper
