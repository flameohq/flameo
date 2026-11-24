from enum import Enum
import re, asyncio, os, sys, weakref, datetime, decimal, ipaddress, mimetypes, urllib.parse, inspect, warnings, time, shutil, asyncio,urllib.parse, warnings, email.utils, stat
from inspect import signature
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache

from .cycles import RouterMap, Request, Response, RSGIResponse, _drain_finishers
from .endpoint_cache import (
    EndpointCacheSpec,
    MemoryCacheStore,
    _resolve_cache,
    bind_redis_json_helpers,
    cached_invoke,
    warn_uncacheable_method,
)
from .ratelimit import RateLimitSpec
from .validation import is_pydantic_model, validate_model, validation_error_payload
from .models import bind_to_instance, bind_to_list

from .parser import set_temp_dir
from .logger import set_log_dir, set_max_log_bytes
from .cleanup import TempSweeper, _DEFAULT_INTERVAL, _DEFAULT_THRESHOLD
from .sessions import make_session_middleware
from .cors import make_cors_middleware
from .templates import build_environment, RouteTemplateEnv
from .docs import (
    DocsRegistry,
    RouteDoc,
    normalize_docs_path,
    register_docs_routes,
)
from pathlib import Path
from typing import List, Optional

_db_driver = None
_db_type = None
_python_to_oracle_type = {}
_oracle_cursor_type = None
_max_body_size = 10 * 1024 * 1024  # 10 MiB
_COMPRESSION_TO_THREAD = 64 * 1024
_STATIC_CACHE_CONTROL = b"public, max-age=0, must-revalidate"
_CANCEL_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)

version__ = ""


@lru_cache(maxsize=2048)
def _static_file_metadata(path: str, size: int, mtime_ns: int):
    """Cache MIME/validator/header metadata until a file's stat changes."""
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "text/plain; charset=utf-8"
    basename = os.path.basename(path) or "file"
    ascii_fallback = basename.encode("ascii", "replace").decode("ascii")
    encoded_star = urllib.parse.quote(basename, safe="")
    disposition = (
        f'inline; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded_star}"
    ).encode("latin-1")
    etag = f'W/"{size:x}-{mtime_ns:x}"'.encode("ascii")
    last_modified = email.utils.formatdate(mtime_ns / 1_000_000_000, usegmt=True).encode("ascii")
    return mime_type.encode(), disposition, etag, last_modified


def _static_request_not_modified(req, etag: bytes, mtime_ns: int) -> bool:
    headers = req.headers
    if_none_match = headers.get("if-none-match")
    if if_none_match:
        expected = etag.decode("ascii").removeprefix("W/")
        for candidate in if_none_match.split(","):
            candidate = candidate.strip()
            if candidate == "*" or candidate.removeprefix("W/") == expected:
                return True
        # If-None-Match takes precedence over If-Modified-Since, including
        # when none of its entity tags match.
        return False

    if_modified_since = headers.get("if-modified-since")
    if not if_modified_since:
        return False
    try:
        modified_at = email.utils.parsedate_to_datetime(if_modified_since)
        if modified_at.tzinfo is None:
            modified_at = modified_at.replace(tzinfo=datetime.timezone.utc)
        return int(mtime_ns / 1_000_000_000) <= int(modified_at.timestamp())
    except (TypeError, ValueError, OverflowError):
        return False

def _noop():
    return None


class _State:
    __slots__ = ("_store",)

    def __init__(self, store: dict):
        self._store = store

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._store.get(name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_store":
            super().__setattr__(name, value)
            return
        self._store[name] = value

    def __delattr__(self, name: str) -> None:
        if name == "_store":
            super().__delattr__(name)
            return
        try:
            del self._store[name]
        except KeyError:
            raise AttributeError(f"[flameo] app state has no attribute {name!r}")

    def __iter__(self):
        return iter(self._store)

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, name: str) -> bool:
        return name in self._store

    def __repr__(self) -> str:
        return f"State({self._store!r})"

    def get(self, name: str, default=None):
        return self._store.get(name, default)

    def setdefault(self, name: str, default=None):
        return self._store.setdefault(name, default)

    def pop(self, name: str, *args):
        return self._store.pop(name, *args)

    def update(self, *args, **kwargs):
        self._store.update(*args, **kwargs)

    def clear(self):
        self._store.clear()

    def keys(self):
        return self._store.keys()

    def values(self):
        return self._store.values()

    def items(self):
        return self._store.items()

    def to_dict(self) -> dict:
        return dict(self._store)


_META_MISSING = object()

_ALLOWED_INJECT = frozenset(("db", "cache", "app", "state"))
_INJECT_HELP = (
    "(req, res), (req, res, db), (req, res, cache), (req, res, app), "
    "(req, res, state), (req, res, db, cache), (req, res, db, app), "
    "(req, res, db, state), (req, res, cache, app), (req, res, cache, state), "
    "or (req, res, db, cache, app/state)"
)


def _inject_flags(params, kind):
    param_count = len(params)
    if param_count > 5:
        raise ValueError(
            "[flameo] Flameo %ss accept at most 5 parameters: %s. Got %d."
            % (kind, _INJECT_HELP, param_count)
        )
    takes_qm = False
    takes_cache = False
    takes_app = False
    takes_state = False
    seen = set()
    for param in params[2:]:
        nm = param.name
        if nm not in _ALLOWED_INJECT:
            raise ValueError(
                "[flameo] After request and response, extra %s parameters must "
                "be named 'db', 'cache', 'app', or 'state'. Got %r." % (kind, nm)
            )
        if nm in seen:
            raise ValueError(
                "[flameo] Duplicate parameter name %r is not allowed." % nm
            )
        seen.add(nm)
        if nm == "db":
            takes_qm = True
        elif nm == "cache":
            takes_cache = True
        elif nm == "app":
            takes_app = True
        else:
            takes_state = True
    if takes_app and takes_state:
        raise ValueError("[flameo] Parameters named app and state cannot co-exist")
    return takes_qm, takes_cache, takes_app, takes_state


def _handler_meta(handler):
    
    cache = _HANDLER_META
    if handler is None:
        return None
    
    key = (id(handler), id(handler.__code__))
    cached = cache.get(key, _META_MISSING)
    if cached is not _META_MISSING:
        return cached
    sig = signature(handler)
    params = list(sig.parameters.values())
    param_count = len(params)
    takes_qm, takes_cache, takes_app, takes_state = _inject_flags(params, "handler")
    first_param = params[0] if params else None
    annotation = first_param.annotation if first_param is not None else None
    is_pydantic = annotation is not None and is_pydantic_model(annotation)
    meta = (param_count, annotation, takes_qm, is_pydantic, takes_app, takes_state,
            takes_cache, [p.name for p in params[2:]])
    if len(cache) > 4096:
        cache.pop(next(iter(cache)))
    cache[key] = meta
    return meta


def _mw_meta(mw):
    
    cache = _MIDDLEWARE_META
    if mw is None:
        return None
    
    key = (id(mw), id(mw.__code__))
    cached = cache.get(key, _META_MISSING)
    if cached is not _META_MISSING:
        return cached
    sig = signature(mw)
    params = list(sig.parameters.values())
    param_count = len(params)
    takes_qm = _inject_flags(params, "middleware")[0]
    meta = (param_count, takes_qm, [p.name for p in params[2:]])
    if len(cache) > 4096:
        cache.pop(next(iter(cache)))
    cache[key] = meta
    return meta


async def _call_middleware(mw, meta, req, res, *, qm, app, state, cache=None):
    slot_names = meta[_MW_SLOT_NAMES]
    if not slot_names:
        result = mw(req, res)
    else:
        values = {"db": qm, "cache": cache, "app": app, "state": state}
        result = mw(req, res, *(values[n] for n in slot_names))
    if inspect.isawaitable(result):
        await result


async def _acquire_request_qm(req, pool):
    """Lazily acquire one QueryManager and retain its pool lease per request."""
    qm = req._db_query_manager
    if qm is not None:
        if pool is not None and req._db_pool is not pool:
            raise RuntimeError(
                "A request cannot use multiple database pools; global middleware, "
                "route middleware, and the handler must share one app pool."
            )
        return qm
    if pool is None:
        return None

    if _db_type == DBType.MongoDB:
        handle = pool.request_handle() if hasattr(pool, "request_handle") else pool
        qm = QueryManager(handle)
        qm._mongo_client = (
            getattr(pool, "client", None)
            if hasattr(pool, "request_handle")
            else None
        )
        req._db_pool = pool
        req._db_query_manager = qm
        return qm

    acquire_context = pool.acquire()
    raw_conn = await acquire_context.__aenter__()
    try:
        qm = QueryManager(raw_conn)
    except BaseException:
        await acquire_context.__aexit__(*sys.exc_info())
        raise
    req._db_pool = pool
    req._db_acquire_context = acquire_context
    req._db_query_manager = qm
    return qm


async def _release_request_qm(req, exc=None):
    """Close the request's QueryManager and return its connection to the pool."""
    qm = req._db_query_manager
    if _db_type == DBType.MongoDB:
        req._db_pool = None
        req._db_acquire_context = None
        req._db_query_manager = None
        if qm is not None:
            await qm.close()
        return
    acquire_context = req._db_acquire_context
    if acquire_context is None:
        return
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else (None, None, None)
    req._db_pool = None
    req._db_acquire_context = None
    req._db_query_manager = None
    try:
        if qm is not None:
            await qm.close()
    finally:
        await acquire_context.__aexit__(*exc_info)


async def _run_middlewares(mws, mw_metas, req, res, *, qm, app, state, cache=None):
    if qm is None and any(m[_MW_TAKES_QM] for m in mw_metas):
        qm = await _acquire_request_qm(req, getattr(app, "DB_POOL", None))
    elif qm is None:
        qm = req._db_query_manager
    for mw, meta in zip(mws, mw_metas):
        await _call_middleware(mw, meta, req, res, qm=qm, cache=cache,
                               app=app, state=state)


def _make_dispatch(app, handler, mws, meta, cache_spec=None, limit_spec=None,
                   route_method=None, route_path=None):
    takes_qm = meta[_META_TAKES_QM]
    slot_names = meta[_META_SLOT_NAMES]
    # Preserves legacy behavior: a db+state handler answers 503 when no
    # pool exists; db-only / db+app handlers get ``db=None`` instead.
    no_db_503 = takes_qm and meta[_META_TAKES_STATE]
    mw_metas = [_mw_meta(mw) for mw in mws] if mws else []

    if not mws and not slot_names and cache_spec is None and limit_spec is None:
        return handler

    if cache_spec is None:
        async def dispatch(req, res):
            host = app or req.app
            if limit_spec is not None and not await limit_spec.check(
                    req, res, host, route_method, route_path):
                return
            qm = await _acquire_request_qm(req, host.DB_POOL if takes_qm else None)
            if qm is None and no_db_503:
                await res.send("Database not available", status=503)
                return
            if mw_metas:
                await _run_middlewares(mws, mw_metas, req, res, qm=qm,
                                       cache=_resolve_cache(host), app=host,
                                       state=host.state)
            if not slot_names:
                return await handler(req, res)
            values = {"db": qm, "cache": _resolve_cache(host),
                      "app": host, "state": host.state}
            return await handler(req, res, *(values[n] for n in slot_names))
        return dispatch

    async def dispatch(req, res):
        host = app or req.app
        if limit_spec is not None and not await limit_spec.check(
                req, res, host, route_method, route_path):
            return
        qm = await _acquire_request_qm(req, host.DB_POOL if takes_qm else None)
        if qm is None and no_db_503:
            await res.send("Database not available", status=503)
            return
        hcache, scache = cache_spec.resolve(host)
        if mw_metas:
            await _run_middlewares(mws, mw_metas, req, res, qm=qm,
                                   cache=hcache, app=host, state=host.state)
        key = cache_spec.key_for(req)
        if not slot_names:
            return await cached_invoke(
                cache_spec, scache, key, res, lambda: handler(req, res))
        values = {"db": qm, "cache": hcache, "app": host, "state": host.state}
        return await cached_invoke(
            cache_spec, scache, key, res,
            lambda: handler(req, res, *(values[n] for n in slot_names)))
    return dispatch


def _make_dispatch_pydantic(app, handler, mws, meta, cache_spec=None,
                            limit_spec=None, route_method=None, route_path=None):
    takes_qm = meta[_META_TAKES_QM]
    slot_names = meta[_META_SLOT_NAMES]
    annotation = meta[_META_ANNOTATION]
    mw_metas = [_mw_meta(mw) for mw in mws] if mws else []

    async def dispatch(req, res):
        host = app or req.app
        if limit_spec is not None and not await limit_spec.check(
                req, res, host, route_method, route_path):
            return
        instance = await _validate_pydantic(
            req, res, annotation,
            validate_case_sensitive=getattr(host, "validate_case_sensitive", True),
        )
        if instance is None:
            return None
        qm = None
        if takes_qm:
            qm = await _acquire_request_qm(req, host.DB_POOL)
            if qm is None:
                await res.send("Database not available", status=503)
                return None
        hcache, scache = (
            cache_spec.resolve(host) if cache_spec is not None
            else (_resolve_cache(host), None)
        )
        if mw_metas:
            await _run_middlewares(mws, mw_metas, req, res, qm=qm,
                                   cache=hcache, app=host, state=host.state)
        if cache_spec is not None:
            key = cache_spec.key_for(req)
            if not slot_names:
                return await cached_invoke(
                    cache_spec, scache, key, res,
                    lambda: handler(instance, res))
            values = {"db": qm, "cache": hcache, "app": host,
                      "state": host.state}
            return await cached_invoke(
                cache_spec, scache, key, res,
                lambda: handler(instance, res, *(values[n] for n in slot_names)))
        if not slot_names:
            return await handler(instance, res)
        values = {"db": qm, "cache": hcache, "app": host, "state": host.state}
        return await handler(instance, res, *(values[n] for n in slot_names))
    return dispatch


async def _validate_pydantic(req, res, annotation, validate_case_sensitive=None):
    """Body-load + pydantic validation.

    Returns the validated instance, or ``None`` if a 422 was already
    written to ``res``.
    """
    if validate_case_sensitive is None:
        app_ref = getattr(req, "app", None)
        validate_case_sensitive = getattr(app_ref, "validate_case_sensitive", True)
    try:
        body_data = await req.body
    except Exception as parse_exc:
        await res.json(
            validation_error_payload([{
                "type": "json_invalid",
                "loc": ["body"],
                "msg": f"Could not parse request body: {parse_exc}",
            }]),
            status=422,
        )
        return None
    
    files_data = None
    try:
        content_type = (req.headers.get("content-type", "") or "").lower()
    except Exception:
        content_type = ""
    if "multipart/form-data" in content_type:
        try:
            files_data = await req.files
        except Exception:
            files_data = None
    instance, errors = validate_model(
        annotation, body_data, files=files_data,
        validate_case_sensitive=validate_case_sensitive,
    )
    if errors:
        await res.json(validation_error_payload(errors), status=422)
        return None
    return instance


def _wrap_route(app, handler, mws, meta, cache_spec=None, limit_spec=None,
                route_method=None, route_path=None):
    if meta[_META_IS_PYDANTIC]:
        return _make_dispatch_pydantic(app, handler, mws, meta,
                                       cache_spec=cache_spec,
                                       limit_spec=limit_spec,
                                       route_method=route_method,
                                       route_path=route_path)
    return _make_dispatch(app, handler, mws, meta, cache_spec=cache_spec,
                          limit_spec=limit_spec, route_method=route_method,
                          route_path=route_path)


def _make_router_middleware(prefix, mw):
    meta = _mw_meta(mw)
    takes_qm = meta[_MW_TAKES_QM]

    async def scoped(req, res):
        if prefix != "/" and req.path != prefix and not req.path.startswith(prefix + "/"):
            return
        if prefix != "/":
            req._base_url = prefix
        app_ref = req.app
        state_ref = app_ref.state
        qm = await _acquire_request_qm(
            req, app_ref.DB_POOL if takes_qm else None,
        )
        await _call_middleware(mw, meta, req, res,
                               qm=qm, cache=_resolve_cache(app_ref),
                               app=app_ref, state=state_ref)

    return scoped


def _with_route_env(dispatch, route_env):
    

    async def dispatch_with_env(req, res):
        req._template_env = route_env
        return await dispatch(req, res)

    return dispatch_with_env


def _with_base_url(dispatch, prefix):
    async def dispatch_with_base(req, res):
        req._base_url = prefix
        return await dispatch(req, res)

    return dispatch_with_base


def _is_dynamic_path(path):
    # Keep this identical to RouterMap.add_route's dynamic-path test.
    return ":" in path or "{" in path


def _stash_route_reg(handler, **facts):
    """Remember route registrations for decorators applied afterward."""
    facts["handler"] = handler
    regs = getattr(handler, "__flameo_route_reg__", None)
    if regs is None:
        regs = []
        try:
            handler.__flameo_route_reg__ = regs
        except (AttributeError, TypeError):
            return
    regs.append(facts)


def _apply_route_reg(reg):
    """Rebuild a route dispatch after a route decorator is applied."""
    router = reg["router"]
    if router is not None and getattr(router, "_flameo_mounted__", False):
        decorator_name = (
            "limit" if getattr(reg["handler"], "__flameo_limit_spec__", None)
            is not None else "cache"
        )
        raise ValueError(
            f"[flameo] @router.{decorator_name} must be applied before "
            "app.use_router() mounts the router"
        )

    method = reg["method"]
    method_key = method.upper()
    handler = reg["handler"]
    cache_spec = getattr(handler, "__flameo_cache_spec__", None)
    limit_spec = getattr(handler, "__flameo_limit_spec__", None)
    if cache_spec is not None and method_key not in ("GET", "HEAD"):
        warn_uncacheable_method(stacklevel=4)

    dispatch = _wrap_route(
        reg["owner"], handler, reg["mws"], reg["meta"],
        cache_spec=cache_spec, limit_spec=limit_spec,
        route_method=method, route_path=reg["path"],
    )
    if reg["route_env"] is not None:
        dispatch = _with_route_env(dispatch, reg["route_env"])

    router_map = reg["router_map"]
    path = reg["path"]
    dynamic_index = reg["dynamic_index"]
    if dynamic_index is None:
        router_map.static_routes[method_key][path] = (dispatch, reg["mws"])
    else:
        entry = router_map.dynamic_routes[method_key][dynamic_index]
        router_map.dynamic_routes[method_key][dynamic_index] = (
            entry[0], entry[1], dispatch, entry[3],
        )
    router_map._cache.clear()

    routes_slot = reg["routes_slot"]
    if routes_slot is not None:
        routes_list, index = routes_slot
        routes_list[index] = (method, path, dispatch, reg["mws"])
    return dispatch


def _cache_decorator(owner, router, ttl, kind):
    if callable(ttl):
        raise TypeError(
            f"[flameo] @{kind}.cache requires a ttl in seconds, e.g. "
            f"@{kind}.cache(ttl=60). Place it above the route decorator."
        )
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise ValueError(
            f"[flameo] @{kind}.cache ttl must be a positive number of seconds"
        )

    def decorate(handler):
        spec = EndpointCacheSpec(ttl)
        try:
            handler.__flameo_cache_spec__ = spec
        except (AttributeError, TypeError):
            raise TypeError(f"[flameo] @{kind}.cache expects a function handler")

        regs = list(getattr(handler, "__flameo_route_reg__", ()) or ())
        matching = [reg for reg in regs
                    if reg["router"] is router and reg["owner"] is owner]
        if regs and not matching:
            warnings.warn(
                f"[flameo] @{kind}.cache was applied to a handler registered "
                "on a different app/router",
                UserWarning,
                stacklevel=2,
            )
        for reg in matching:
            _apply_route_reg(reg)
        return handler

    return decorate


def _limit_decorator(owner, router, count, seconds, key_function, kind):
    spec = RateLimitSpec(count, seconds, key_function)

    def decorate(handler):
        try:
            handler.__flameo_limit_spec__ = spec
        except (AttributeError, TypeError):
            raise TypeError(f"[flameo] @{kind}.limit expects a function handler")

        regs = list(getattr(handler, "__flameo_route_reg__", ()) or ())
        matching = [reg for reg in regs
                    if reg["router"] is router and reg["owner"] is owner]
        if regs and not matching:
            warnings.warn(
                f"[flameo] @{kind}.limit was applied to a handler registered "
                "on a different app/router",
                UserWarning,
                stacklevel=2,
            )
        for reg in matching:
            _apply_route_reg(reg)
        return handler

    return decorate


def _warn_if_uncacheable(method):
    if method.upper() not in ("GET", "HEAD"):
        warn_uncacheable_method(stacklevel=4)


_META_PARAM_COUNT = 0
_META_ANNOTATION = 1
_META_TAKES_QM = 2
_META_IS_PYDANTIC = 3
_META_TAKES_APP = 4
_META_TAKES_STATE = 5
_META_TAKES_CACHE = 6
_META_SLOT_NAMES = 7

_MW_PARAM_COUNT = 0
_MW_TAKES_QM = 1
_MW_SLOT_NAMES = 2


_HANDLER_META: dict = {}
_MIDDLEWARE_META: dict = {}

if sys.platform == "win32":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
else:
    import uvloop
    loop = uvloop.new_event_loop()
    asyncio.set_event_loop(loop)

_LOG_SEMAPHORE_LIMIT = 64
_log_semaphore: "asyncio.Semaphore | None" = None


def _get_log_semaphore() -> "asyncio.Semaphore":
    global _log_semaphore
    if _log_semaphore is None:
        
        _log_semaphore = asyncio.Semaphore(_LOG_SEMAPHORE_LIMIT)
    return _log_semaphore

class CPUExecutor:
    def __init__(self, max_workers=None):
        self.executor = ProcessPoolExecutor(max_workers=max_workers or os.cpu_count())

    async def run(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, func, *args)

class _SQLitePool:
    def __init__(self, file_path):
        self._file_path = file_path

    def acquire(self):
        return _SQLiteAcquireContext(self._file_path)

    async def close(self):
        return


class _SQLiteAcquireContext:
    """Per-checkout SQLite connection context (safe across concurrent requests)."""

    __slots__ = ("_file_path", "_conn")

    def __init__(self, file_path):
        self._file_path = file_path
        self._conn = None

    async def __aenter__(self):
        import aiosqlite

        self._conn = await aiosqlite.connect(self._file_path, isolation_level=None)
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        return False


class _MongoPool:
    __slots__ = ("client", "database")

    def __init__(self, client, database):
        self.client = client
        self.database = database

    def request_handle(self):
        if isinstance(self.database, str):
            return self.client[self.database]
        return self.client

    async def close(self):
        await self.client.close()


class FlameoError(Exception):
    """Base class for errors raised by Flameo."""

    status = 500
    code = "flameo_error"

    def __init__(self, message=None, *, status=None, code=None, details=None):
        self.message = message or "A Flameo error occurred"
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.details = details
        super().__init__(self.message)

    def __str__(self):
        return self.message


class TransactionError(FlameoError):
    """Raised when a database transaction fails."""

    status = 500
    code = "transaction_failed"

    def __init__(self, cause=None, *, rolled_back=False, rollback_error=None,
                 message=None):
        self.cause = cause
        self.rolled_back = rolled_back
        self.rollback_error = rollback_error
        if isinstance(cause, FlameoError):
            status = cause.status
            code = cause.code
            details = cause.details
        else:
            status = 500
            code = "transaction_failed"
            details = None
        if message is None:
            reason = str(cause) if cause is not None else "unknown error"
            if rollback_error is not None:
                message = (
                    f"[flameo] Database transaction failed and rollback also "
                    f"failed: {reason} (rollback error: {rollback_error})"
                )
            elif rolled_back:
                message = (
                    f"[flameo] Database transaction failed and was rolled "
                    f"back: {reason}"
                )
            else:
                message = (
                    f"[flameo] Database transaction failed and could not be "
                    f"rolled back: {reason}"
                )
        super().__init__(message, status=status, code=code, details=details)


_PG_NAMED_BIND = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_PROC_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*\Z")

_MONGO_ALIASES = {
    "min_size": "minPoolSize",
    "max_size": "maxPoolSize",
    "max_connecting": "maxConnecting",
    "server_selection_timeout": "serverSelectionTimeoutMS",
    "connect_timeout": "connectTimeoutMS",
    "wait_queue": "waitQueueTimeoutMS",
}
_MONGO_DEFAULTS = {
    "minPoolSize": 10,
    "maxPoolSize": 100,
    "maxConnecting": 5,
    "serverSelectionTimeoutMS": 5000,
    "connectTimeoutMS": 10000,
    "waitQueueTimeoutMS": 5000,
}


def _is_mongo_collection(value):
    return value.__class__.__name__.endswith("Collection")


def _is_mongo_database(value):
    return value.__class__.__name__.endswith("Database")


def _mongo_client_options(config):
    options = {
        key: value
        for key, value in config.items()
        if key not in {"db_type", "mongo_uri", "db", *_MONGO_ALIASES}
    }
    for alias, canonical in _MONGO_ALIASES.items():
        if alias in config and canonical in options:
            raise ValueError(
                f"[flameo] db_config cannot set both {alias!r} and "
                f"{canonical!r}"
            )
        if canonical not in options:
            options[canonical] = config.get(alias, _MONGO_DEFAULTS[canonical])
    return options


def _pg_binds(sql, params):
    """Return (query, args) using asyncpg's positional ``$n`` placeholders.

    When ``params`` is a dict, ``:name`` tokens in ``sql`` are rewritten to
    ``$n`` (numbered by first appearance, reused for repeated names) and the
    matching values are collected into a positional tuple. Extra dict keys
    are ignored. Quoted strings, comments and ``::`` casts are left intact.
    """
    if not isinstance(params, dict):
        return sql, tuple(params or ())
    if not params:
        return sql, ()

    names = {}
    args = []
    out = []
    i = 0
    size = len(sql)
    while i < size:
        char = sql[i]
        if char in ("'", '"'):
            quote = char
            start = i
            i += 1
            while i < size:
                if sql[i] == "\\" and quote == "'":
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < size and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(sql[start:i])
            continue
        if sql.startswith("--", i) or sql.startswith("/*", i):
            closer = "\n" if sql.startswith("--", i) else "*/"
            end = sql.find(closer, i + 2)
            if end < 0:
                out.append(sql[i:])
                break
            end = end if closer == "\n" else end + 2
            out.append(sql[i:end])
            i = end
            continue
        if char == ":":
            if i + 1 < size and sql[i + 1] == ":":
                out.append("::")
                i += 2
                continue
            match = _PG_NAMED_BIND.match(sql, i)
            if match:
                name = match.group(1)
                if name not in names:
                    if name not in params:
                        raise ValueError(
                            f"[flameo] Missing value for named parameter :{name}"
                        )
                    names[name] = len(args) + 1
                    args.append(params[name])
                out.append(f"${names[name]}")
                i = match.end()
                continue
        if char == "$" and i + 1 < size and sql[i + 1].isdigit():
            raise ValueError(
                "[flameo] Use a tuple/list with positional ($1) placeholders "
                "or a dict with named (:name) placeholders"
            )
        out.append(char)
        i += 1
    return "".join(out), tuple(args)


def _row_columns(cur):
    if cur.description is None:
        raise ValueError(
            "[flameo] fetch()/fetchrow() only work with statements that return "
            "rows; use execute() for INSERT/UPDATE/DELETE"
        )
    return [col[0] for col in cur.description]


def _bind_row(row, model):
    if row is None or model is None:
        return row
    return bind_to_instance(row, model)


def _bind_rows(rows, model):
    if not rows:
        return None if model is not None else []
    return bind_to_list(rows, model) if model is not None else rows


class QueryManager:
    __slots__ = ("_raw_conn", "_closed", "_transaction", "_tx_depth",
                 "_cursor", "_column_cache", "_session", "_bind_cm",
                 "_mongo_client")

    def __init__(self, raw_conn):
        self._raw_conn = raw_conn
        self._closed = False
        self._transaction = None
        self._tx_depth = 0
        self._cursor = None
        self._column_cache = {}
        self._session = None
        self._bind_cm = None
        self._mongo_client = None

    class TransactionContext:
        def __init__(self, qm: "QueryManager"):
            self.qm = qm

        async def __aenter__(self):
            qm = self.qm
            try:
                await qm.begin_transaction()
            except BaseException as exc:
                had_transaction = qm._transaction is not None
                rolled_back, rollback_error = (
                    await self._rollback(qm) if had_transaction else (False, None)
                )
                if isinstance(exc, _CANCEL_EXCEPTIONS):
                    raise
                raise TransactionError(
                    exc, rolled_back=rolled_back,
                    rollback_error=rollback_error,
                ) from exc
            return qm

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            qm = self.qm
            if exc_type is not None:
                rolled_back, rollback_error = await self._rollback(qm)
                if isinstance(exc_val, _CANCEL_EXCEPTIONS):
                    return False
                if isinstance(exc_val, TransactionError):
                    if rollback_error is not None:
                        exc_val.rolled_back = False
                        exc_val.rollback_error = rollback_error
                    return False
                raise TransactionError(
                    exc_val, rolled_back=rolled_back,
                    rollback_error=rollback_error,
                ) from exc_val
            try:
                await qm.commit_transaction()
            except BaseException as exc:
                rolled_back, rollback_error = await self._rollback(qm)
                if isinstance(exc, _CANCEL_EXCEPTIONS):
                    raise
                raise TransactionError(
                    exc, rolled_back=rolled_back,
                    rollback_error=rollback_error,
                ) from exc
            return False

        async def _rollback(self, qm):
            had_transaction = qm._transaction is not None
            try:
                await qm.rollback_transaction()
            except Exception as exc:
                qm._transaction = None
                qm._tx_depth = 0
                return False, exc
            return had_transaction, None

    def transaction(self):
        return QueryManager.TransactionContext(self)

    async def _unbind_mongo(self):
        bind_cm = self._bind_cm
        self._bind_cm = None
        if bind_cm is not None:
            try:
                await bind_cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def _end_mongo_session(self, abort=False):
        await self._unbind_mongo()
        session = self._session
        self._session = None
        self._transaction = None
        self._tx_depth = 0
        if session is None:
            return
        try:
            if abort:
                await session.abort_transaction()
        finally:
            await session.end_session()

    def _mongo_session_client(self):
        if self._mongo_client is not None:
            return self._mongo_client
        if self._raw_conn.__class__.__name__.endswith("Client"):
            return self._raw_conn
        return self._raw_conn.client

    async def begin_transaction(self):
        if self._transaction is not None:
            self._tx_depth += 1
            return
        if _db_type == DBType.MongoDB:
            client = self._mongo_session_client()
            session = client.start_session()
            self._session = session
            try:
                self._transaction = await session.start_transaction()
                bind_cm = session.bind(end_session=False)
                self._bind_cm = bind_cm
                await bind_cm.__aenter__()
            except BaseException:
                try:
                    await self._end_mongo_session()
                except Exception:
                    pass
                raise
            return
        if _db_type == DBType.PostgreSQL:
            self._transaction = self._raw_conn.transaction()
            await self._transaction.start()
        elif _db_type == DBType.MySQL:
            await self._raw_conn.begin()
            self._transaction = True
        elif _db_type == DBType.Oracle:
            self._transaction = True
        elif _db_type == DBType.SQLite:
            await self._raw_conn.execute("BEGIN")
            self._transaction = True

    async def commit_transaction(self):
        if self._transaction:
            if self._tx_depth > 0:
                self._tx_depth -= 1
                return
            if _db_type == DBType.MongoDB:
                await self._unbind_mongo()
                session = self._session
                self._session = None
                self._transaction = None
                try:
                    if session is not None:
                        await session.commit_transaction()
                finally:
                    if session is not None:
                        await session.end_session()
                return
            if _db_type == DBType.PostgreSQL:
                await self._transaction.commit()
            elif _db_type == DBType.MySQL:
                await self._raw_conn.commit()
            elif _db_type == DBType.Oracle:
                await self._raw_conn.commit()
            elif _db_type == DBType.SQLite:
                await self._raw_conn.commit()
            self._transaction = None

    async def rollback_transaction(self):
        if self._transaction:
            if self._tx_depth > 0:
                self._tx_depth -= 1
                return
            if _db_type == DBType.MongoDB:
                await self._unbind_mongo()
                session = self._session
                self._session = None
                self._transaction = None
                try:
                    if session is not None:
                        await session.abort_transaction()
                finally:
                    if session is not None:
                        await session.end_session()
                return
            if _db_type == DBType.PostgreSQL:
                await self._transaction.rollback()
            elif _db_type == DBType.MySQL:
                await self._raw_conn.rollback()
            elif _db_type == DBType.Oracle:
                await self._raw_conn.rollback()
            elif _db_type == DBType.SQLite:
                await self._raw_conn.rollback()
            self._transaction = None

    def _get_cursor(self):
        if _db_type == DBType.Oracle:
            if self._cursor is None:
                self._cursor = self._raw_conn.cursor()
            return self._cursor
        return None

    async def _read_ref_cursor(self, cursor_var):
        refcur = cursor_var.getvalue()
        if refcur is None:
            return []
        columns = [col[0] for col in refcur.description]
        fetch_result = refcur.fetchall()
        fetched = await fetch_result if inspect.isawaitable(fetch_result) else fetch_result
        close_result = refcur.close()
        if inspect.isawaitable(close_result):
            await close_result
        return [dict(zip(columns, row)) for row in fetched]

    async def fetchrow(self, sql, params=None, model=None):
        if _db_type == DBType.MongoDB:
            collection = self._raw_conn[sql]
            if not _is_mongo_collection(collection):
                raise ValueError(
                    "[flameo] fetchrow() requires a database handle; use "
                    "db[db_name].fetchrow() when db_config['db'] is a list"
                )
            doc = await collection.find_one(params or {})
            return _bind_row(dict(doc) if doc is not None else None, model)
        if _db_type == DBType.PostgreSQL:
            sql, args = _pg_binds(sql, params)
            row = await self._raw_conn.fetchrow(sql, *args)
            return _bind_row(dict(row) if row else None, model)
        elif _db_type == DBType.MySQL:
            async with self._raw_conn.cursor() as cur:
                await cur.execute(sql, params or ())
                columns = _row_columns(cur)
                row = await cur.fetchone()
                if not row:
                    return None
                return _bind_row(dict(zip(columns, row)), model)
        elif _db_type == DBType.Oracle:
            cur = self._get_cursor()
            await cur.execute(sql, params or ())
            if sql not in self._column_cache:
                self._column_cache[sql] = _row_columns(cur)
            columns = self._column_cache[sql]
            row = await cur.fetchone()
            if not row:
                return None
            return _bind_row(dict(zip(columns, row)), model)
        elif _db_type == DBType.SQLite:
            cur = await self._raw_conn.execute(sql, params or ())
            columns = _row_columns(cur)
            row = await cur.fetchone()
            if not row:
                return None
            return _bind_row(dict(zip(columns, row)), model)

    fetchOne = fetchrow
    findOne = fetchrow
    fetchRow = fetchrow

    async def fetch(self, sql, params=None, batch_size=None, model=None):
        """Return all rows as dicts, or as ``model`` instances when given.

        ``batch_size`` controls driver-side batching on MySQL, Oracle and
        SQLite; PostgreSQL ignores it because asyncpg buffers the full result.
        With ``model``, an empty result is ``None`` instead of a list.
        """
        if batch_size is not None and not isinstance(batch_size, int):
            raise TypeError("If 'batch_size' is provided, it must be an integer")
        if batch_size is not None and batch_size <= 0:
            raise ValueError("If 'batch_size' is provided, it must be a positive integer")
        if _db_type == DBType.MongoDB:
            collection = self._raw_conn[sql]
            if not _is_mongo_collection(collection):
                raise ValueError(
                    "[flameo] fetch() requires a database handle; use "
                    "db[db_name].fetch() when db_config['db'] is a list"
                )
            cursor = collection.find(params or {})
            docs = await cursor.to_list(length=batch_size)
            return _bind_rows([dict(doc) for doc in docs], model)
        if batch_size is None:
            batch_size = 1000
        # No try/except here on purpose: DB errors must propagate to the
        # dispatcher, which answers 500, releases the pooled connection
        # with the exception (triggering rollback), and logs the
        # traceback in debug mode. Swallowing them used to turn every
        # failure into a silent ``None``.
        if _db_type == DBType.PostgreSQL:
            sql, args = _pg_binds(sql, params)
            rows = await self._raw_conn.fetch(sql, *args)
            return _bind_rows([dict(r) for r in rows], model)
        elif _db_type == DBType.MySQL:
            async with self._raw_conn.cursor() as cur:
                await cur.execute(sql, params or ())
                columns = _row_columns(cur)
                result = []
                while True:
                    batch = await cur.fetchmany(batch_size)
                    if not batch:
                        break
                    result.extend(dict(zip(columns, row)) for row in batch)
                return _bind_rows(result, model)
        elif _db_type == DBType.Oracle:
            cur = self._get_cursor()
            await cur.execute(sql, params or ())
            if sql not in self._column_cache:
                self._column_cache[sql] = _row_columns(cur)
            columns = self._column_cache[sql]
            result = []
            while True:
                batch = await cur.fetchmany(batch_size)
                if not batch:
                    break
                result.extend(dict(zip(columns, row)) for row in batch)
            return _bind_rows(result, model)
        elif _db_type == DBType.SQLite:
            cur = await self._raw_conn.execute(sql, params or ())
            columns = _row_columns(cur)
            result = []
            while True:
                batch = await cur.fetchmany(batch_size)
                if not batch:
                    break
                result.extend(dict(zip(columns, row)) for row in batch)
            return _bind_rows(result, model)

    fetchMany = fetch
    findMany = fetch
    findAll = fetch
    fetchAll = fetch

    async def execute(self, sql, params=None):
        if _db_type == DBType.PostgreSQL:
            sql, args = _pg_binds(sql, params)
            status = await self._raw_conn.execute(sql, *args)
            parts = (status or "").split()
            return int(parts[-1]) if parts and parts[-1].lstrip("+-").isdigit() else 0
        elif _db_type == DBType.MySQL:
            async with self._raw_conn.cursor() as cur:
                await cur.execute(sql, params or ())
                rowcount = cur.rowcount
                if not self._transaction:
                    await self._raw_conn.commit()
                return rowcount
        elif _db_type == DBType.Oracle:
            cur = self._get_cursor()
            await cur.execute(sql, params or ())
            if not self._transaction:
                await self._raw_conn.commit()
            return cur.rowcount
        elif _db_type == DBType.SQLite:
            cur = await self._raw_conn.execute(sql, params or ())
            if not self._transaction:
                await self._raw_conn.commit()
            return cur.rowcount

    async def executemany(self, sql, param_list):
        if _db_type == DBType.PostgreSQL:
            await self._raw_conn.executemany(sql, param_list)
            return len(param_list)
        elif _db_type == DBType.MySQL:
            async with self._raw_conn.cursor() as cur:
                await cur.executemany(sql, param_list)
                if not self._transaction:
                    await self._raw_conn.commit()
            return len(param_list)
        elif _db_type == DBType.Oracle:
            cur = self._get_cursor()
            await cur.executemany(sql, param_list)
            if not self._transaction:
                await self._raw_conn.commit()
            return len(param_list)
        elif _db_type == DBType.SQLite:
            await self._raw_conn.executemany(sql, param_list)
            if not self._transaction:
                await self._raw_conn.commit()
            return len(param_list)
    
    async def execute_returning(self, sql, params=None, returning=None):
        if _db_type == DBType.PostgreSQL:
            sql, args = _pg_binds(sql, params)
            row = await self._raw_conn.fetchrow(sql, *args)
            return dict(row) if row else {}
        elif _db_type == DBType.MySQL:
            if isinstance(returning, dict):
                if len(returning) != 1:
                    raise ValueError(
                        "[flameo] For MySQL/MariaDB, 'returning' must be a column "
                        "name or a single-key dict"
                    )
                returning = next(iter(returning))
            elif not isinstance(returning, str) or not returning:
                raise ValueError("\033[33m\n\n[flameo] For MySQL/Maria DB, column name parameter 'returning' must be provided as a parameter of execute_returning.\n\n\033[0m")
            row = None
            has_is_returning = re.search(r"\breturning\b", sql, re.IGNORECASE)
            nested = self._transaction is not None
            if not nested:
                await self.begin_transaction()
            try:
                async with self._raw_conn.cursor() as cur:
                    await cur.execute(sql, params or ())
                    await cur.execute("SELECT LAST_INSERT_ID()")
                    row = await cur.fetchone()
                if not nested:
                    await self.commit_transaction()
            except Exception as e:
                if not nested:
                    await self.rollback_transaction()
                if has_is_returning:
                    raise ValueError(
                        "\033[33m\n\n[flameo] For MySQL/MariaDB, you don't need "
                        "to send RETURNING in your query itself. Flameo manages "
                        f"that for you. Original error: {e}\n\n\033[0m"
                    ) from e
                raise
            if not row:
                return {}
            return {returning: row[0]}
        elif _db_type == DBType.SQLite:
            cur = await self._raw_conn.execute(sql, params or ())
            row = await cur.fetchone()
            if not self._transaction:
                await self._raw_conn.commit()
            if not row:
                return {}
            columns = [col[0] for col in cur.description]
            return dict(zip(columns, row))
        
        elif _db_type == DBType.Oracle:
            if not returning:
                raise ValueError("[flameo] For Oracle, 'returning' dict must be provided")
            if not isinstance(returning, dict):
                raise ValueError(
                    "[flameo] For Oracle, 'returning' must be a dict of "
                    "{column_name: python_type}"
                )
            if params and not isinstance(params, dict):
                raise ValueError(
                    "[flameo] For Oracle, execute_returning requires params to be "
                    "a named-bind dict, e.g. {'id': 1}"
                )

            if re.search(r"\bRETURNING\b", sql, re.IGNORECASE) and not sql.strip().upper().startswith("BEGIN"):
                sql = f"BEGIN {sql}; END;"
            
            async with self._raw_conn.cursor() as cur:
                bind_vars = {}
                for k, py_type in returning.items():
                    oracle_type = _python_to_oracle_type.get(py_type)
                    if not oracle_type:
                        raise ValueError(f"[flameo] Unsupported Python type for Oracle returning: {py_type}\nOptions are int, float, decimal.Decimal, str, bytes, bytearray, datetime.date, datetime.datetime, datetime.time")
                    bind_vars[k] = cur.var(oracle_type)
                
                exec_params = params.copy() if params else {}
                exec_params.update(bind_vars)

                await cur.execute(sql, exec_params)

                result = {k: v.getvalue() for k, v in bind_vars.items()}

            if not self._transaction:
                await self._raw_conn.commit()

            return result

        else:
            raise ValueError("Unsupported database type")

    async def call_procedure(self, name, params=None, *, out=None, returns_rows=False):
        """Call a stored procedure and return a dict of OUT parameter values.

        ``out`` maps OUT parameter names to Python types. With
        ``returns_rows=True`` the result is ``{"out": {...}, "rows": [...]}``;
        otherwise only the OUT dict is returned. PostgreSQL: plain ``CALL``
        with no ``out``; with ``out`` (or ``returns_rows``) ``name`` is a
        set-returning function, since asyncpg cannot stream rows from CALL.
        MySQL: pass a placeholder (e.g. ``None``) for every OUT parameter in
        ``params``; values are read from ``@_<name>_<index>`` session
        variables. Oracle: ``returns_rows`` appends a SYS_REFCURSOR OUT bind
        as the last argument and requires ``init_db_pool()``. SQLite has no
        procedures. Names must be plain identifiers; never build them from
        request data.
        """
        if not isinstance(name, str) or not _PROC_NAME.match(name):
            raise ValueError(
                "[flameo] Procedure name must be a plain identifier, "
                "e.g. 'schema.proc_name'"
            )
        out = out or {}
        if not isinstance(out, dict) or not all(
            isinstance(key, str) and _PROC_NAME.match(key) for key in out
        ):
            raise ValueError("[flameo] 'out' must be a dict of identifier: type pairs")

        def _result(out_values, rows=None):
            return out_values if rows is None else {"out": out_values, "rows": rows}

        if _db_type == DBType.PostgreSQL:
            if isinstance(params, dict):
                if not all(_PROC_NAME.match(key) for key in params):
                    raise ValueError("[flameo] Parameter names must be plain identifiers")
                placeholders = ", ".join(f":{key}" for key in params)
            else:
                placeholders = ", ".join(
                    f"${index + 1}" for index in range(len(params or ()))
                )
            if returns_rows and not out:
                raise ValueError(
                    "[flameo] PostgreSQL CALL cannot return rows; use out=... "
                    "to call a set-returning function"
                )
            if out:
                query = f"SELECT * FROM {name}({placeholders})"
                query, args = _pg_binds(query, params)
                if returns_rows:
                    records = await self._raw_conn.fetch(query, *args)
                    row = records[0] if records else None
                    values = list(row.values()) if row else []
                    out_values = {
                        key: values[index] if index < len(values) else None
                        for index, key in enumerate(out)
                    }
                    return _result(out_values, [dict(record) for record in records])
                row = await self._raw_conn.fetchrow(query, *args)
                values = list(row.values()) if row else []
                return _result({
                    key: values[index] if index < len(values) else None
                    for index, key in enumerate(out)
                })
            query = f"CALL {name}({placeholders})"
            query, args = _pg_binds(query, params)
            await self._raw_conn.execute(query, *args)
            return _result({})

        elif _db_type == DBType.MySQL:
            if isinstance(params, dict):
                if not all(_PROC_NAME.match(key) for key in params):
                    raise ValueError("[flameo] Parameter names must be plain identifiers")
                if out and "." in name:
                    raise ValueError(
                        "[flameo] MySQL OUT parameters require an unqualified procedure name"
                    )
                missing = [key for key in out if key not in params]
                if missing:
                    raise ValueError(
                        f"[flameo] MySQL OUT parameters must be present in params: {missing}"
                    )
                keys = list(params)
                call_args = [params[key] for key in keys]
            else:
                keys = None
                call_args = list(params or ())
            rows = []
            async with self._raw_conn.cursor() as cur:
                await cur.callproc(name, call_args)
                while True:
                    if returns_rows and cur.description is not None:
                        columns = [col[0] for col in cur.description]
                        rows.extend(
                            dict(zip(columns, row)) for row in await cur.fetchall()
                        )
                    if not await cur.nextset():
                        break
                result = {}
                if out:
                    if keys is not None:
                        indexes = [keys.index(key) for key in out]
                    else:
                        indexes = [len(call_args) - len(out) + i for i in range(len(out))]
                        if any(index < 0 for index in indexes):
                            raise ValueError(
                                "[flameo] MySQL OUT parameters must occupy the "
                                "trailing positions of 'params'"
                            )
                    variables = ", ".join(f"@_{name}_{index}" for index in indexes)
                    await cur.execute(f"SELECT {variables}")
                    row = await cur.fetchone()
                    values = row or ()
                    result = {
                        key: values[index] if index < len(values) else None
                        for index, key in enumerate(out)
                    }
                if not self._transaction:
                    await self._raw_conn.commit()
                return _result(result, rows if returns_rows else None)

        elif _db_type == DBType.Oracle:
            cur = self._get_cursor()
            call_args = (
                [params[key] for key in params] if isinstance(params, dict)
                else list(params or ())
            )
            out_vars = {}
            for key, python_type in out.items():
                oracle_type = _python_to_oracle_type.get(python_type)
                if not oracle_type:
                    raise ValueError(
                        f"[flameo] Unsupported Python type for Oracle OUT parameter: {python_type}"
                    )
                out_vars[key] = cur.var(oracle_type)
                call_args.append(out_vars[key])
            cursor_var = None
            if returns_rows:
                if _oracle_cursor_type is None:
                    raise ValueError(
                        "[flameo] Oracle REF CURSOR support requires init_db_pool()"
                    )
                cursor_var = cur.var(_oracle_cursor_type)
                call_args.append(cursor_var)
            await cur.callproc(name, call_args)
            rows = []
            if cursor_var is not None:
                rows = await self._read_ref_cursor(cursor_var)
            if not self._transaction:
                await self._raw_conn.commit()
            out_values = {key: var.getvalue() for key, var in out_vars.items()}
            return _result(out_values, rows if returns_rows else None)

        elif _db_type == DBType.SQLite:
            raise NotImplementedError(
                "[flameo] SQLite has no stored procedures; use execute()/executemany()"
            )

    async def ref_cursor(self, name, params=None):
        """Call an Oracle procedure whose OUT parameter is a REF CURSOR.

        ``name`` is the procedure identifier. ``params`` supplies its IN
        arguments (dict values in order, or a sequence); a SYS_REFCURSOR OUT
        bind is appended as the last argument. Rows are returned as a list of
        dicts and the cursor is always closed. Requires ``init_db_pool()``.
        Other backends have no REF CURSOR and raise NotImplementedError.
        """
        if _db_type != DBType.Oracle:
            raise NotImplementedError(
                "[flameo] ref_cursor() is only supported on Oracle; use fetch() on other backends"
            )
        if not isinstance(name, str) or not _PROC_NAME.match(name):
            raise ValueError(
                "[flameo] Procedure name must be a plain identifier, "
                "e.g. 'schema.proc_name'"
            )
        if _oracle_cursor_type is None:
            raise ValueError("[flameo] Oracle REF CURSOR support requires init_db_pool()")

        cur = self._get_cursor()
        call_args = (
            [params[key] for key in params] if isinstance(params, dict)
            else list(params or ())
        )
        cursor_var = cur.var(_oracle_cursor_type)
        call_args.append(cursor_var)
        await cur.callproc(name, call_args)
        rows = await self._read_ref_cursor(cursor_var)
        if not self._transaction:
            await self._raw_conn.commit()
        return rows

    async def close(self):
        if self._closed:
            return
        if _db_type == DBType.PostgreSQL:
            if self._cursor:
                try:
                    await self._cursor.close()
                except:
                    pass
        elif _db_type == DBType.Oracle and self._transaction:
            self._tx_depth = 0
            await self.rollback_transaction()
        elif _db_type == DBType.MongoDB:
            try:
                await self._end_mongo_session(abort=True)
            except Exception:
                pass
            self._closed = True
            return
        self._closed = True

    def __getitem__(self, name):
        value = self._raw_conn[name]
        if _db_type == DBType.MongoDB and _is_mongo_database(value):
            child = QueryManager(value)
            child._mongo_client = self._mongo_client or self._mongo_session_client()
            return child
        return value

    def __getattr__(self, name):
        return getattr(self._raw_conn, name)


_CACHE_DEFAULTS = {
    "timeout": 5,
    "decode_responses": False,
    "socket_connect_timeout": 5,
    "socket_timeout": 5,
    "health_check_interval": 30,
}


def _validate_cache_config(config):
    if not isinstance(config, dict):
        raise ValueError("[flameo] cache_config must be a dict")
    missing = [k for k in ("cache_type", "max_connections", "cache_url")
               if k not in config]
    if missing:
        raise ValueError(
            "[flameo] cache_config requires %s" % ", ".join(missing)
        )
    if not isinstance(config["cache_type"], CacheType):
        raise ValueError(
            "[flameo] cache_config 'cache_type' must be a CacheType "
            "(CacheType.Redis, CacheType.Valkey or CacheType.Dragonfly)"
        )
    if not isinstance(config["max_connections"], int) or config["max_connections"] < 1:
        raise ValueError(
            "[flameo] cache_config 'max_connections' must be a positive integer"
        )
    if not isinstance(config["cache_url"], str) or not config["cache_url"]:
        raise ValueError(
            "[flameo] cache_config 'cache_url' must be a non-empty string"
        )


class Flameo:
    
    def __init__(self, **kwargs):

        self.router = RouterMap()
        self.global_middlewares = []
        self._global_mw_metas_src = None
        self._global_mw_metas = ()
        self.DB_POOL = None
        self.CACHE_POOL = None
        self.CACHE = None
        # The public handler cache and the internal endpoint-response
        # cache are separate so response-cache churn cannot evict keys
        # an application handler deliberately stored.
        self._memory_cache = MemoryCacheStore()
        self._response_memory_cache = MemoryCacheStore()
        self._memory_rate_limit = MemoryCacheStore()
        self.static_middleware__ = None
        
        self.not_found_handler__ = None
        self.db_config = kwargs.get("db_config")
        self.cache_config = kwargs.get("cache_config")
        if self.cache_config is not None:
            _validate_cache_config(self.cache_config)
        self.debug = kwargs.get("debug", False)
        self.validate_case_sensitive: bool = kwargs.get("validate_case_sensitive", True)
        trust_ip = kwargs.get("trust_ip")
        if trust_ip is None:
            trust_ip = ()
        if not isinstance(trust_ip, (list, tuple, set, frozenset)):
            raise ValueError("[flameo] trust_ip must be a list of trusted proxy IP addresses")
        validated = []
        for address in trust_ip:
            try:
                validated.append(str(ipaddress.ip_address(address)))
            except ValueError:
                raise ValueError(
                    f"[flameo] trust_ip contains an invalid IP address: {address!r}"
                )
        self.trust_ip = frozenset(validated)
        
        self.title = kwargs.get("title")
        self.description = kwargs.get("description")
        self.max_body_size = kwargs.get("max_body_size", 1024 * 1024 * 10)
        global _max_body_size
        _max_body_size = self.max_body_size
        
        self.version = kwargs.get("version")
        self._active_requests = weakref.WeakSet()
        self._shutting_down = False
        
        self._rsgi_init_task__ = None
        self.cpu_executor = CPUExecutor()
        self.enable_gzip = False
        self.gzip_min_size = 1024
        self._gzip_module = None
        
        self._compressors: dict[str, callable | None] = {}
        self._stream_factories: dict[str, callable | None] = {}
        self._compress_level: dict[str, int] = {}
        
        
        self._compressible_types: frozenset[str] = frozenset({
            "application/json",
            "application/problem+json",
            "application/xml",
            "text/xml",
            "text/html",
            "text/plain",
            "text/css",
            "text/javascript",
            "application/javascript",
            "application/wasm",
            "application/x-javascript",
            "application/graphql-response+json",
            "text/csv",
            "text/event-stream",
            "application/ld+json",
            "application/manifest+json",
            "application/sql",
        })
        
        self._compressible_suffixes: tuple[str, ...] = ("+json", "+xml")
        
        
        self._encoding_priority: tuple[str, ...] = (
            "zstd", "br", "gzip", "deflate",
        )
        
        
        self._encoding_wire: dict[str, str] = {
            "zstd": "zstd",
            "br": "br",
            "gzip": "gzip",
            "deflate": "deflate",
        }
        self._json_buffer = bytearray(64 * 1024)
        self._base_headers: list = []
        self._base_header_map = dict(self._base_headers)
        
        self._show_requests: bool = False
        self._template_env = None
        self._template_context: dict | None = None

        self._temp_sweeper: TempSweeper | None = None

        self._state_store: dict = {}
        self.state = _State(self._state_store)
        self.locals = _State(self._state_store)
        self._startup_handlers: list = []
        self._shutdown_handlers: list = []
        self._startup_ran: bool = False
        self._shutdown_ran: bool = False

        # ``temp_dir`` relocates the directory the parser spills uploads
        # into. Cleanup is always enabled; without ``temp_dir`` the
        # parser's default ``temp/`` folder is used.
        self.temp_dir: str | None = kwargs.get("temp_dir")
        if self.temp_dir:
            set_temp_dir(self.temp_dir)
        self.enable_temp_cleanup()

        # ``log_dir`` relocates the directory the logger writes its daily
        # log files into. Defaults to ``<cwd>/logs`` when not provided.
        self.log_dir: str | None = kwargs.get("log_dir")
        if self.log_dir:
            set_log_dir(self.log_dir)

        # ``max_log_bytes`` is the size at which a log file rolls over to
        # the next numbered one (``flameo-YYYY-MM-DD-1.log``, ...).
        self.max_log_bytes: int | None = kwargs.get("max_log_bytes")
        if self.max_log_bytes:
            set_max_log_bytes(self.max_log_bytes)

        # --- Swagger / ReDoc documentation ---------------------------
        # Docs are generated only when ``docs_url`` (or its alias
        # ``swagger_url``) is supplied. The two are mutually exclusive.
        auth_header = kwargs.get("auth_header")
        if auth_header is True:
            auth_header = "Authorization"
        if auth_header and not isinstance(auth_header, str):
            raise ValueError("[flameo] auth_header must be True or a header name")
        self.auth_header: str | None = auth_header or None
        if kwargs.get("docs_url") and kwargs.get("swagger_url"):
            raise ValueError(
                "[flameo] Provide only one of docs_url or swagger_url"
            )
        ui_path = normalize_docs_path(
            "docs_url/swagger_url", kwargs.get("docs_url") or kwargs.get("swagger_url")
        )
        redoc_path = normalize_docs_path("redoc_url", kwargs.get("redoc_url"))
        spec_path = normalize_docs_path("openapi_url", kwargs.get("openapi_url"))
        if redoc_path and not ui_path:
            raise ValueError(
                "[flameo] redoc_url requires docs_url or swagger_url"
            )
        if spec_path and not ui_path:
            raise ValueError(
                "[flameo] openapi_url requires docs_url or swagger_url"
            )
        if ui_path and not spec_path:
            spec_path = "/openapi.json"
        enabled_paths = [p for p in (ui_path, redoc_path, spec_path) if p]
        if len(set(enabled_paths)) != len(enabled_paths):
            raise ValueError(
                "[flameo] docs_url/swagger_url, redoc_url and openapi_url "
                "must use distinct paths"
            )
        self._docs_ui_path = ui_path
        self._docs_redoc_path = redoc_path
        self._docs_openapi_path = spec_path if ui_path else None
        self._docs_paths = frozenset(enabled_paths)
        self._docs_registry = DocsRegistry() if ui_path else None
        if ui_path:
            register_docs_routes(self)

    def gzip_compress(self, data):
        import gzip as _gzip
        import io as _io
        buf = _io.BytesIO()
        with _gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as f:
            f.write(data)
        return buf.getvalue()

    def deflate_compress(self, data):
        import zlib as _zlib
        co = _zlib.compressobj(level=6, wbits=-15)
        return co.compress(data) + co.flush()

    def zstd_compress(self, data):
        zstd = self._zstd_module
        return zstd.ZstdCompressor(level=self._compress_level["zstd"]).compress(data)

    def brotli_compress(self, data):
        br = self._brotli_module
        return br.compress(data, quality=self._compress_level["br"])
    
    def enable_temp_cleanup(
        self,
        interval_seconds: float = _DEFAULT_INTERVAL,
        threshold_seconds: float = _DEFAULT_THRESHOLD,
    ) -> None:
        sweeper = self._temp_sweeper
        if sweeper is not None:
            if sweeper._task is None and not sweeper._started:
                if interval_seconds <= 0:
                    raise ValueError("interval_seconds must be > 0")
                if threshold_seconds <= 0:
                    raise ValueError("threshold_seconds must be > 0")
                sweeper.interval_seconds = interval_seconds
                sweeper.threshold_seconds = threshold_seconds
            return
        sweeper = TempSweeper(
            interval_seconds=interval_seconds,
            threshold_seconds=threshold_seconds,
        )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._temp_sweeper = sweeper
            return
        loop.create_task(sweeper.start())
        self._temp_sweeper = sweeper

    async def disable_temp_cleanup(self) -> None:
        sweeper = self._temp_sweeper
        if sweeper is None:
            return
        self._temp_sweeper = None
        await sweeper.stop()

    async def _start_temp_sweeper(self) -> None:
        sweeper = self._temp_sweeper
        if sweeper is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if sweeper._task is not None:
            return  # already started
        loop.create_task(sweeper.start())

    def use_compression(self, min_size: int = 1024) -> None:
        """Enable response compression for eligible responses.

        Codec preference (highest first): ``zstd`` → ``br`` (brotli) →
        ``gzip`` → ``deflate``.
        Bodies smaller than ``min_size``, content types outside the
        whitelist, and clients that accept nothing are sent
        uncompressed. Defaults: ``min_size=1024`` (1 KiB). Compression
        is off until this method is called.
        """
        
        self._init_compressors()        
        self.enable_gzip = True
        self.gzip_min_size = min_size
        
        self._gzip_module = self.gzip_compress

    def accepts_gzip(self, req) -> bool:
        raw = req.headers.get("accept-encoding", "") if req.headers else ""
        if not raw:
            return False
        raw = raw.lower()
        
        gzip_q = None
        identity_q = None
        star_q = None
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if ";" in part:
                coding, params = part.split(";", 1)
                q = 1.0
                for p in params.split(";"):
                    p = p.strip()
                    if p.startswith("q="):
                        try:
                            q = float(p[2:])
                        except ValueError:
                            q = 1.0
            else:
                coding = part
                q = 1.0
            coding = coding.strip()
            if coding == "gzip":
                if gzip_q is None or q > gzip_q:
                    gzip_q = q
            elif coding == "identity":
                if identity_q is None or q > identity_q:
                    identity_q = q
            elif coding == "*":
                if star_q is None or q > star_q:
                    star_q = q
        # gzip wins iff its q-value beats identity and is non-zero.
        if gzip_q is not None and gzip_q > 0:
            if identity_q is None or gzip_q >= identity_q:
                return True
            return False
        
        
        if star_q is not None and star_q > 0:
            if identity_q is None or star_q >= identity_q:
                return True
            return False
        return False


    def _init_compressors(self) -> None:       
        # gzip
        self._compressors["gzip"] = self.gzip_compress
        self._stream_factories["gzip"] = self.stream_gzip_factory
        self._compress_level["gzip"] = 6
        # deflate
        self._compressors["deflate"] = self.deflate_compress
        self._stream_factories["deflate"] = self.stream_deflate_factory
        self._compress_level["deflate"] = 6
        # zstd — try the C-backed ``zstandard`` first (faster, lower
        # memory). No pure-Python implementation ships in this repo, so a
        # missing module is the only failure mode.
        self._zstd_module = None
        try:
            import zstandard as _zstd  # type: ignore[import-not-found]
            self._zstd_module = _zstd
            self._compressors["zstd"] = self.zstd_compress
            self._stream_factories["zstd"] = self.stream_zstd_factory
            self._compress_level["zstd"] = 3
        except ImportError:
            self._compressors["zstd"] = None
            self._stream_factories["zstd"] = None
        # brotli — ``brotli`` is the C-backed wheel; ``brotlicffi`` is a
        # pure-CFFI wrapper that ships the same API surface. Prefer
        # ``brotli`` for speed, fall back to ``brotlicffi``.
        self._brotli_module = None
        try:
            import brotli as _brotli  # type: ignore[import-not-found]
            self._brotli_module = _brotli
        except ImportError:
            try:
                import brotlicffi as _brotli  # type: ignore[import-not-found]
                self._brotli_module = _brotli
            except ImportError:
                _brotli = None
        if self._brotli_module is not None:
            self._compressors["br"] = self.brotli_compress
            self._stream_factories["br"] = self.stream_brotli_factory
            self._compress_level["br"] = 4
        else:
            self._compressors["br"] = None
            self._stream_factories["br"] = None

    def _parse_accept_encoding(self, req) -> dict[str, float]:
        """Return ``{code: qvalue}`` from the request's ``Accept-Encoding``.
        """
        raw = ""
        
        try:
            raw = req.headers.get("accept-encoding", "") if req.headers else ""
        except Exception:
            raw = ""
        result: dict[str, float] = {"identity": 0.0}
        if not raw:
            return result
        for chunk in raw.lower().split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(";", 1)
            code = parts[0].strip()
            if not code:
                continue
            q = 1.0
            if len(parts) == 2:
                qpart = parts[1].strip()
                if qpart.startswith("q="):
                    try:
                        q = float(qpart[2:])
                    except ValueError:
                        q = 1.0
            
            if q < 0.0:
                q = 0.0
            elif q > 1.0:
                q = 1.0
            
            result[code] = q
        return result

    def _negotiate_encoding(self, req) -> str | None:
        """Pick the best codec the client accepts AND we can produce.
        """
        ae = self._parse_accept_encoding(req)
        identity_q = ae.get("identity", 1.0)
        star_q = ae.get("*")
        for name in self._encoding_priority:
            if self._compressors.get(name) is None:
                continue
            # Client-side acceptance check: explicit name first, then
            # wildcard. If the client listed it with q=0 we MUST skip
            # (it's the standard "no, thank you" signal).
            code_q = ae.get(name)
            if code_q is None:
                if star_q is None or star_q <= 0:
                    continue
                code_q = star_q
            if code_q <= 0:
                continue
            # If the client would strictly prefer identity over this
            # codec (e.g. ``Accept-Encoding: gzip;q=0.1, identity;q=1.0``),
            # respect that — they get uncompressed bytes. We use a
            # strict ``>`` so a tie (``identity;q=0.5, *;q=0.5``) is
            # resolved in favour of the codec: the client bothered to
            # send ``Accept-Encoding``, so they almost certainly want
            # compression when it's available — the ``Accept-Encoding:
            # identity;q=1.0, *;q=0`` form is the unambiguous "no
            # thanks" signal.
            if identity_q is not None and identity_q > code_q:
                continue
            return name
        return None

    def _is_compressible_type(self, content_type) -> bool:
        """Return True iff ``content_type`` is on the compressible list.
        """
        if content_type is None:
            return False
        if isinstance(content_type, bytes):
            try:
                content_type = content_type.decode("ascii")
            except UnicodeDecodeError:
                return False
        if not isinstance(content_type, str):
            return False
        # Drop parameters, lowercase, trim.
        bare = content_type.split(";", 1)[0].strip().lower()
        if not bare:
            return False
        if bare in self._compressible_types:
            return True
        for suffix in self._compressible_suffixes:
            if bare.endswith(suffix):
                return True
        return False

    def _maybe_compress(self, req, body: bytes) -> tuple[bytes, str | None]:
        """Return ``(body, encoding)`` honoring ``use_compression``.
        """
        selected = self._compression_for_body(req, body)
        if selected is None:
            return body, None
        compress, encoding = selected
        return compress(body), encoding

    def _compression_for_body(self, req, body: bytes):
        if not self.enable_gzip:
            return None
        if len(body) < self.gzip_min_size:
            return None
        
        if not self._compressors:
            return None
        
        encoding = self._negotiate_encoding(req)
        if encoding is None:
            return None
        compress = self._compressors.get(encoding)
        if compress is None:
            return None
        return compress, encoding

    async def _maybe_compress_async(self, req, body: bytes) -> tuple[bytes, str | None]:
        """Compress response bodies without blocking the event loop on large payloads."""
        selected = self._compression_for_body(req, body)
        if selected is None:
            return body, None
        compress, encoding = selected
        if len(body) >= _COMPRESSION_TO_THREAD:
            compressed = await asyncio.to_thread(compress, body)
        else:
            compressed = compress(body)
        return compressed, encoding

    def stream_compress_factory(self, req, content_type, file_size):
        """Streaming-file single funnel.
        """
        if not self.enable_gzip:
            return None, None
        if file_size is None:
            return None, None
        if file_size < self.gzip_min_size:
            return None, None
        if not self._is_compressible_type(content_type):
            return None, None
        encoding = self._negotiate_encoding(req)
        if encoding is None:
            return None, None
        factory = self._stream_factories.get(encoding)
        if factory is None:
            return None, None
        return encoding, factory

    def stream_gzip_factory(self):
        """Return a ``(compress_fn, finalize_fn, gzip_header)`` 3-tuple for
        streaming gzip output.
        """
        import zlib as _zlib
        
        comp = _zlib.compressobj(level=6, wbits=-15, memLevel=8)
        
        gzip_header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"

        # Python 3.14's ``zlib.Compress`` doesn't expose running counters,
        # so we maintain the gzip trailer (CRC32 + ISIZE) ourselves.
        # Both fields are little-endian uint32 in the gzip trailer.
        crc_state = 0  # unsigned CRC32 accumulator
        isize = 0  # unsigned uncompressed-byte counter

        def compress(chunk: bytes) -> bytes:
            nonlocal crc_state, isize
            # Update the running CRC32 + length BEFORE handing the chunk
            # to the compressor. Order doesn't matter for correctness
            # but doing it here means the trailer is ready the moment
            # the last deflate block ships.
            crc_state = _zlib.crc32(chunk, crc_state) & 0xFFFFFFFF
            isize = (isize + len(chunk)) & 0xFFFFFFFF
            return comp.compress(chunk)

        def finalize() -> bytes:
            
            tail = comp.flush(_zlib.Z_FINISH)
            if tail.endswith(b"\x00\x00\xff\xff"):
                tail = tail[:-4]
            trailer = (
                crc_state.to_bytes(4, "little")
                + isize.to_bytes(4, "little")
            )
            return tail + trailer

        return compress, finalize, gzip_header

    def stream_deflate_factory(self):
        """Streaming raw deflate (HTTP ``Content-Encoding: deflate``).
        """
        import zlib as _zlib
        comp = _zlib.compressobj(level=6, wbits=-15)

        def compress(chunk: bytes) -> bytes:
            return comp.compress(chunk)

        def finalize() -> bytes:
            tail = comp.flush(_zlib.Z_FINISH)
            if tail.endswith(b"\x00\x00\xff\xff"):
                tail = tail[:-4]
            return tail

        return compress, finalize, b""

    def stream_zstd_factory(self):
        """Streaming zstd for the ``sendFile`` / ``download`` path.
        """
        zstd = self._zstd_module
        co = zstd.ZstdCompressor(level=self._compress_level["zstd"]).compressobj()

        def compress(chunk: bytes) -> bytes:
            return co.compress(chunk)

        def finalize() -> bytes:            
            return co.flush(zstd.COMPRESSOBJ_FLUSH_FINISH)

        return compress, finalize, b""

    def stream_brotli_factory(self):
        """Streaming brotli for the ``sendFile`` / ``download`` path.
        """
        br = self._brotli_module
        comp = br.Compressor(quality=self._compress_level["br"])

        def compress(chunk: bytes) -> bytes:
            # ``process`` is the streaming analogue of ``compress`` —
            # same return-empty-when-nothing-ready contract.
            return comp.process(chunk)

        def finalize() -> bytes:
            return comp.finish()

        return compress, finalize, b""

    def show_requests(self, enabled: bool = True) -> bool:
        """request logging on stdout.

        The status code is colored:
          * 1xx/2xx/3xx → green
          * 4xx         → yellow
          * 5xx         → red
          * other       → default

        Colors are emitted via ANSI escape codes. If stdout is not a TTY
        (e.g. log file, piped output) colors are skipped automatically.

        Returns the resulting state so it's chainable.
        """
        self._show_requests = bool(enabled)
        return self._show_requests

    _ANSI = {
        "reset":  "\033[0m",
        "green":  "\033[32m",
        "yellow": "\033[33m",
        "red":    "\033[31m",
        "cyan":   "\033[36m",
        "dim":    "\033[2m",
    }

    def _color_for_status(self, status: int) -> str:
        if 200 <= status < 400:
            return self._ANSI["green"]
        if 400 <= status < 500:
            return self._ANSI["yellow"]
        if 500 <= status < 600:
            return self._ANSI["red"]
        return ""

    def _colors_enabled(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        try:
            return sys.stdout.isatty()
        except Exception:
            return False

    def _format_request_log(self, method: str, path: str, status: int,
                            size: int, http_version: str = "HTTP/1.1") -> str:
        ts = time.strftime("%d/%b/%Y %H:%M:%S", time.localtime())
        line = f'[{ts}] "{method} {path} {http_version}" {status} {size}'
        if not self._colors_enabled():
            return line
        color = self._color_for_status(status)
        if not color:
            return line
        
        return (
            f"{self._ANSI['dim']}[{ts}]{self._ANSI['reset']} "
            f'"{method} {path} {http_version}" '
            f"{color}{status}{self._ANSI['reset']} "
            f"{self._ANSI['dim']}{size}{self._ANSI['reset']}"
        )

    async def _log_request_async(self, line: str) -> None:
        semaphore = _get_log_semaphore()
        loop = asyncio.get_running_loop()
        async with semaphore:
            await loop.run_in_executor(None, print, line)

    def _maybe_log_request(self, method: str, path: str, query: bytes,
                            status: int, size: int,
                            http_version: str = "HTTP/1.1") -> None:
        
        if not self._show_requests:
            return
        full_path = f"{path}?{query.decode('latin-1')}" if query else path
        line = self._format_request_log(method, full_path, status, size, http_version)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            print(line)
            return
        
        asyncio.create_task(self._log_request_async(line))

    async def _init_mongo_pool(self):
        cfg = self.db_config
        mongo_uri = cfg.get("mongo_uri")
        database = cfg.get("db")
        if not isinstance(mongo_uri, str) or not mongo_uri:
            raise ValueError(
                "\n\n[flameo] You need to set 'mongo_uri' in 'db_config' for "
                "DBType.MongoDB.\n\ne.g.\ndb_config = {\n    "
                "'db_type': DBType.MongoDB,\n    "
                "'mongo_uri': 'mongodb://127.0.0.1:27017',\n    "
                "'db': 'flameo',\n}"
            )
        if isinstance(database, list):
            if not database or not all(
                isinstance(name, str) and name for name in database
            ):
                raise ValueError(
                    "[flameo] db_config 'db' must be a non-empty string or a "
                    "non-empty list of strings"
                )
        elif not isinstance(database, str) or not database:
            raise ValueError(
                "[flameo] db_config 'db' must be a non-empty string or a "
                "non-empty list of strings"
            )
        try:
            from pymongo import AsyncMongoClient
        except ImportError:
            raise ValueError(
                "[flameo] You need to install 'pymongo' to connect MongoDB\n"
                "pip install pymongo"
            )
        client = AsyncMongoClient(mongo_uri, **_mongo_client_options(cfg))
        try:
            await client.admin.command("ping")
        except BaseException:
            try:
                await client.close()
            except Exception:
                pass
            raise
        self.DB_POOL = _MongoPool(client, database)
        print("[flameo] Database connected successfully")

    async def init_db_pool(self):
        if self.db_config and self.DB_POOL is None:
            try:
                global _db_type
                _db_type = self.db_config.get("db_type", None)
                if _db_type is None:
                    raise ValueError(f"\n\n[flameo] You need to set 'db_type' in 'db_config'.\n\nimport DBType from flameo\nThen use DBType.PostgreSQL, DBType.MySQL, DBType.Oracle, DBType.SQLite or DBType.MongoDB according to your preference")
                if _db_type == DBType.MongoDB:
                    await self._init_mongo_pool()
                    return
                self.db_config.setdefault("min_size", 5)
                self.db_config.setdefault("max_size", 20)
                self.db_config["user"] = self.db_config.get("username", "")
                if _db_type==DBType.PostgreSQL:
                    self.db_config.pop("db_type", None)
                    self.db_config.pop("username", None)
                    try:
                        import asyncpg
                        _db_driver = asyncpg
                    except:
                        raise ValueError("\n\nYou need to install 'asyncpg' to connect PostgreSQL\npip install asyncpg")
                    self.DB_POOL = await _db_driver.create_pool(
                        **self.db_config
                    )
                    async with self.DB_POOL.acquire() as conn:
                        await conn.execute("SELECT 1")
                    print("Database connected successfully")
                elif _db_type==DBType.Oracle:
                    try:
                        import oracledb
                        _db_driver = oracledb
                        global _python_to_oracle_type, _oracle_cursor_type
                        _oracle_cursor_type = oracledb.CURSOR
                        _python_to_oracle_type = {
                            int: oracledb.NUMBER,
                            float: oracledb.NUMBER,
                            decimal.Decimal: oracledb.NUMBER,
                            str: oracledb.STRING,
                            bool: oracledb.NUMBER,
                            bytes: oracledb.BLOB,
                            bytearray: oracledb.BLOB,
                            datetime.date: oracledb.DATETIME,
                            datetime.datetime: oracledb.DATETIME,
                            datetime.time: oracledb.STRING,
                        }
                    except:
                        raise ValueError("\n\n[flameo] You need to install 'oracledb' to connect Oracle\npip install oracledb")
                    self.DB_POOL = _db_driver.create_pool_async(
                        user = self.db_config.get("username", ""),
                        password = self.db_config.get("password", ""),
                        dsn = _db_driver.makedsn(self.db_config.get("host", ""), self.db_config.get("port", 1521), service_name=self.db_config.get("database", "")),
                        min=self.db_config.get("min_size", 5),
                        max=self.db_config.get("max_size", 20),
                        increment=3,
                        stmtcachesize=30,
                    )
                    async with self.DB_POOL.acquire() as conn:
                        await conn.execute("SELECT 1 FROM DUAL")
                    print("Database connected successfully")
                elif _db_type==DBType.MySQL:
                    try:
                        import asyncmy
                        _db_driver = asyncmy
                    except:
                        raise ValueError("\n\n[flameo] You need to install 'asyncmy' and 'cryptography' to connect MySQL\npip install asyncmy cryptography")
                    self.DB_POOL = await _db_driver.create_pool(
                        host = self.db_config.get("host", ""),
                        port = self.db_config.get("port", 3306),
                        user = self.db_config.get("user", ""),
                        password = self.db_config.get("password", ""),
                        db = self.db_config.get("database", ""),
                        minsize = self.db_config.get("min_size", 5),
                        maxsize = self.db_config.get("max_size", 20),
                    )
                    async with self.DB_POOL.acquire() as conn:
                        await conn.query("SELECT 1")
                    async with self.DB_POOL.acquire() as conn:
                        async with conn.cursor() as _vcur:
                            await _vcur.execute("SELECT VERSION()")
                            _vrow = await _vcur.fetchone()
                            global version__
                            version__ = (_vrow[0] if _vrow else "") or ""
                    print("[flameo] Database connected successfully")
                elif _db_type==DBType.SQLite:
                    file_path = self.db_config.get("file_path")
                    if not file_path:
                        raise ValueError("\n\n[flameo] You need to set 'file_path' in 'db_config' for DBType.SQLite.\n\ne.g.\ndb_config = {\n    'db_type': DBType.SQLite,\n    'file_path': 'app.db',\n}")
                    try:
                        import aiosqlite
                        _db_driver = aiosqlite
                    except:
                        raise ValueError("\n\n[flameo] You need to install 'aiosqlite' to connect SQLite\npip install aiosqlite")
                    self.db_config.pop("db_type", None)
                    self.db_config.pop("file_path", None)
                    self.DB_POOL = _SQLitePool(file_path)
                    async with self.DB_POOL.acquire() as conn:
                        await conn.execute("SELECT 1")
                    print("[flameo] Database connected successfully")
            except Exception as e:
                print(f"[flameo] Database connection failed: {e}")
                raise

    async def shutdown_db_pool(self):
        if self.DB_POOL:
            close_result = self.DB_POOL.close()
            if inspect.isawaitable(close_result):
                await close_result
            else:
                # asyncmy's Pool.close() only marks the pool as closing and
                # returns None; the actual drain happens in wait_closed().
                wait_closed = getattr(self.DB_POOL, "wait_closed", None)
                if wait_closed is not None:
                    await wait_closed()
            self.DB_POOL = None

    async def init_cache_pool(self):
        if self.cache_config and self.CACHE is None:
            try:
                import redis.asyncio as redis_asyncio
            except ImportError:
                raise ValueError(
                    "\n\n[flameo] You need to install 'redis' to use cache\n"
                    "pip install 'redis[hiredis]'\n\n"
                )
            cfg = {**_CACHE_DEFAULTS, **self.cache_config}
            cache_url = cfg.pop("cache_url")
            cfg.pop("cache_type", None)
            max_connections = cfg.pop("max_connections")
            try:
                pool = redis_asyncio.BlockingConnectionPool.from_url(
                    cache_url, max_connections=max_connections, **cfg,
                )
                client = redis_asyncio.Redis(connection_pool=pool)
                bind_redis_json_helpers(client)
                await client.ping()
            except Exception as e:
                print(f"[flameo] Cache connection failed: {e}")
                raise
            self.CACHE_POOL = pool
            self.CACHE = client
            print("[flameo] Cache connected successfully")

    async def shutdown_cache_pool(self):
        client, pool = self.CACHE, self.CACHE_POOL
        self.CACHE = None
        self.CACHE_POOL = None
        if client is None:
            return
        try:
            try:
                await client.aclose()
            except AttributeError:
                await client.close()
        finally:
            if pool is not None:
                await pool.disconnect()

    def startup(self, fn=None):       
        if fn is None:
            def _decorator(func):
                self._startup_handlers.append(func)
                return func
            return _decorator
        self._startup_handlers.append(fn)
        return fn

    def shutdown(self, fn=None):        
        if fn is None:
            def _decorator(func):
                self._shutdown_handlers.append(func)
                return func
            return _decorator
        self._shutdown_handlers.append(fn)
        return fn

    async def _run_startup_hooks(self) -> None:
     
        if self._startup_ran:
            return
        self._startup_ran = True
        n = len(self._startup_handlers)
        if n and self.debug:
            print(f"[flameo] running {n} startup hook(s)")
        for hook in self._startup_handlers:
            result = hook()
            if inspect.isawaitable(result):
                await result

    async def _run_shutdown_hooks(self) -> None:
       
        if self._shutdown_ran:
            return
        self._shutdown_ran = True
        n = len(self._shutdown_handlers)
        if n and self.debug:
            print(f"[flameo] running {n} shutdown hook(s)")
        for hook in self._shutdown_handlers:
            result = hook()
            if inspect.isawaitable(result):
                await result

    def use(self, middleware):
        self.global_middlewares.append(middleware)

    def use_template(self, directory: str, filters=None, globals=None) -> None:
        if globals is not None and not isinstance(globals, dict):
            raise TypeError(
                "[flameo] use_template() 'globals' must be a dict; got "
                f"{type(globals).__name__}."
            )
        self._template_env = build_environment(directory, filters)
        self._template_context = dict(globals) if globals is not None else None

    def _reject_cwd_static(self, directory: str, fn_name: str) -> bool:
        if not directory:
            warnings.warn(
                f"[flameo] {fn_name}: refusing to mount the current directory "
                f"as static root — pass an explicit sub-directory "
                f'(e.g. "public" or "static").',
                UserWarning,
                stacklevel=3,
            )
            return True
        try:
            cwd = os.path.abspath(os.getcwd())
            target = os.path.abspath(directory)
        except (TypeError, ValueError):
            return False
        if target == cwd:
            warnings.warn(
                f"[flameo] {fn_name}: refusing to mount the current working "
                f"directory ({cwd!r}) as static root — expose a "
                f"sub-directory instead.",
                UserWarning,
                stacklevel=3,
            )
            return True
        return False

    def use_router(self, prefix: str, router) -> None:
        
        if not isinstance(router, Router):
            raise TypeError(
                "[flameo] use_router() expects an Router instance; got "
                f"{type(router).__name__}."
            )

        if not isinstance(prefix, str):
            raise TypeError(
                "[flameo] use_router() prefix must be a string; got "
                f"{type(prefix).__name__}."
            )
        # A router's mounted dispatches are copies wrapped with their
        # mount prefix. Retro-patching after mounting would leave them stale.
        router._flameo_mounted__ = True
        norm = prefix.rstrip("/")
        if norm == "":
            norm = "/"

        if not hasattr(self, "_mounted_routers"):
            self._mounted_routers = set()
        mount_key = (norm, id(router))
        if mount_key in self._mounted_routers:
            raise ValueError(
                f"[flameo] use_router: router {id(router):#x} is already mounted "
                f"at prefix {norm!r}. Re-mounting the same router at "
                f"the same prefix would silently overwrite its routes."
            )
        self._mounted_routers.add(mount_key)

        def _prefix_paths(route_path: str) -> list[str]:
            if route_path in ("", "/"):
                if norm == "/":
                    return ["/"]
                return [norm, norm + "/"]
            rel = route_path if route_path.startswith("/") else "/" + route_path
            if norm == "/":
                
                return [rel if rel.startswith("/") else "/" + rel]
            return [norm + rel]

        for method, route_path, dispatch, route_mws in router._iter_routes():
            if norm != "/":
                dispatch = _with_base_url(dispatch, norm)
            for full_path in _prefix_paths(route_path):
                self.router.add_route(method, full_path, dispatch, route_mws)

        for mw in router._iter_middlewares():
            self.global_middlewares.append(_make_router_middleware(norm, mw))

        # Mirror the router's routes into the docs registry so the
        # spec shows the mounted (prefixed) paths, grouped under the
        # router's title as the OpenAPI tag.
        if self._docs_registry is not None:
            owner_tag = getattr(router, "title", None)
            if not owner_tag:
                owner_tag = norm.strip("/") or None
            if owner_tag:
                self._docs_registry.add_tag(
                    owner_tag, getattr(router, "description", None)
                )
            router_registry = getattr(router, "_docs_registry", None)
            if router_registry is not None:
                for record in router_registry.records():
                    full_path = _prefix_paths(record.path)[0]
                    if self._docs_paths and full_path in self._docs_paths:
                        raise ValueError(
                            f"[flameo] Router route {full_path!r} collides "
                            "with a docs URL; change the route path or "
                            "docs_url/openapi_url"
                        )
                    self._docs_registry.add(record.rebased(full_path, owner_tag))

        
        if hasattr(self.router, "_cache"):
            self.router._cache.clear()

    def serve_static(self, url_prefix: str, directory: str):
        url_prefix = url_prefix.rstrip("/")
        if self._reject_cwd_static(directory, "serve_static"):
            return
        directory = os.path.realpath(directory)

        async def static_middleware(req, res):
            
            if req.path.startswith(url_prefix):
                rel_path = req.path[len(url_prefix):].lstrip("/")
                file_path = os.path.realpath(os.path.join(directory, rel_path))
                
                if os.path.commonpath([directory, file_path]) != directory:
                    await res.send("Access denied", status=403)
                    return True
                
                try:
                    file_stat = os.stat(file_path)
                except OSError:
                    await res.send("File not found", status=404)
                    return True
                if not stat.S_ISREG(file_stat.st_mode):
                    await res.send("File not found", status=404)
                    return True

                mime_type, inline_disposition, etag, last_modified = _static_file_metadata(
                    file_path, file_stat.st_size, file_stat.st_mtime_ns,
                )
                if _static_request_not_modified(req, etag, file_stat.st_mtime_ns):
                    res.setHeader("Cache-Control", _STATIC_CACHE_CONTROL.decode("ascii"))
                    res.setHeader("ETag", etag.decode("ascii"))
                    res.setHeader("Last-Modified", last_modified.decode("ascii"))
                    await res._send_body(b"", b"text/plain; charset=utf-8", 304)
                    return True
                                
                # Delegate to ``res._send_file_streaming`` so the size-tier
                # chunking policy mirrors ``res.sendFile`` exactly:
                #
                #   * ``file_size < 64 KiB``  -> single ``read()`` and one
                #     ``http.response.body`` event (the whole file in one
                #     frame; equivalent to the ASGI single-shot path).
                #   * ``64 KiB <= file_size < 4 MiB`` -> reads in
                #     ``chunk_size`` slices (default 256 KiB), each shipped
                #     as a chunked body event.
                #   * ``file_size >= 4 MiB``  -> 1 MiB reads to amortize
                #     syscall + executor-hop overhead.
                #
                # ``file_path`` has already been bounded by the directory
                # check above, so we go one layer below ``sendFile`` to
                # skip its ``inspect.stack``-based path-safety walk (which
                # would otherwise see this framework file as the caller).
                await res._send_file_streaming(
                    resolved_path=file_path,
                    content_type=mime_type,
                    status=200,
                    chunk_size=256 * 1024,
                    file_size=file_stat.st_size,
                    extra_headers=(
                        (b"content-disposition", inline_disposition),
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", _STATIC_CACHE_CONTROL),
                        (b"etag", etag),
                        (b"last-modified", last_modified),
                    ),
                )
                return True

        self.static_middleware__ = static_middleware

    def use_static(self, url_prefix: str, directory: str):
        
        url_prefix = url_prefix.rstrip("/")
        if self._reject_cwd_static(directory, "use_static"):
            return
        directory = os.path.realpath(directory)

        async def static_middleware(req, res):
            # Only handle paths under our prefix. Returning ``False``
            # tells the dispatcher loop "not mine, keep going".
            if req.path != url_prefix and not req.path.startswith(url_prefix + "/"):
                return False

            rel_path = req.path[len(url_prefix):].lstrip("/")
            file_path = os.path.realpath(os.path.join(directory, rel_path))

            # Traversal protection: ``file_path`` must stay inside
            # ``directory``. ``commonpath`` can raise on Windows when
            # the resolved path crosses drives — treat that as a 403.
            try:
                if os.path.commonpath([directory, file_path]) != directory:
                    await res.send("Access denied", status=403)
                    return True
            except ValueError:
                await res.send("Access denied", status=403)
                return True

            try:
                file_stat = os.stat(file_path)
            except FileNotFoundError:
                await res.send("Not found", status=404)
                return True
            except OSError:
                await res.send("Not a file", status=403)
                return True
            if stat.S_ISDIR(file_stat.st_mode):
                await res.send("Forbidden", status=403)
                return True
            if not stat.S_ISREG(file_stat.st_mode):
                await res.send("Not a file", status=403)
                return True

            mime_type, inline_disposition, etag, last_modified = _static_file_metadata(
                file_path, file_stat.st_size, file_stat.st_mtime_ns,
            )
            if _static_request_not_modified(req, etag, file_stat.st_mtime_ns):
                res.setHeader("Cache-Control", _STATIC_CACHE_CONTROL.decode("ascii"))
                res.setHeader("ETag", etag.decode("ascii"))
                res.setHeader("Last-Modified", last_modified.decode("ascii"))
                await res._send_body(b"", b"text/plain; charset=utf-8", 304)
                return True

            # Delegate to ``res._send_file_streaming`` so the size-tier
            # chunking policy mirrors ``res.sendFile`` exactly:
            #
            #   * ``file_size < 64 KiB``  -> single ``read()`` and one
            #     ``http.response.body`` event (the whole file in one
            #     frame; equivalent to the ASGI single-shot path).
            #   * ``64 KiB <= file_size < 4 MiB`` -> reads in
                #     ``chunk_size`` slices (default 256 KiB), each shipped
            #     as a chunked body event.
                #   * ``file_size >= 4 MiB``  -> 1 MiB reads to amortize
            #     syscall + executor-hop overhead.
            #
            # ``file_path`` has already been bounded by the directory
            # check above, so we go one layer below ``sendFile`` to
            # skip its ``inspect.stack``-based path-safety walk (which
            # would otherwise see this framework file as the caller).
            file_size = file_stat.st_size
            await res._send_file_streaming(
                resolved_path=file_path,
                content_type=mime_type,
                status=200,
                chunk_size=256 * 1024,
                file_size=file_size,
                extra_headers=(
                    (b"content-disposition", inline_disposition),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", _STATIC_CACHE_CONTROL),
                    (b"etag", etag),
                    (b"last-modified", last_modified),
                ),
            )
            return True

        self.global_middlewares.append(static_middleware)


    def use_session(self, **options):
        """Enable signed-cookie session middleware

            app.use_session(
                secret_key="super-secret-key-please-change",
                max_age=86400,          # 1 day, in seconds
                https_only=False,
                same_site="lax",
                path="/",
                name="session",
            )

        In handlers::

            request.session["message"] = "Hello World"
            await res.send(request.session.get("message") or "")

        Parameter names are flexible — every option accepts both
        snake_case and camelCase (``secret_key`` / ``secretKey``,
        ``max_age`` / ``maxAge``, ``https_only`` / ``httpsOnly``,
        ``same_site`` / ``sameSite``).

        The cookie is **always HttpOnly** (clients cannot read it via
        JS). Tamper resistance is provided by HMAC-SHA256 over the
        payload + issue timestamp using ``secret_key``.

        ``secret_key`` MUST be at least 10 characters.
        """
        middleware = make_session_middleware(self, **options)
        self.global_middlewares.append(middleware)
        return middleware

    def use_cors(self, **options):
        """Enable CORS middleware.


            app.use_cors(
                origins=["http://127.0.0.1:8001", "http://example.com"],
                credentials=True,
                methods=["GET", "POST", "PUT", "DELETE"],
                headers=["Content-Type", "Authorization"],
                expose_headers=["X-Request-Id"],
                max_age=600,
            )

        Every option accepts **both** naming styles — the friendly
        short form (``origins`` / ``methods`` / ``headers``) and the
        FastAPI form (``allow_origins`` / ``allow_methods`` /
        ``allow_headers``). ``expose_headers`` and ``max_age`` also
        accept the camelCase variants (``exposeHeaders`` / ``maxAge``).
        Last write wins when both spellings are passed.

        Defaults:

            * ``origins``    -> ``["*"]``
            * ``credentials``-> ``False``
            * ``methods``    -> standard REST verbs
            * ``headers``    -> ``["*"]``
            * ``expose_headers`` -> ``[]``
            * ``max_age``    -> ``600``  (10 min)

        Returns the installed middleware for testability.
        """
        middleware = make_cors_middleware(self, **options)
        self.global_middlewares.append(middleware)
        return middleware

    def not_found(self, handler):
        """Register a custom 404 handler.

        — a no-path ``app.use`` callback that fires when no route
        matches. Pass any callable ``async (req, res) -> ...`` and it
        will be invoked instead of the built-in
        ``Cannot <METHOD> <path>`` response. The handler is responsible
        for ending the response (``res.send`` / ``res.json`` /
        ``res.sendStatus(404)``).
        """
        if not callable(handler):
            raise TypeError("[flameo] not_found handler must be a callable")
        self.not_found_handler__ = handler
        return handler

    def cache(self, ttl=None):
        """Cache an endpoint response for ``ttl`` seconds.

        Place above the route decorator::

            @app.cache(ttl=100)
            @app.get("/items")
            async def items(req, res): ...

        The cache decorator can also be placed below the route decorator.
        """
        return _cache_decorator(self, None, ttl, "app")

    def limit(self, count, seconds, key_function=None, *, key_func=None):
        """Limit requests to an endpoint within a fixed time window.

        ``key_function`` (also accepted as ``key_func``) receives the
        request and returns the value used to group requests. Without it,
        all requests to this endpoint share one limit.
        """
        if key_function is not None and key_func is not None:
            raise ValueError("[flameo] provide only one of key_function or key_func")
        return _limit_decorator(
            self, None, count, seconds,
            key_function if key_function is not None else key_func, "app",
        )

    def route(self, method, *args, middlewares=None, filters=None,
              summary=None, description=None, tags=None, responses=None,
              query=None, headers=None, deprecated=False, operation_id=None):
        """Register a route, optionally with its own template filters.
        Decorator form:   ``@app.route("POST", "/path", mw1, mw2)``
        Imperative form:  ``app.route("POST", "/path", mw1, mw2, handler)``

        ``filters`` is an optional list of functions registered as Jinja2
        filters that are available **only** to the templates this route
        renders through ``res.render()``. The app-wide filters registered
        via ``app.use_template(directory, filters=...)`` remain available
        as well. Each function is exposed under its own ``__name__`` and is
        called in the usual Jinja2 manner::

            def shout(value, times=1):
                return (str(value).upper() + "!") * times

            @app.get("/x", filters=[shout])
            async def handler(req, res):
                await res.render("x.html")

            # {{ name | shout }}      -> "MOE!"
            # {{ name | shout(3) }}   -> "MOE!MOE!MOE!"

        A dict may be passed instead of a list to choose the names
        explicitly: ``filters={"exclaim": shout}``.

        Filters may be sync or ``async def``; an async filter is awaited
        by the renderer.

        Documentation kwargs (used when ``docs_url``/``swagger_url`` is
        configured): ``summary``, ``description``, ``tags``,
        ``responses`` (``{200: Model, 404: Model}``), ``query`` /
        ``headers`` (``{"page": int}`` or ``{"page": (int, True)}``),
        ``deprecated``, ``operation_id``. Without ``summary`` the
        handler docstring's first line is used.
        """
        if not args:
            raise ValueError("[flameo] At least a path must be provided")

        path = args[0]
        if self._docs_paths and path in self._docs_paths:
            raise ValueError(
                f"[flameo] Route path {path!r} collides with a docs URL; "
                "change the route path or docs_url/openapi_url"
            )
        middlewares = list(middlewares) if middlewares else []
        rest = list(args[1:])

        route_env = RouteTemplateEnv(filters) if filters else None

        def _record_docs(handler, meta):
            if self._docs_registry is None:
                return
            self._docs_registry.add(RouteDoc(
                method=method,
                path=path,
                handler=handler,
                body_model=meta[_META_ANNOTATION] if meta[_META_IS_PYDANTIC] else None,
                summary=summary,
                description=description,
                tags=tags,
                responses=responses,
                query=query,
                headers=headers,
                deprecated=deprecated,
                operation_id=operation_id,
            ))

        if len(rest) == 0:
            # Decorator form — return a decorator that registers the handler
            # when it is applied. Middlewares may be supplied up front.
            def decorator(handler):
                meta = _handler_meta(handler)
                spec = getattr(handler, "__flameo_cache_spec__", None)
                limit_spec = getattr(handler, "__flameo_limit_spec__", None)
                if spec is not None:
                    _warn_if_uncacheable(method)
                dispatch = _wrap_route(self, handler, middlewares, meta,
                                       cache_spec=spec, limit_spec=limit_spec,
                                       route_method=method, route_path=path)
                if route_env is not None:
                    dispatch = _with_route_env(dispatch, route_env)
                method_key = method.upper()
                dynamic_index = (
                    len(self.router.dynamic_routes[method_key])
                    if _is_dynamic_path(path) else None
                )
                _stash_route_reg(
                    handler, owner=self, router=None, method=method,
                    path=path, mws=middlewares, meta=meta,
                    route_env=route_env, router_map=self.router,
                    dynamic_index=dynamic_index, routes_slot=None,
                )
                self.router.add_route(method, path, dispatch, middlewares)
                _record_docs(handler, meta)
                return handler
            return decorator

        handler = rest[-1]
        route_mws = middlewares + rest[:-1]
        meta = _handler_meta(handler)
        spec = getattr(handler, "__flameo_cache_spec__", None)
        limit_spec = getattr(handler, "__flameo_limit_spec__", None)
        if spec is not None:
            _warn_if_uncacheable(method)
        dispatch = _wrap_route(self, handler, route_mws, meta,
                               cache_spec=spec, limit_spec=limit_spec,
                               route_method=method, route_path=path)
        if route_env is not None:
            dispatch = _with_route_env(dispatch, route_env)
        method_key = method.upper()
        dynamic_index = (
            len(self.router.dynamic_routes[method_key])
            if _is_dynamic_path(path) else None
        )
        _stash_route_reg(
            handler, owner=self, router=None, method=method, path=path,
            mws=route_mws, meta=meta, route_env=route_env,
            router_map=self.router, dynamic_index=dynamic_index,
            routes_slot=None,
        )
        self.router.add_route(method, path, dispatch, route_mws)
        _record_docs(handler, meta)
        return handler

    def get(self, *args, **kwargs):
        return self.route("GET", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self.route("POST", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self.route("PUT", *args, **kwargs)

    def patch(self, *args, **kwargs):
        return self.route("PATCH", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self.route("DELETE", *args, **kwargs)

    def options(self, *args, **kwargs):
        return self.route("OPTIONS", *args, **kwargs)

    def head(self, *args, **kwargs):
        return self.route("HEAD", *args, **kwargs)

    def query(self, *args, **kwargs):
        return self.route("QUERY", *args, **kwargs)

    async def run_cpu_task(self, func, *args):
        return await self.cpu_executor.run(func, *args)

    async def __call__(self, scope, receive, send=None):
        if scope['type'] == "lifespan":
            await self.lifespan(scope, receive, send)
            return

        if self._shutting_down:
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"text/plain")]
            })
            await send({"type": "http.response.body", "body": b"Server shutting down"})
            return

        req = Request(scope, receive, _max_body_size, self.trust_ip)
        req._app = self
        res = Response(send, app=self, req=req)

        try:
            try:
                await self._handle_asgi_hot(req, res)
            finally:
                req._res = None
        except Exception as e:
            if self.debug:
                # Print the real exception (not ``repr(e)`` which for
                # oracledb errors just yields
                # ``<oracledb.errors._Error object at 0x...>`` and hides
                # the ORA-NNNNN code). Write to stderr + flush so the
                # message survives stdout capture by the ASGI server.
                import traceback, sys
                tb_text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                code = getattr(e, "code", None)
                sys.stderr.write(
                    f"[flameo] {type(e).__module__}.{type(e).__name__}: {e}\n"
                )
                if code is not None:
                    sys.stderr.write(f"[flameo] oracledb.code = {code}\n")
                sys.stderr.write("[flameo] traceback:\n")
                sys.stderr.write(tb_text)
                sys.stderr.flush()

            if not res._response_started:
                try:
                    await res.send("Internal Server Error", status=500)
                except Exception:
                    pass
            elif not res._sent:
                # A response that has started cannot be replaced with a
                # new status. Best-effort close the existing body stream.
                try:
                    await send({
                        "type": "http.response.body",
                        "body": b"",
                    })
                except Exception:
                    pass

        # Logging is a single bool check + early-return when off; no
        # exception unwinding happens here.
        if self._show_requests:
            self._maybe_log_request(req.method, req.path, req._query_raw,
                                    res._status, res._sent_body_size)
        return

    def __rsgi_init__(self, loop):
        """Sync startup hook invoked by Granian-RSGI once per worker, before
        the listener accepts connections.

        Granian-RSGI does NOT speak the ASGI lifespan protocol, so the
        async ``init_db_pool()`` would otherwise sit un-called until the
        first request hit ``__rsgi__``. With a missing driver (e.g.
        ``asyncmy``) the user got the install hint only when they loaded
        ``/mysql`` — far too late to act on.

        Granian calls this method synchronously and does NOT await it,
        so we cannot drive the init to completion here. Instead we
        schedule ``init_db_pool()`` as a task on Granian's own event
        loop. The async pool objects (asyncpg / asyncmy / oracledb)
        bind to the loop they were created on, so the task MUST live on
        that exact loop — creating the pool on a side loop gives back
        "Event loop is closed" the moment a request tries to use it.
        ``__rsgi__`` awaits the task before handling requests, so any
        failure (missing driver, unreachable server, bad credentials)
        surfaces as the original error on the first request instead of
        staying silent.

        NB: do NOT block on the task here. The Granian-RSGI worker is
        already inside ``_spawn_rsgi_worker`` (an async fn) when this
        is called, and any sync wait on a not-yet-finished coroutine
        either deadlocks or — if the loop were a different one — would
        again put the pool on the wrong loop.
        """
        if self._rsgi_init_task__ is not None:
            return
        
        if self.db_config or self.cache_config:
            self._rsgi_init_task__ = loop.create_task(self._init_db_pool_async__())
        elif self._startup_handlers or self._temp_sweeper is not None:
            self._rsgi_init_task__ = loop.create_task(self._rsgi_startup_async__())

    async def _rsgi_startup_async__(self):
        await self._start_temp_sweeper()
        await self._run_startup_hooks()

    async def _init_db_pool_async__(self):
        
        try:
            await self.init_db_pool()
            await self.init_cache_pool()
            await self.run_cpu_task(_noop)
            await self._start_temp_sweeper()
            
            await self._run_startup_hooks()
        except BaseException:
            # Leave the task in its failed state so ``__rsgi__`` re-raises.
            raise

    async def __rsgi__(self, scope, receive=None):
        if self._rsgi_init_task__ is not None:
            
            try:
                await self._rsgi_init_task__
            finally:
                self._rsgi_init_task__ = None
        
        req = Request(scope, receive, _max_body_size, self.trust_ip)
        req._app = self
        
        res = RSGIResponse(req=req, proto=receive, app=self)
        

        try:
            try:
                await self._handle_asgi_hot(req, res)
            finally:
                req._res = None
        except Exception as e:
            if self.debug:
                import traceback, sys
                tb_text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                code = getattr(e, "code", None)
                sys.stderr.write(
                    f"[flameo] {type(e).__module__}.{type(e).__name__}: {e}\n"
                )
                if code is not None:
                    sys.stderr.write(f"[flameo] oracledb.code = {code}\n")
                sys.stderr.write("[flameo] traceback:\n")
                sys.stderr.write(tb_text)
                sys.stderr.flush()
            await res.send(b"Internal Server Error", status=500)

        if self._shutting_down:
            if self._active_requests:
                await asyncio.gather(*self._active_requests, return_exceptions=True)
            # Run user shutdown hooks before tearing the pool down, the
            # same way the ASGI lifespan shutdown event does.
            await self._run_shutdown_hooks()
            await self.shutdown_db_pool()
            await self.shutdown_cache_pool()
            await self.disable_temp_cleanup()

        
        if getattr(res, "_streamed", False):
            self._maybe_log_request(req.method, req.path, req._query_raw,
                                    res._status, len(res._body))
            return None

        # ``res._hdr`` is already native str pairs — granian's RSGI
        # requires str, so there is no decode round trip to do here.
        status, headers_str, body = res._into_bytes()

        encoding = None
        if self.enable_gzip:
            body, encoding = await self._maybe_compress_async(req, body)
        if encoding is not None:
            new_headers = [
                (k, v) for k, v in headers_str
                if k.lower() != "content-length"
                and k.lower() != "content-encoding"
                and k.lower() != "vary"
            ]
            new_headers.append(("content-length", str(len(body))))
            new_headers.append(("content-encoding", encoding))
            # Always advertise the response varies on Accept-Encoding
            # when we compressed — caches need it to avoid serving the
            # compressed body to a client that didn't ask for it.
            if not any(k.lower() == "vary" for k, _ in new_headers):
                new_headers.append(("vary", "Accept-Encoding"))
            headers_str = new_headers

        # Mirror ASGI: only log the request when logging is enabled, and
        # never as part of the response-bytes path. The `server` header
        # is omitted — the ASGI/RSGI host already announces itself.
        self._maybe_log_request(req.method, req.path, req._query_raw,
                                status, len(body))
        if hasattr(receive, "response_bytes"):
            receive.response_bytes(status=status, headers=headers_str, body=body)
        else:
            return status, headers_str, [body]


    async def _handle_asgi_hot(self, req, res):
            _finalize = getattr(res, "_finalize_session__", None)
            request_exc = None
            try:
                global_mws = self.global_middlewares
                if global_mws:
                    if (self._global_mw_metas_src is not global_mws
                            or len(self._global_mw_metas) != len(global_mws)):
                        self._global_mw_metas_src = global_mws
                        self._global_mw_metas = tuple(
                            _mw_meta(mw) for mw in global_mws
                        )
                    global_mw_metas = self._global_mw_metas
                    for _mw, _meta in zip(global_mws, global_mw_metas):
                        _qm = req._db_query_manager
                        if _meta[_MW_TAKES_QM]:
                            _qm = await _acquire_request_qm(req, self.DB_POOL)
                        await _call_middleware(
                            _mw, _meta, req, res,
                            qm=_qm, cache=_resolve_cache(self), app=self, state=self.state,
                        )
                        if res._sent:
                            return

                # Cached router lookup — single dict ``in`` on the warm path.
                # match() returns ``(dispatch, params, route_mws)``.
                dispatch, params, _route_mws = self.router.match(req.method, req.path)
                if params:
                    req.params = params

                # Single conditional on dispatch being None handles both
                # "no route registered" and "no static middleware configured".
                # The wrapper built by _wrap_route for a registered route
                # already handles middleware iteration + qm + pydantic.
                if dispatch is None:
                    static = self.static_middleware__
                    if static:
                        await static(req, res)
                        # ``serve_static`` returns ``True`` when it handled the
                        # request and ``False`` when the path didn't fall under
                        # its URL prefix. If we got here and the response
                        # wasn't sent, fall through to the 404 path; otherwise
                        # the file was already streamed.
                        if res._sent:
                            return
                    await self._send_default_not_found(req, res)
                    return

                await dispatch(req, res)
                if not res._sent:
                    await res.send("No response set by handler", status=500)

            except BaseException as exc:
                request_exc = exc
                raise
            finally:
                if _finalize is not None:
                    try:
                        _finalize()
                    except Exception:
                        pass
                try:
                    await _release_request_qm(req, request_exc)
                finally:
                    # Drain any res.finish() callbacks. Covers the case
                    # where a handler raised BEFORE reaching res.send/json
                    # (so the in-_send_body drain never fired) -- the
                    # finishers still run via ensure_future. No-op when
                    # _send_body already drained (idempotent: we pop the
                    # list on first drain).
                    if res._finishers__:
                        _drain_finishers(res)

    async def _send_default_not_found(self, req, res):
        """404 response.

        Order of precedence:
          1. User-supplied ``app.not_found(handler)`` — full control.
          2. Built-in ``Cannot <METHOD> <path>`` text body with
             ``Content-Type: text/html; charset=utf-8`. Wrapped in a tiny HTML doc
             so curl / browsers render it sensibly.

        ``res._sent`` is honored at the entry so a misbehaving custom
        handler that forgot to end the response still produces a
        response — we never double-send.
        """
        if res._sent:
            return
        custom = self.not_found_handler__
        if custom is not None:
            try:
                await custom(req, res)
            except Exception:
                # If the user handler blew up, fall back to the
                # built-in text so the client never sees a hung socket.
                pass
            if res._sent:
                return

        method = getattr(req, "method", "GET") or "GET"
        path = getattr(req, "path", "/") or "/"
        body = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\">\n"
            f"  <title>Cannot {method} {path}</title>\n"
            "</head>\n"
            "<body>\n"
            "  <pre>Cannot " + method + " " + path + "</pre>\n"
            "</body>\n"
            "</html>\n"
        ).encode("utf-8")

        if hasattr(res, "setHeader"):
            res.setHeader("X-Content-Type-Options", "nosniff")
        # Use the internal ``_send_body`` so we can pin both
        # ``Content-Type`` and the raw bytes without going through
        # ``res.send`` (which would re-encode as text/plain and ignore
        # a content_type kwarg that doesn't exist on that signature).
        await res._send_body(
            body,
            b"text/html; charset=utf-8",
            404,
        )

    async def lifespan(self, scope, receive, send):
        while True:
            event = await receive()
            if event['type'] == "lifespan.startup":
                await self.init_db_pool()
                await self.init_cache_pool()
                await self.run_cpu_task(_noop)
                # Cleanup is enabled by default; if the sweeper was
                # configured before the loop started, start it now so
                # its startup pass also covers leftovers.
                await self._start_temp_sweeper()
                await self._run_startup_hooks()
                await send({"type": "lifespan.startup.complete"})
            elif event['type'] == "lifespan.shutdown":
                self._shutting_down = True
                if self._active_requests:
                    await asyncio.gather(*self._active_requests, return_exceptions=True)
                # User shutdown hooks first — they may still want the DB
                # pool alive (e.g. flush a buffer, close a channel) so
                # they run *before* ``shutdown_db_pool``.
                await self._run_shutdown_hooks()
                await self.shutdown_db_pool()
                await self.shutdown_cache_pool()
                # Cancels the sweeper task. No-op if cleanup was
                # explicitly disabled.
                await self.disable_temp_cleanup()
                await send({"type": "lifespan.shutdown.complete"})
                break
            
class Router:
    """Modular route group, mounted onto a parent app via ``app.use_router``.
    
    Runs on top  Flameo's middleware / route pipeline:

      - ``router.get/post/put/patch/delete/options/head/query`` and the
        generic ``router.route(method, ...)`` accept the same
        decorator- and imperative-style signatures as the matching
        methods on :class:`Flameo`.
      - ``router.use(middleware)`` registers a path-scoped middleware
        that fires only after the router has been mounted via
        ``app.use_router(prefix, router)``.

    The router is *passive* on its own: calling ``router.get(...)``
    just stores the route on the router's internal ``RouterMap``; no
    route is registered on any app until ``use_router`` consumes it.

    Example::

        # routes.py
        from flameo import Router

        router = Router()

        @router.get("/")
        async def root(req, res):
            await res.json({"ok": True})

        @router.get("/users/:id")
        async def get_user(req, res):
            await res.json({"id": req.params["id"]})

        async def log_request(req, res):
            print("hit:", req.method, req.path)

        router.use(log_request)

        # main.py
        from flameo import Flameo
        from routes import router

        app = Flameo()
        app.use_router("/api", router)
        # GET /api/         -> {"ok": True}
        # GET /api/users/7  -> {"id": "7"}
        # GET /anything-else falls through to other middlewares / 404;
        #   ``router.use`` middlewares do NOT fire for non-matching paths.
    """

    def __init__(self, **kwargs) -> None:
        self.title = kwargs.get("title")
        self.description = kwargs.get("description")
        # Local router — only consumed by ``app.use_router``. Holds
        # the user's paths as-written, *without* any mount prefix.
        # Re-registration on the parent app prefixes them.
        self._router_map = RouterMap()
        # Parallel ordered list of ``(method, path, dispatch, mws)``
        # tuples. RouterMap's dynamic_routes side stores compiled
        # regexes and drops the original path string, so we keep
        # our own copy here so ``_iter_routes`` can yield the
        # original path with parameters intact.
        self._routes: list = []
        # Each entry is a middleware callable (async or sync).
        self._middlewares: list = []
        # Documentation records; consumed by ``app.use_router`` (which
        # prefixes the paths and tags them with this router's title).
        self._docs_registry = DocsRegistry()
        self._flameo_mounted__ = False

    def cache(self, ttl=None):
        """Cache a router endpoint response for ``ttl`` seconds.

        Example::

            @router.cache(ttl=100)
            @router.get("/items")
            async def items(req, res): ...
        """
        return _cache_decorator(None, self, ttl, "router")

    def limit(self, count, seconds, key_function=None, *, key_func=None):
        """Limit requests to a router endpoint within a fixed time window.

        ``key_function`` (also accepted as ``key_func``) receives the
        request and returns the value used to group requests. Without it,
        all requests to this endpoint share one limit.
        """
        if key_function is not None and key_func is not None:
            raise ValueError("[flameo] provide only one of key_function or key_func")
        return _limit_decorator(
            None, self, count, seconds,
            key_function if key_function is not None else key_func, "router",
        )

    # ---------------------------------------------------------------
    # route() + method shortcuts — mirror Flameo.route().
    # ---------------------------------------------------------------

    def route(self, method, *args, middlewares=None, filters=None,
              summary=None, description=None, tags=None, responses=None,
              query=None, headers=None, deprecated=False, operation_id=None):
        """Same two calling forms as ``Flameo.route``::

            @router.route("GET", "/users/:id")
            async def get_user(req, res): ...

            router.route("GET", "/users/:id", get_user)

        ``filters=`` registers template filters that are available only to
        the templates this route renders. The documentation kwargs
        (``summary``, ``tags``, ``responses``, ``query``, ``headers``,
        ...) behave exactly as on ``Flameo.route``.
        """
        if not args:
            raise ValueError("At least a path must be provided")

        path = args[0]
        middlewares = list(middlewares) if middlewares else []
        rest = list(args[1:])

        route_env = RouteTemplateEnv(filters) if filters else None

        def _record_docs(handler, meta):
            self._docs_registry.add(RouteDoc(
                method=method,
                path=path,
                handler=handler,
                body_model=meta[_META_ANNOTATION] if meta[_META_IS_PYDANTIC] else None,
                summary=summary,
                description=description,
                tags=tags,
                responses=responses,
                query=query,
                headers=headers,
                deprecated=deprecated,
                operation_id=operation_id,
            ))

        if len(rest) == 0:
            def decorator(handler):
                meta = _handler_meta(handler)
                spec = getattr(handler, "__flameo_cache_spec__", None)
                limit_spec = getattr(handler, "__flameo_limit_spec__", None)
                if spec is not None:
                    _warn_if_uncacheable(method)
                dispatch = _wrap_route(None, handler, middlewares, meta,
                                       cache_spec=spec, limit_spec=limit_spec,
                                       route_method=method, route_path=path)
                if route_env is not None:
                    dispatch = _with_route_env(dispatch, route_env)
                method_key = method.upper()
                dynamic_index = (
                    len(self._router_map.dynamic_routes[method_key])
                    if _is_dynamic_path(path) else None
                )
                routes_index = len(self._routes)
                _stash_route_reg(
                    handler, owner=None, router=self, method=method,
                    path=path, mws=middlewares, meta=meta,
                    route_env=route_env, router_map=self._router_map,
                    dynamic_index=dynamic_index,
                    routes_slot=(self._routes, routes_index),
                )
                self._router_map.add_route(method, path, dispatch, middlewares)
                self._routes.append((method, path, dispatch, middlewares))
                _record_docs(handler, meta)
                return handler
            return decorator

        handler = rest[-1]
        route_mws = middlewares + rest[:-1]
        meta = _handler_meta(handler)
        spec = getattr(handler, "__flameo_cache_spec__", None)
        limit_spec = getattr(handler, "__flameo_limit_spec__", None)
        if spec is not None:
            _warn_if_uncacheable(method)
        dispatch = _wrap_route(None, handler, route_mws, meta,
                               cache_spec=spec, limit_spec=limit_spec,
                               route_method=method, route_path=path)
        if route_env is not None:
            dispatch = _with_route_env(dispatch, route_env)
        method_key = method.upper()
        dynamic_index = (
            len(self._router_map.dynamic_routes[method_key])
            if _is_dynamic_path(path) else None
        )
        routes_index = len(self._routes)
        _stash_route_reg(
            handler, owner=None, router=self, method=method, path=path,
            mws=route_mws, meta=meta, route_env=route_env,
            router_map=self._router_map, dynamic_index=dynamic_index,
            routes_slot=(self._routes, routes_index),
        )
        self._router_map.add_route(method, path, dispatch, route_mws)
        self._routes.append((method, path, dispatch, route_mws))
        _record_docs(handler, meta)
        return handler

    def get(self, *args, **kwargs):
        return self.route("GET", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self.route("POST", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self.route("PUT", *args, **kwargs)

    def patch(self, *args, **kwargs):
        return self.route("PATCH", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self.route("DELETE", *args, **kwargs)

    def options(self, *args, **kwargs):
        return self.route("OPTIONS", *args, **kwargs)

    def head(self, *args, **kwargs):
        return self.route("HEAD", *args, **kwargs)

    def query(self, *args, **kwargs):
        return self.route("QUERY", *args, **kwargs)

    # ---------------------------------------------------------------
    # Middleware registration — mirrors Flameo.use().
    # ---------------------------------------------------------------

    def use(self, middleware) -> None:
        """Register a middleware on this router.

        Fires *only* after the router is mounted via
        ``app.use_router(prefix, router)`` *and* the incoming request
        starts with ``prefix``. Recorded middlewares run in
        registration order, before any of the router's routes.
        """
        if not callable(middleware):
            raise TypeError(
                "[flameo] router.use() expects a callable (async or sync "
                "middleware accepting (req, res) plus optional db/app/state)."
            )
        _mw_meta(middleware)
        self._middlewares.append(middleware)

    # ---------------------------------------------------------------
    # Internal API consumed by Flameo.use_router.
    # ---------------------------------------------------------------

    def _iter_routes(self):
        """Yield ``(method, path, dispatch, middlewares)`` for every route.

        ``path`` is exactly as registered on the router — without any
        mount prefix. The caller is responsible for prefixing before
        adding to a parent ``RouterMap``.

        Order is registration order, which is more useful for error
        messages than the per-method/dict iteration order that
        ``RouterMap.static_routes.items()`` would yield.
        """
        for entry in self._routes:
            yield entry

    def _iter_middlewares(self) -> list:
        """Return a snapshot of registered middlewares (defensive copy).

        Named ``_iter_middlewares`` (not ``_middlewares``) to avoid
        shadowing the ``self._middlewares`` attribute on this class.
        """
        return list(self._middlewares)


class DBType(Enum):
    PostgreSQL = 1
    MySQL = 2
    Oracle = 3
    SQLite = 4
    MongoDB = 5

class CacheType(Enum):
    Redis = 1
    Valkey = 2
    Dragonfly = 3


_base64 = None


def _resolve_upload_path(directory: str, name: str) -> Path:
    root = os.path.realpath(directory)
    try:
        target = os.path.realpath(os.path.join(root, os.path.normpath(str(name))))
        inside = os.path.commonpath([root, target]) == root
    except (OSError, ValueError):
        inside = False
        target = None
    if not inside:
        raise ValueError("[flameo] upload name must stay inside the upload directory")
    return Path(target)


class File:
    """A single uploaded file from a multipart/form-data request.

    * ``filename``     -- the name the browser sent (``"photo.png"``).
    * ``new_name``     -- what we'll save as; defaults to ``filename``
      until the caller picks another via :meth:`rename_as`.
    * ``content_type`` -- the part's ``Content-Type`` header.
    * ``field_name``   -- the form input name (``"photo"``).
    * ``size``         -- byte size of the body (or ``None`` if unknown).
    * ``content``      -- bytes for uploads up to 1 MiB. Larger uploads
      are streamed to a temp file and expose ``None`` here; call
      :meth:`upload_to` to persist either form.
    """

    def __init__(
        self,
        file_content=None,
        temp_path: Optional[Path] = None,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        field_name: Optional[str] = None,
        size: Optional[int] = None,
    ):
        self.content = file_content        
        self.temp_path: Optional[Path] = temp_path
        self.filename = filename
        self.new_name = filename
        self.content_type = content_type
        self.field_name = field_name
        self.size: Optional[int] = size if size is not None else (
            len(file_content) if isinstance(file_content, (bytes, bytearray)) else 0
        )
        self.save_dir: Optional[str] = None
        self._tracker = None

    async def base64_to_file(self):
        global _base64
        if _base64 is None:
            import base64
            _base64 = base64
        self.content = await asyncio.to_thread(_base64.b64decode, self.content)
        return self

    def rename_as(self, new_name: str):
        self.new_name = new_name
        return self

    def _drop_temp(self) -> None:
        if self.temp_path is None:
            return
        try:
            if self.temp_path.exists():
                self.temp_path.unlink()
        except OSError:
            # Swallow -- the caller cannot meaningfully react.
            pass

    def _unregister(self) -> None:
        tracker = getattr(self, "_tracker", None)
        if tracker is None:
            return
        try:
            tracker.remove(self)
        except ValueError:
            pass
        self._tracker = None

    def _discard(self) -> None:
        self._drop_temp()
        self.temp_path = None
        self.content = None
        self._unregister()

    async def upload_to(self, directory: str, name: Optional[str] = None, chunk_size: int = 64 * 1024, **kwargs):
        
        if not directory:
            raise ValueError("Upload directory not specified. Use .upload_to() first.")
        if not (os.path.exists(directory) and os.path.isdir(directory)):
            raise ValueError(f'Directory "{directory}" not exists')

        self.save_dir = directory
        
        chosen_name = name if name is not None else self.new_name
        if not chosen_name:
            raise NameError("No name assigned for uploaded file.")
        file_path = _resolve_upload_path(directory, chosen_name)

        try:
            import aiofiles
        except ImportError:
            raise ImportError(
                "You need to install aiofiles to upload files.\n\n"
                "pip install aiofiles"
            )

        if self.temp_path is not None:
            try:
                os.replace(self.temp_path, file_path)
            except OSError as exc:
                if getattr(exc, "errno", None) != 18:  # not EXDEV
                    raise
                
                await _chunked_copy(self.temp_path, file_path, chunk_size)
            
            self._drop_temp()
            self.temp_path = None
            self._unregister()
            return file_path

        if not kwargs.get("replace", False) and file_path.exists():
            raise ValueError(f"File {file_path} already exists")
        async with aiofiles.open(file_path, "wb") as f:
            if self.content is None:
                self._unregister()
                return file_path
            offset = 0
            data = self.content
            total = len(data)
            while offset < total:
                await f.write(data[offset : offset + chunk_size])
                offset += chunk_size
        self._unregister()
        return file_path

    def __repr__(self) -> str:
        size = self.size
        if size is None and self.content is not None:
            size = len(self.content)
        return (
            f"File(filename={self.filename!r}, "
            f"content_type={self.content_type!r}, "
            f"size={size})"
        )

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, _handler):
        from pydantic_core import core_schema
        return core_schema.no_info_plain_validator_function(
            cls._pydantic_validate,
        )

    @classmethod
    def _pydantic_validate(cls, value):
        """Runtime validator used by the pydantic core schema.
        Accepts a ``File`` instance or ``None``. Raises a ``ValueError`` for
        any other type.
        """
        if value is None or isinstance(value, cls):
            return value
        raise ValueError(
            f"[flameo] Expected flameo.flameo.File instance, got "
            f"{type(value).__name__}"
        )


async def _chunked_copy(src: Path, dst: Path, chunk_size: int) -> None:
    """Copy ``src`` to ``dst`` in ``chunk_size`` bytes -- never loads the
    whole file into RAM. Runs the blocking ``shutil.copyfileobj`` on a
    worker thread so the asyncio event loop stays free.
    """
    def _do_copy():
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=chunk_size)

    await asyncio.to_thread(_do_copy)