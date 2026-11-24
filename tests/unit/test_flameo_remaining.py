from types import SimpleNamespace
import builtins
import sys

import pytest

from flameo import Router
from flameo.flameo import (
    QueryManager,
    _SQLiteAcquireContext,
    _SQLitePool,
    _cache_decorator,
    _handler_meta,
    _make_dispatch,
    _make_dispatch_pydantic,
    _make_router_middleware,
    _mw_meta,
    _noop,
    _stash_route_reg,
)


def test_small_module_helpers_and_meta_none():
    assert _noop() is None
    assert _handler_meta(None) is None
    assert _mw_meta(None) is None
    _stash_route_reg(len, route="ignored")
    def handler(req, res):
        pass
    _stash_route_reg(handler, route="first")
    _stash_route_reg(handler, route="second")


def test_handler_and_middleware_metadata_eviction():
    from flameo import flameo as framework

    framework._HANDLER_META.clear()
    framework._MIDDLEWARE_META.clear()
    handlers = []
    middlewares = []
    for index in range(4098):
        def make_handler(value):
            def handler(req, res, app):
                return value
            return handler
        handler = make_handler(index)
        handlers.append(handler)
        handler.__name__ = f"handler_{index}"
        _handler_meta(handler)
    for index in range(4098):
        def make_middleware(value):
            def middleware(req, res, app):
                return value
            return middleware
        middleware = make_middleware(index)
        middlewares.append(middleware)
        middleware.__name__ = f"middleware_{index}"
        _mw_meta(middleware)
    assert _handler_meta(handlers[-1]) == _handler_meta(handlers[-1])
    assert _mw_meta(middlewares[-1]) == _mw_meta(middlewares[-1])


def test_cache_decorator_validates_and_warns():
    with pytest.raises(TypeError, match="requires a ttl"):
        _cache_decorator(None, None, lambda: None, "app")
    with pytest.raises(ValueError, match="positive number"):
        _cache_decorator(None, None, 0, "app")
    with pytest.raises(ValueError, match="positive number"):
        _cache_decorator(None, None, True, "app")

    def handler():
        pass
    handler.__flameo_route_reg__ = [{"router": object()}]

    with pytest.warns(UserWarning, match="different app"):
        _cache_decorator(None, object(), 1, "app")(handler)
    with pytest.raises(TypeError, match="expects a function"):
        _cache_decorator(None, None, 1, "app")(object())


def test_cache_config_validation(app_factory):
    from flameo import CacheType

    base = {"cache_type": CacheType.Redis, "max_connections": 1,
            "cache_url": "redis://cache"}
    with pytest.raises(ValueError, match="cache_config must be a dict"):
        app_factory(cache_config=[])
    with pytest.raises(ValueError, match="requires"):
        app_factory(cache_config={})
    with pytest.raises(ValueError, match="must be a CacheType"):
        app_factory(cache_config={**base, "cache_type": "redis"})
    with pytest.raises(ValueError, match="positive integer"):
        app_factory(cache_config={**base, "max_connections": 0})
    with pytest.raises(ValueError, match="non-empty string"):
        app_factory(cache_config={**base, "cache_url": ""})


@pytest.mark.asyncio
async def test_non_pydantic_dispatch_injects_slots_and_middleware(app_factory):
    calls = []

    async def middleware(req, res, app):
        calls.append(app)

    async def handler(req, res, app):
        calls.append(app)
        return "done"

    app = app_factory()
    meta = _handler_meta(handler)
    dispatch = _make_dispatch(app, handler, [middleware], meta)
    req = SimpleNamespace(app=app, _db_query_manager=None, _db_pool=None,
                          _db_acquire_context=None)
    result = await dispatch(req, object())
    assert result == "done"
    assert calls == [app, app]


@pytest.mark.asyncio
async def test_cached_dispatch_paths_and_pydantic_no_cache(app_factory, asgi_client):
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str

    app = app_factory()
    seen = []

    async def route_middleware(req, res):
        seen.append("middleware")

    @app.get("/cached", middlewares=[route_middleware])
    @app.cache(ttl=10)
    async def cached(req, res, app):
        seen.append("handler")
        await res.send("cached")

    first = await asgi_client(app, path="/cached")
    second = await asgi_client(app, path="/cached")
    assert first["body"] == second["body"] == b"cached"
    assert seen == ["middleware", "handler", "middleware"]

    @app.post("/model", middlewares=[route_middleware])
    async def model(data: Item, res, app):
        await res.send(data.name)

    result = await asgi_client(
        app, "POST", "/model",
        headers={"Content-Type": "application/json"}, body=b'{"name":"ok"}',
    )
    assert result["body"] == b"ok"

    @app.post("/model-db")
    async def model_db(data: Item, res, db):
        raise AssertionError("not reached")

    result = await asgi_client(
        app, "POST", "/model-db",
        headers={"Content-Type": "application/json"}, body=b'{"name":"ok"}',
    )
    assert result["status"] == 503

    @app.get("/cached-db-state")
    @app.cache(ttl=1)
    async def cached_db_state(req, res, db, state):
        raise AssertionError("not reached")

    result = await asgi_client(app, path="/cached-db-state")
    assert result["status"] == 503

    with pytest.warns(UserWarning):
        @app.post("/cached-model-simple")
        @app.cache(ttl=1)
        async def cached_model_simple(data: Item, res):
            await res.send(data.name)

    result = await asgi_client(
        app, "POST", "/cached-model-simple",
        headers={"Content-Type": "application/json"}, body=b'{"name":"simple"}',
    )
    assert result["body"] == b"simple"

    with pytest.warns(UserWarning):
        @app.post("/warn-cache")
        @app.cache(ttl=1)
        async def warn_cache(req, res):
            await res.send("ok")


@pytest.mark.asyncio
async def test_dispatch_db_state_without_pool_sends_503(app_factory):
    async def handler(req, res, db, state):
        raise AssertionError("not reached")

    class Response:
        def __init__(self):
            self.sent = None

        async def send(self, body, status=None):
            self.sent = (body, status)

    app = app_factory()
    req = SimpleNamespace(app=app, _db_query_manager=None, _db_pool=None,
                          _db_acquire_context=None)
    response = Response()
    await _make_dispatch(app, handler, [], _handler_meta(handler))(req, response)
    assert response.sent == ("Database not available", 503)


@pytest.mark.asyncio
async def test_router_scoped_middleware_sets_base_url():
    seen = []

    async def middleware(req, res):
        seen.append(req._base_url)

    app = SimpleNamespace(DB_POOL=None, CACHE=None, _memory_cache=object(), state=None)
    req = SimpleNamespace(path="/api/item", app=app, _db_query_manager=None,
                          _db_pool=None, _db_acquire_context=None)
    await _make_router_middleware("/api", middleware)(req, object())
    assert seen == ["/api"]
    root_req = SimpleNamespace(path="/", app=app, _db_query_manager=None,
                               _db_pool=None, _db_acquire_context=None,
                               _base_url=None)
    await _make_router_middleware("/", middleware)(root_req, object())
    assert seen == ["/api", None]


@pytest.mark.asyncio
async def test_pydantic_dispatch_reuses_request_query_manager():
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str

    class Response:
        def __init__(self):
            self.body = None

        async def send(self, body, status=None):
            self.body = body

    async def handler(data: Item, res, db):
        assert data.name == "x"
        assert db == "qm"
        await res.send("ok")

    app = SimpleNamespace(
        validate_case_sensitive=True, DB_POOL=object(), CACHE=None,
        _memory_cache=object(), state=None,
    )
    req = SimpleNamespace(
        app=app, body=SimpleNamespace(), _db_query_manager="qm",
        _db_pool=app.DB_POOL, _db_acquire_context=None,
    )
    async def body():
        return {"name": "x"}
    req.body = body()
    response = Response()
    dispatch = _make_dispatch_pydantic(app, handler, [], _handler_meta(handler))
    await dispatch(req, response)
    assert response.body == "ok"


@pytest.mark.asyncio
async def test_sqlite_pool_context_and_query_manager_tails(monkeypatch, tmp_path):
    import aiosqlite
    import flameo.flameo as framework
    from flameo import DBType

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    pool = _SQLitePool(str(tmp_path / "db.sqlite"))
    context = pool.acquire()
    assert isinstance(context, _SQLiteAcquireContext)
    async with context as connection:
        manager = QueryManager(connection)
        await manager.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
        assert await manager.fetchrow("SELECT * FROM items") is None
        with pytest.raises(TypeError, match="must be an integer"):
            await manager.fetch("SELECT * FROM items", batch_size="1")
        assert await manager.execute_returning("SELECT * FROM items") == {}
        assert manager.total_changes == connection.total_changes
        await manager.close()
        await manager.close()
    await pool.close()


@pytest.mark.asyncio
async def test_router_aliases_mount_and_scoped_middleware(app_factory, asgi_client):
    router = Router(title="api")
    seen = []

    async def middleware(req, res):
        seen.append(req.path)

    router.use(middleware)
    methods = ("get", "post", "put", "patch", "delete", "options", "head", "query")
    def make_handler(value):
        async def handler(req, res):
            await res.send(value)
        return handler

    for method in methods:
        getattr(router, method)(f"/{method}", make_handler(method))

    app = app_factory()
    app.use_router("/api/", router)
    result = await asgi_client(app, path="/api/get")
    assert result["body"] == b"get"
    assert seen == ["/api/get"]
    assert (await asgi_client(app, path="/outside/get"))["status"] == 404
    assert seen == ["/api/get"]
    with pytest.raises(ValueError, match="At least a path"):
        Router().route("GET")
    with pytest.raises(TypeError, match="callable"):
        Router().use(123)


def test_router_mount_validation_and_duplicate(app_factory):
    app = app_factory()
    with pytest.raises(TypeError, match="prefix must be a string"):
        app.use_router(123, Router())
    with pytest.raises(TypeError, match="Router instance"):
        app.use_router("/api", object())
    router = Router()
    app.use_router("/", router)
    with pytest.raises(ValueError, match="already mounted"):
        app.use_router("", router)


@pytest.mark.asyncio
async def test_router_root_mount_and_prefix_forms(app_factory, asgi_client):
    router = Router()

    @router.get("/")
    async def root(req, res):
        await res.send("ok")

    app = app_factory()
    app.use_router("/", router)
    result = await asgi_client(app, path="/")
    assert result["status"] == 200


def test_optional_compressor_modules_and_streams(app_factory, monkeypatch):
    class ZstdCompressor:
        def __init__(self, level):
            self.level = level

        def compress(self, data):
            return b"z" + data

        def compressobj(self):
            return SimpleNamespace(
                compress=lambda data: b"z" + data,
                flush=lambda mode: b"!",
            )

    class BrotliCompressor:
        def __init__(self, quality):
            self.quality = quality

        def process(self, data):
            return b"b" + data

        def finish(self):
            return b"!"

    zstd = SimpleNamespace(
        ZstdCompressor=ZstdCompressor,
        COMPRESSOBJ_FLUSH_FINISH=1,
    )
    brotli = SimpleNamespace(
        compress=lambda data, quality: b"b" + data,
        Compressor=BrotliCompressor,
    )
    monkeypatch.setitem(sys.modules, "zstandard", zstd)
    monkeypatch.setitem(sys.modules, "brotli", brotli)
    app = app_factory()
    app.use_compression(min_size=1)
    assert app.zstd_compress(b"x") == b"zx"
    assert app.brotli_compress(b"x") == b"bx"
    _, zfactory = app.stream_compress_factory(
        SimpleNamespace(headers={"accept-encoding": "zstd"}),
        "text/plain", 10,
    )
    zcompress, zfinish, _ = zfactory()
    assert zcompress(b"x") == b"zx"
    assert zfinish() == b"!"
    _, bfactory = app.stream_compress_factory(
        SimpleNamespace(headers={"accept-encoding": "br"}),
        "text/plain", 10,
    )
    bcompress, bfinish, _ = bfactory()
    assert bcompress(b"x") == b"bx"
    assert bfinish() == b"!"

    real_import = builtins.__import__

    def no_brotli(name, *args, **kwargs):
        if name == "brotli":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_brotli)
    monkeypatch.setitem(sys.modules, "brotlicffi", brotli)
    fallback = app_factory()
    fallback._init_compressors()
    assert fallback._brotli_module is brotli


def test_stream_compressor_strips_zlib_sync_suffix(app_factory, monkeypatch):
    import zlib

    class Compressor:
        def compress(self, data):
            return b"body"

        def flush(self, mode):
            return b"tail\x00\x00\xff\xff"

    monkeypatch.setattr(zlib, "compressobj", lambda *args, **kwargs: Compressor())
    app = app_factory()
    compress, finalize, _ = app.stream_gzip_factory()
    compress(b"x")
    assert finalize() == b"tail" + zlib.crc32(b"x").to_bytes(4, "little") + b"\x01\x00\x00\x00"
    _, finalize, _ = app.stream_deflate_factory()
    assert finalize() == b"tail"


@pytest.mark.asyncio
async def test_pydantic_cached_dispatch_with_app_slot(app_factory, asgi_client):
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str

    app = app_factory()
    calls = []

    with pytest.warns(UserWarning):
        @app.post("/cached-model")
        @app.cache(ttl=10)
        async def cached(data: Item, res, app):
            calls.append(data.name)
            await res.json({"name": data.name})

    first = await asgi_client(
        app, "POST", "/cached-model",
        headers={"Content-Type": "application/json"}, body=b'{"name":"x"}',
    )
    second = await asgi_client(
        app, "POST", "/cached-model",
        headers={"Content-Type": "application/json"}, body=b'{"name":"x"}',
    )
    assert first["body"] == second["body"] == b'{"name":"x"}'
    assert calls == ["x"]


def test_router_cache_mount_and_docs_edges(app_factory):
    router = Router(title="widgets", description="Widget routes")

    @router.get("/item/:id", summary="item")
    async def item(req, res):
        pass

    router.cache(ttl=1)(item)
    app = app_factory(docs_url="/docs")
    app.use_router("/api", router)
    with pytest.raises(ValueError, match="use_router"):
        router.cache(ttl=1)(item)

    collision = Router()

    @collision.get("/docs")
    async def docs(req, res):
        pass

    with pytest.raises(ValueError, match="collides"):
        app_factory(docs_url="/docs").use_router("/", collision)
    app_factory(docs_url="/docs").use_router("/empty", Router())


def test_router_route_forms_and_post_cache_warning():
    router = Router()
    def identity(value):
        return value

    @router.route("GET", "/filtered", filters=[identity])
    async def filtered(req, res):
        pass
    router.cache(ttl=1)(filtered)

    async def posted(req, res):
        pass

    router.route("POST", "/posted", posted)
    with pytest.warns(UserWarning):
        router.cache(ttl=1)(posted)
    async def imperative_cached(req, res):
        pass
    router.cache(ttl=1)(imperative_cached)
    with pytest.warns(UserWarning):
        router.route("POST", "/imperative-cached", imperative_cached)

    with pytest.warns(UserWarning):
        @router.route("POST", "/decorated")
        @router.cache(ttl=1)
        async def decorated(req, res):
            pass

    @router.route("GET", "/env", filters=[identity])
    async def env_route(req, res):
        pass
    async def imperative_env(req, res):
        pass
    router.route("GET", "/imperative-env", imperative_env, filters=[identity])
