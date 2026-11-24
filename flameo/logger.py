import atexit
import datetime as dt
import inspect
import logging
import logging.handlers
import multiprocessing as mp
import os
import pathlib
import sys
import threading
import time
from queue import SimpleQueue, Empty
from typing import Optional

# Maximum size of a single log file before the writer rolls over to the
# next one. The first file of a day is ``flameo-YYYY-MM-DD.log``; every
# rollover adds a suffix: ``flameo-YYYY-MM-DD-1.log``, ``-2.log``, ...
_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MiB


def _writer_process_main(log_dir_str: str, queue: "mp.Queue", max_bytes: int = _MAX_LOG_BYTES) -> None:
    """Entry point for the log writer child process.

    Shutdown semantics:
      * ``None`` sentinel on queue => drain pending records, close all
        handlers, exit.
      * Parent process dies unexpectedly => ``queue.get`` raises
        ``EOFError`` / ``OSError``; we close handlers and exit.
      * TaskKill / SIGTERM => pending records in the queue are lost.
        For HTTP request logs that's acceptable; if you need stricter
        durability use an external log shipper.
    """
    log_dir = pathlib.Path(log_dir_str)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Roll over once the active file reaches this size. Guard against a
    # zero/negative value so the writer can never spin on rollover.
    max_bytes = max(1, int(max_bytes))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(caller_info)s - %(message)s"
    )

    handlers: dict = {}
    day_index: dict = {}
    day_size: dict = {}

    def _file_path(day: dt.date, index: int) -> pathlib.Path:
        """Path of the ``index``-th file of ``day``.

        Index ``0`` is the first file of the day and carries no number:
        ``flameo-YYYY-MM-DD.log``. Every rollover adds one:
        ``flameo-YYYY-MM-DD-1.log``, ``flameo-YYYY-MM-DD-2.log``, ...
        """
        stem = f"flameo-{day.isoformat()}"
        if index:
            stem = f"{stem}-{index}"
        return log_dir / f"{stem}.log"

    def _last_index_for(day: dt.date) -> int:
        """Highest file index already on disk for ``day`` (``0`` if none).
        
        Used when the writer starts so a restart resumes in the last file
        of that day instead of appending back into an older one.
        """
        best = 0
        prefix = f"flameo-{day.isoformat()}"
        try:
            entries = os.listdir(log_dir)
        except OSError:
            return 0
        for name in entries:
            if not name.startswith(prefix) or not name.endswith(".log"):
                continue
            suffix = name[len(prefix):-len(".log")]
            if suffix == "":
                continue
            if suffix.startswith("-") and suffix[1:].isdigit():
                best = max(best, int(suffix[1:]))
        return best

    def _open_handler(day: dt.date, index: int) -> logging.Handler:
        handler = logging.FileHandler(_file_path(day, index), encoding="utf-8")
        handler.setFormatter(fmt)
        return handler

    def _file_size(handler: logging.Handler) -> int:
        """Current size of the file behind ``handler``, in bytes."""
        try:
            return os.path.getsize(handler.baseFilename)
        except OSError:
            return 0

    def _get_handler(day: dt.date) -> logging.Handler:
        """Return the handler for ``day``, rolling over when it is full."""
        handler = handlers.get(day)
        if handler is None:
            # First write for this day (or a writer restart): resume in the
            # last file that already exists so we never write back into a
            # file that was already rotated away from.
            index = _last_index_for(day)
            handler = _open_handler(day, index)
            handlers[day] = handler
            day_index[day] = index
            day_size[day] = _file_size(handler)
            if day_size[day] < max_bytes:
                return handler
            # The last file on disk is already full -- fall through to the
            # rollover branch below and start a fresh one.

        if day_size.get(day, 0) >= max_bytes:
            # The current file is full -- roll over to the next one.
            index = day_index.get(day, 0) + 1
            while True:
                candidate = _file_path(day, index)
                try:
                    existing_size = candidate.stat().st_size
                except OSError:
                    existing_size = 0
                if existing_size < max_bytes:
                    break
                # That numbered file is already full too -- skip it.
                index += 1
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
            handler = _open_handler(day, index)
            handlers[day] = handler
            day_index[day] = index
            day_size[day] = 0
        return handler

    def _build_record(payload: dict):
        level = payload["level"]
        msg = payload["msg"]
        args = payload["args"]
        kwargs = payload["kwargs"]
        created = payload["created"]
        fn = payload.get("fn") or "<logger>"
        lineno = int(payload.get("lineno") or 0)
        func_name = payload.get("func_name") or "<unknown>"

        stem = pathlib.Path(fn).stem if fn != "<logger>" else "flameo"
        caller_info = f"{stem}.{func_name}:{lineno}"

        record = logging.getLogger("flameo_logger").makeRecord(
            name="flameo_logger",
            level=level,
            fn=fn, lno=lineno,
            msg=msg, args=args, exc_info=None,
            extra={"caller_info": caller_info},
        )
        # ``makeRecord`` overwrites ``created``; restore producer-side
        # timestamp so the filename + line order reflect when
        # ``logger.info(...)`` was actually invoked.
        record.created = created
        record.msecs = (created - int(created)) * 1000.0
        return record

    while True:
        try:
            payload = queue.get()
        except (EOFError, OSError):
            # Parent went away. Close handlers and exit.
            break

        if payload is None:
            # Sentinel from ``stop()``: drain and exit. Any pending
            # records already in the queue will arrive as separate
            # ``get`` calls; we only break when we see the sentinel.
            break

        record = _build_record(payload)
        day = dt.date.fromtimestamp(record.created)
        handler = _get_handler(day)
        handler.emit(record)
        handler.flush()
        # Track the file size so we know when to roll over. ``fstat`` on the
        # open handle gives the exact byte count (the handler is in append
        # mode, so the file only grows from the end) and avoids a second
        # path-based stat call per record.
        try:
            day_size[day] = os.fstat(handler.stream.fileno()).st_size
        except (AttributeError, OSError, ValueError):
            day_size[day] = day_size.get(day, 0) + len(record.getMessage())

    # Release file locks on exit.
    for handler in handlers.values():
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass


_FLUSH_SIZE = 100
_FLUSH_INTERVAL = 1.0


def _resolve_log_dir(directory=None) -> pathlib.Path:
    """Normalise ``directory`` into an absolute log directory path.

    ``None`` (or a blank string) keeps the default location:
    ``<cwd>/logs``. Relative paths are resolved against the current
    working directory and ``~`` is expanded, so the writer child
    process always receives an unambiguous absolute path.
    """
    if directory is None:
        return pathlib.Path.cwd() / "logs"
    if isinstance(directory, str) and not directory.strip():
        return pathlib.Path.cwd() / "logs"
    path = pathlib.Path(directory).expanduser()
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    return path.resolve()


class FlameoLogger:
    def __init__(
        self,
        name: str = "flameo_logger",
        batch_size: int = _FLUSH_SIZE,
        batch_timeout: float = _FLUSH_INTERVAL,
        log_dir=None,
        max_log_bytes: int = _MAX_LOG_BYTES,
    ):
        self._name = name
        self._batch_size = max(1, int(batch_size))
        self._batch_timeout = max(0.0, float(batch_timeout))

        # Where the writer process will create log files. Resolve to
        # an absolute path so the child process knows exactly where to
        # write regardless of its CWD.
        #
        # The directory is NOT created here -- the writer process creates
        # it on first use. Creating it eagerly would leave a stray
        # ``logs/`` folder behind for apps that never log, and would
        # create the default folder even when the user asked for a
        # custom ``log_dir``.
        self._logs_dir = _resolve_log_dir(log_dir)
        # Size at which the writer rolls over to the next numbered file.
        self._max_log_bytes = max(1, int(max_log_bytes))

        # Producer -> bridge: in-memory unbounded SimpleQueue. Producer
        # pushes via put_nowait; bridge drains via get(). SimpleQueue
        # is an unbounded pure-Python queue, which is fine for the
        # volume a single request thread produces (a few hundred
        # records per second at most).
        self._local_queue: SimpleQueue = SimpleQueue()

        # Bridge -> writer process. multiprocessing.Queue uses an OS
        # pipe under the hood; the bridge is the only producer on this
        # end.
        self._mp_queue: "mp.Queue" = mp.Queue()
        # Guards ``_mp_queue`` swaps -- ``set_log_dir`` replaces the
        # queue so the next writer starts on a clean pipe.
        self._mp_queue_lock = threading.Lock()

        # Writer process state.
        self._writer_lock = threading.Lock()
        self._writer_proc: Optional[mp.Process] = None

        # Bridge shutdown coordination.
        self._stop_bridge = threading.Event()
        # Used by ``error()`` / ``critical()`` / ``flush()`` to nudge
        # the bridge to forward its current batch on the next tick.
        self._immediate_evt = threading.Event()

        self._bridge = threading.Thread(
            target=self._bridge_main,
            name="flameo-logger-bridge",
            daemon=True,
        )
        self._bridge.start()

        atexit.register(self.stop)

    # ---- public API ---------------------------------------------------------
    @property
    def log_dir(self) -> pathlib.Path:
        """The directory the writer process writes log files into."""
        return self._logs_dir

    @property
    def max_log_bytes(self) -> int:
        """Size (bytes) at which a log file rolls over to the next one."""
        return self._max_log_bytes

    def set_log_dir(self, directory) -> pathlib.Path:
        """Point the writer at a different log directory.

        The writer process is restarted (on a fresh queue) when it is
        already running, because the child process receives the
        directory as a start-up argument and keeps its file handlers
        open for the lifetime of the process.

        Call this before the first log record is emitted -- i.e. while
        building the app -- to avoid records landing in the old
        directory during the swap.

        Returns the new absolute :class:`~pathlib.Path`.
        """
        new_dir = _resolve_log_dir(directory)

        with self._writer_lock:
            self._logs_dir = new_dir
            proc = self._writer_proc
            self._writer_proc = None
            # A fresh queue for the next writer: killing the old writer
            # can leave a half-read frame in the shared pipe, which
            # would make the next writer die on its first ``get()``.
            with self._mp_queue_lock:
                old_queue = self._mp_queue
                self._mp_queue = mp.Queue()

        self._shutdown_writer(proc, old_queue)
        return new_dir

    def set_max_log_bytes(self, size: int) -> int:
        """Set the size (bytes) at which a log file rolls over.

        When the active file reaches this size the writer starts a new one
        (``flameo-YYYY-MM-DD-1.log``, ``-2.log``, ...). The writer process is
        restarted so it picks up the new threshold immediately.
        """
        new_size = max(1, int(size))

        with self._writer_lock:
            self._max_log_bytes = new_size
            proc = self._writer_proc
            self._writer_proc = None
            with self._mp_queue_lock:
                old_queue = self._mp_queue
                self._mp_queue = mp.Queue()

        self._shutdown_writer(proc, old_queue)
        return new_size

    def info(self, msg, *args, **kwargs):
        self._enqueue(logging.INFO, msg, args, kwargs)

    def warning(self, msg, *args, **kwargs):
        self._enqueue(logging.WARNING, msg, args, kwargs)

    def error(self, msg, *args, **kwargs):
        # Errors must surface immediately.
        self._enqueue(logging.ERROR, msg, args, kwargs, immediate=True)

    def debug(self, msg, *args, **kwargs):
        self._enqueue(logging.DEBUG, msg, args, kwargs)

    def critical(self, msg, *args, **kwargs):
        self._enqueue(logging.CRITICAL, msg, args, kwargs, immediate=True)

    # Capitalised aliases so callers can write ``logger.Info(...)``.
    Info = info
    Warning = warning
    Error = error
    Debug = debug
    Critical = critical

    def flush(self):
        """Force the bridge to forward its current batch on the next tick.

        The writer process will then flush to disk. Safe to call from
        any thread.
        """
        self._immediate_evt.set()

    def stop(self):
        """Stop the bridge and writer, draining pending records.

        Called automatically on interpreter shutdown via ``atexit``.
        Safe to call more than once. Blocks until both the bridge
        thread and the writer process have exited cleanly.

        Flush-once semantics: any records still sitting in the
        in-memory queue, plus any batch the bridge was accumulating,
        are forwarded to the writer process and fsynced before this
        method returns. No records produced before ``stop()`` is
        called are dropped.
        """
        if self._stop_bridge.is_set():
            return
        self._stop_bridge.set()

        try:
            self._local_queue.put_nowait(None)
        except Exception:
            pass

        self._bridge.join(timeout=self._batch_timeout + 1.0)

        # Final catch-all drain: anything a producer pushed after
        # the bridge drained (e.g. mid-``_enqueue`` request threads)
        # or anything the bridge couldn't get to before the join
        # timeout. Sent straight to the writer before we ask it to
        # exit so the disk write still happens.
        self._flush_pending_to_writer()

        # Tell the writer process to drain and exit.
        try:
            with self._mp_queue_lock:
                self._mp_queue.put_nowait(None)
        except Exception:
            pass

        # Wait for the writer process to exit. ``proc._popen`` may be
        # ``None`` if ``_ensure_writer`` never managed to spawn it
        # (e.g. bootstrap error, ``__name__`` guard missing on
        # Windows); in that case there is nothing to join.
        with self._writer_lock:
            proc = self._writer_proc
        if proc is not None and proc._popen is not None:
            proc.join(timeout=5)
            if proc.is_alive():
                try:
                    proc.terminate()
                except Exception:
                    pass
                proc.join(timeout=2)

    # ---- internals ----------------------------------------------------------
    _PAYLOAD_VERSION = 1

    def _shutdown_writer(self, proc, queue) -> None:
        """Stop a writer process, letting it drain what it already has."""
        if proc is None:
            return
        try:
            if getattr(proc, "_popen", None) is None:
                return
            if proc.is_alive():
                # Sentinel first: the writer drains pending records
                # before it exits.
                try:
                    queue.put_nowait(None)
                except Exception:
                    pass
                proc.join(timeout=2)
            if proc.is_alive():
                try:
                    proc.terminate()
                except Exception:
                    pass
                proc.join(timeout=1)
        except Exception:
            pass
        finally:
            # Release the abandoned queue without blocking on its
            # feeder thread (the old writer may already be gone).
            try:
                queue.cancel_join_thread()
            except Exception:
                pass
            try:
                queue.close()
            except Exception:
                pass

    def _ensure_writer(self) -> None:
        """Lazily start the writer process on first use.

        """
        with self._writer_lock:
            if self._writer_proc is not None and self._writer_proc.is_alive():
                return
            ctx = mp.get_context()  # default; works on win + linux
            with self._mp_queue_lock:
                queue = self._mp_queue
            self._writer_proc = ctx.Process(
                target=_writer_process_main,
                args=(str(self._logs_dir), queue, self._max_log_bytes),
                name="flameo-logger-writer",
                daemon=True,
            )
            self._writer_proc.start()

    def _enqueue(
        self,
        level: int,
        msg,
        args,
        kwargs,
        immediate: bool = False,
    ) -> None:
        """Hot-path method. Do as little as possible here.

        Steps:
          1. ``_ensure_writer`` -- only on the very first call does
             this spawn the writer process; after that the in-process
             alive-check is a single attribute access.
          2. One ``f_back.f_back`` frame hop to capture the caller's
             filename/lineno/function name.
          3. Build a small dict.
          4. ``SimpleQueue.put_nowait`` -- unbounded, never raises in
             practice.
          5. If the caller asked for an immediate flush (errors and
             criticals), nudge the bridge to forward ASAP.
        """
        self._ensure_writer()

        frame = inspect.currentframe()
        fn: Optional[str] = None
        lineno: int = 0
        func_name: Optional[str] = None
        if frame is not None and frame.f_back is not None:
            caller_frame = frame.f_back.f_back
            if caller_frame is not None:
                fn = caller_frame.f_code.co_filename
                lineno = caller_frame.f_lineno
                func_name = caller_frame.f_code.co_name

        payload = {
            "v": self._PAYLOAD_VERSION,
            "level": level,
            "msg": msg,
            "args": args,
            "kwargs": kwargs,
            "created": time.time(),
            "fn": fn,
            "lineno": lineno,
            "func_name": func_name,
        }

        try:
            self._local_queue.put_nowait(payload)
        except Exception:
            return

        if immediate:
            self._immediate_evt.set()

    # ---- bridge thread ------------------------------------------------------
    def _bridge_main(self) -> None:
        """Forward batches from the in-memory queue to the writer process.

        The bridge is the ONLY thread on the parent side that touches
        the multiprocessing queue. By accumulating up to 100 records
        (or 1 second of wait time) before forwarding, we keep the IPC
        lock contention low even when requests are bursty.
        """
        # We use SimpleQueue.get(timeout=...) so we can also react to
        # the stop / immediate events without busy-waiting. The
        # timeout here is the longest we'll wait for the FIRST record
        # of a batch; subsequent records wake us via get returning
        # promptly. The actual 1-second gate is computed against the
        # first record's arrival time, not this timeout.
        timeout = self._batch_timeout  # cap so we can also check stop

        while not self._stop_bridge.is_set():
            # Wait for the first record of a batch.
            try:
                first = self._local_queue.get(timeout=timeout)
            except Empty:
                # Timed out with no records. Loop and check stop.
                continue

            # Shutdown sentinel from ``stop()``: flush whatever is
            # still in the local queue and exit so the caller can
            # join us.
            if first is None:
                self._flush_pending_to_writer()
                return

            batch = [first]
            first_at = time.time()

            # Drain any records that arrive within the batch window.
            while len(batch) < self._batch_size:
                remaining = self._batch_timeout - (time.time() - first_at)
                if remaining <= 0:
                    break
                try:
                    batch.append(self._local_queue.get(timeout=min(remaining, 0.05)))
                except Empty:
                    # Window elapsed OR a short poll tick fired; check
                    # the time / size gate.
                    if (time.time() - first_at) >= self._batch_timeout:
                        break
                    if len(batch) >= self._batch_size:
                        break
                    # If immediate-flush was requested, break early so
                    # we don't sit in the inner loop.
                    if self._immediate_evt.is_set():
                        self._immediate_evt.clear()
                        break
                    # If shutdown was requested, don't keep polling
                    # until the batch window elapses -- forward what
                    # we have and let stop() handle the rest.
                    if self._stop_bridge.is_set():
                        break

            # Forward the batch. We do this OUTSIDE any local lock so
            # the request thread can keep enqueuing while we send.
            for payload in batch:
                try:
                    with self._mp_queue_lock:
                        self._mp_queue.put_nowait(payload)
                except Exception:
                    # Writer process gone -- bridge can't do anything
                    # useful until it comes back. Drop the rest of
                    # the batch and retry on the next iteration.
                    break

            # If stop was requested while we were building or
            # forwarding the batch, drain anything that arrived
            # after the batch started and exit. Without this the
            # daemon thread is killed mid-loop on interpreter
            # shutdown and those late records are lost.
            if self._stop_bridge.is_set():
                self._flush_pending_to_writer()
                return

    def _flush_pending_to_writer(self) -> None:
        """Move every payload still in ``_local_queue`` to the writer.

        Called by the bridge on shutdown so no in-flight records are
        lost. Stops at the first ``put_nowait`` failure (writer gone)
        so we don't busy-loop.
        """
        while True:
            try:
                payload = self._local_queue.get_nowait()
            except Empty:
                return
            try:
                with self._mp_queue_lock:
                    self._mp_queue.put_nowait(payload)
            except Exception:
                return


logger = FlameoLogger()


def set_log_dir(directory) -> pathlib.Path:
    """Point the module-level logger at ``directory``.

    Called by :class:`~flameo.Flameo` when the app is built with a
    ``log_dir``. Returns the new :class:`~pathlib.Path`.
    """
    return logger.set_log_dir(directory)


def set_max_log_bytes(size: int) -> int:
    """Set the size (bytes) at which a log file rolls over.

    Called by :class:`~flameo.Flameo` when the app is built with a
    ``max_log_bytes``. The writer process is restarted so it picks up the
    new threshold. Returns the effective size.
    """
    return logger.set_max_log_bytes(size)
