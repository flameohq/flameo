"""Request-lifecycle and response-helper tests for ``flameo/flameo.py``.

Slice: ``_bind_context``-adjacent dispatch paths (middleware short-circuit,
``db``/``state`` 503, pydantic 422 envelopes, 500 envelopes), auth-header
handling, session commit/skip, temp-dir cleanup hooks, gzip/compression
helpers, static-file serving (validators, 304, traversal, HEAD), lifespan
startup/shutdown and background finishers. All in-process via ``asgi_client``
/ ``rsgi_client``; pure helpers are called directly.
"""

import asyncio
import gzip as _gzip
import json
import os
import stat
import sys
import warnings
import zlib
from types import SimpleNamespace

import pytest

from flameo.flameo import CacheType
from flameo.cycles import Request, Response


def _forty_two():
    return 40 + 2


# ---------------------------------------------------------------------------
# auth_header kwarg + OpenAPI security passthrough
# ---------------------------------------------------------------------------


def test_auth_header_true_maps_to_authorization(app_factory):
    app = app_factory(auth_header=True)
    assert app.auth_header == "Authorization"


def test_auth_header_custom_name(app_factory):
    app = app_factory(auth_header="X-API-Key")
    assert app.auth_header == "X-API-Key"


def test_auth_header_invalid_type_raises(app_factory):
    with pytest.raises(ValueError, match="auth_header"):
        app_factory(auth_header=123)


@pytest.mark.asyncio
async def test_openapi_spec_includes_security_when_auth_header(app_factory, asgi_client):
    app = app_factory(docs_url="/docs", auth_header=True)

    @app.get("/ping")
    async def ping(req, res):
        await res.send("pong")

    result = await asgi_client(app, path="/openapi.json")
    assert result["status"] == 200
    spec = json.loads(result["body"])
    assert spec["components"]["securitySchemes"]["authHeader"]["name"] == "Authorization"
    assert spec["security"] == [{"authHeader": []}]


@pytest.mark.asyncio
async def test_openapi_spec_omits_security_without_auth_header(app_factory, asgi_client):
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str

    app = app_factory(docs_url="/docs")

    @app.post("/items")
    async def create(data: Item, res):
        await res.json({"name": data.name})

    result = await asgi_client(app, path="/openapi.json")
    spec = json.loads(result["body"])
    assert "security" not in spec
    assert "securitySchemes" not in spec.get("components", {})


# ---------------------------------------------------------------------------
# accepts_gzip (legacy negotiation helper)
# ---------------------------------------------------------------------------


def _req(headers):
    return SimpleNamespace(headers=dict(headers))


def test_accepts_gzip_plain(app_factory):
    app = app_factory()
    assert app.accepts_gzip(_req({"accept-encoding": "gzip"})) is True
    assert app.accepts_gzip(_req({})) is False
    assert app.accepts_gzip(_req({"accept-encoding": "compress"})) is False


def test_accepts_gzip_q_zero_rejects(app_factory):
    app = app_factory()
    assert app.accepts_gzip(_req({"accept-encoding": "gzip;q=0"})) is False


def test_accepts_gzip_identity_preferred(app_factory):
    app = app_factory()
    assert app.accepts_gzip(_req({"accept-encoding": "gzip;q=0.1, identity;q=1.0"})) is False
    assert app.accepts_gzip(_req({"accept-encoding": "gzip;q=0.5, identity;q=0.5"})) is True


def test_accepts_gzip_wildcard(app_factory):
    app = app_factory()
    assert app.accepts_gzip(_req({"accept-encoding": "*;q=0.5, identity;q=0"})) is True
    assert app.accepts_gzip(_req({"accept-encoding": "*;q=0"})) is False


def test_accepts_gzip_bad_q_defaults_to_one(app_factory):
    app = app_factory()
    assert app.accepts_gzip(_req({"accept-encoding": "gzip;q=bogus"})) is True
    assert app.accepts_gzip(_req({"accept-encoding": " , , "})) is False


# ---------------------------------------------------------------------------
# _parse_accept_encoding
# ---------------------------------------------------------------------------


def test_parse_accept_encoding_empty(app_factory):
    app = app_factory()
    assert app._parse_accept_encoding(_req({})) == {"identity": 0.0}


def test_parse_accept_encoding_clamps_and_tolerates(app_factory):
    app = app_factory()
    parsed = app._parse_accept_encoding(_req({"accept-encoding": "gzip;q=2, br;q=-1, deflate;q=oops, ;q=0.5"}))
    assert parsed["gzip"] == 1.0
    assert parsed["br"] == 0.0
    assert parsed["deflate"] == 1.0
    assert "" not in parsed


def test_parse_accept_encoding_header_error(app_factory):
    app = app_factory()

    class Exploding:
        @property
        def headers(self):
            raise RuntimeError("nope")

    assert app._parse_accept_encoding(Exploding()) == {"identity": 0.0}


# ---------------------------------------------------------------------------
# _negotiate_encoding
# ---------------------------------------------------------------------------


def _gzip_only(app):
    app.use_compression(min_size=1)
    app._compressors = {"gzip": app.gzip_compress, "deflate": app.deflate_compress}
    return app


def test_negotiate_encoding_gzip(app_factory):
    app = _gzip_only(app_factory())
    assert app._negotiate_encoding(_req({"accept-encoding": "gzip"})) == "gzip"
    assert app._negotiate_encoding(_req({"accept-encoding": "gzip;q=0.5, identity;q=0.5"})) == "gzip"
    assert app._negotiate_encoding(_req({"accept-encoding": "*;q=0.5, identity;q=0"})) == "gzip"


def test_negotiate_encoding_refusals(app_factory):
    app = _gzip_only(app_factory())
    assert app._negotiate_encoding(_req({"accept-encoding": "gzip;q=0"})) is None
    assert app._negotiate_encoding(_req({"accept-encoding": "br"})) is None
    assert app._negotiate_encoding(_req({"accept-encoding": "*;q=0"})) is None
    assert app._negotiate_encoding(_req({"accept-encoding": "gzip;q=0.1, identity;q=1.0"})) is None


# ---------------------------------------------------------------------------
# _is_compressible_type / _compression_for_body / _maybe_compress
# ---------------------------------------------------------------------------


def test_is_compressible_type(app_factory):
    app = app_factory()
    assert app._is_compressible_type(None) is False
    assert app._is_compressible_type(b"application/json") is True
    assert app._is_compressible_type(b"\xff\xfe") is False
    assert app._is_compressible_type(123) is False
    assert app._is_compressible_type("text/html; charset=utf-8") is True
    assert app._is_compressible_type("application/vnd.api+json") is True
    assert app._is_compressible_type("application/vnd.api+xml") is True
    assert app._is_compressible_type("image/png") is False
    assert app._is_compressible_type("") is False


def test_compression_for_body_guards(app_factory):
    app = app_factory()
    body = b"x" * 2048
    assert app._compression_for_body(_req({"accept-encoding": "gzip"}), body) is None

    app.use_compression(min_size=4096)
    assert app._compression_for_body(_req({"accept-encoding": "gzip"}), b"tiny") is None
    assert app._compression_for_body(_req({"accept-encoding": "identity"}), body) is None

    app._compressors = {}
    big = b"x" * 2048
    assert app._compression_for_body(_req({"accept-encoding": "gzip"}), big) is None


def test_compression_for_body_missing_compressor(app_factory, monkeypatch):
    app = _gzip_only(app_factory())
    app._compressors = {"gzip": None}
    monkeypatch.setattr(app, "_negotiate_encoding", lambda req: "gzip")
    assert app._compression_for_body(_req({"accept-encoding": "gzip"}), b"x" * 2048) is None


def test_maybe_compress_roundtrip(app_factory):
    app = _gzip_only(app_factory())
    body = b"hello world, " * 200
    assert app._maybe_compress(_req({}), body) == (body, None)
    compressed, encoding = app._maybe_compress(_req({"accept-encoding": "gzip"}), body)
    assert encoding == "gzip"
    assert _gzip.decompress(compressed) == body


def test_stream_compress_factory_guards(app_factory):
    app = app_factory()
    req = _req({"accept-encoding": "gzip"})
    assert app.stream_compress_factory(req, "text/plain", 5000) == (None, None)

    app.use_compression(min_size=1)
    assert app.stream_compress_factory(req, "text/plain", None) == (None, None)
    assert app.stream_compress_factory(req, "text/plain", 0) == (None, None)
    assert app.stream_compress_factory(req, "image/png", 5000) == (None, None)
    assert app.stream_compress_factory(_req({"accept-encoding": "identity"}), "text/plain", 5000) == (None, None)

    encoding, factory = app.stream_compress_factory(req, "text/plain", 5000)
    assert encoding == "gzip"
    assert callable(factory)

    app._stream_factories = {"gzip": None}
    assert app.stream_compress_factory(req, "text/plain", 5000) == (None, None)


def test_compress_primitives_roundtrip(app_factory):
    app = app_factory()
    app.use_compression(min_size=1)
    payload = b"the quick brown fox " * 100
    assert _gzip.decompress(app.gzip_compress(payload)) == payload
    assert zlib.decompress(app.deflate_compress(payload), -15) == payload


def test_init_compressors_without_optional_codecs(app_factory):
    import importlib.util

    app = app_factory()
    app.use_compression(min_size=1)
    assert app.enable_gzip is True
    assert app._compressors["gzip"] is not None
    assert app._compressors["deflate"] is not None
    if importlib.util.find_spec("zstandard") is None:
        assert app._compressors["zstd"] is None
        assert app._stream_factories["zstd"] is None
    if importlib.util.find_spec("brotli") is None and importlib.util.find_spec("brotlicffi") is None:
        assert app._compressors["br"] is None
        assert app._stream_factories["br"] is None


# ---------------------------------------------------------------------------
# RSGI compression path (_maybe_compress_async incl. thread offload)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rsgi_compresses_small_body(app_factory, rsgi_client):
    app = app_factory()
    app.use_compression(min_size=1)

    @app.get("/blob")
    async def blob(req, res):
        await res.json({"blob": "x" * 5000})

    result = await rsgi_client(app, path="/blob", headers={"Accept-Encoding": "gzip"})
    assert result["headers"]["content-encoding"] == ["gzip"]
    assert json.loads(_gzip.decompress(result["body"]).decode()) == {"blob": "x" * 5000}


@pytest.mark.asyncio
async def test_rsgi_compresses_large_body_off_thread(app_factory, rsgi_client):
    app = app_factory()
    app.use_compression(min_size=1)

    @app.get("/big")
    async def big(req, res):
        await res.json({"blob": "y" * 70000})

    result = await rsgi_client(app, path="/big", headers={"Accept-Encoding": "gzip"})
    assert result["headers"]["content-encoding"] == ["gzip"]
    assert json.loads(_gzip.decompress(result["body"]).decode()) == {"blob": "y" * 70000}


# ---------------------------------------------------------------------------
# request logging helpers
# ---------------------------------------------------------------------------


def test_show_requests_returns_state(app_factory):
    app = app_factory()
    assert app.show_requests(True) is True
    assert app._show_requests is True
    assert app.show_requests(False) is False


def test_color_for_status(app_factory):
    app = app_factory()
    assert app._color_for_status(200) == app._ANSI["green"]
    assert app._color_for_status(399) == app._ANSI["green"]
    assert app._color_for_status(404) == app._ANSI["yellow"]
    assert app._color_for_status(500) == app._ANSI["red"]
    assert app._color_for_status(150) == ""


def test_colors_enabled_branches(app_factory, monkeypatch):
    app = app_factory()
    monkeypatch.setenv("NO_COLOR", "1")
    assert app._colors_enabled() is False
    monkeypatch.delenv("NO_COLOR")

    class BadOut:
        def isatty(self):
            raise OSError("no tty")

    monkeypatch.setattr(sys, "stdout", BadOut())
    assert app._colors_enabled() is False


def test_format_request_log_plain_and_colored(app_factory, monkeypatch):
    app = app_factory()
    monkeypatch.setenv("NO_COLOR", "1")
    line = app._format_request_log("GET", "/x", 200, 5)
    assert '"GET /x HTTP/1.1" 200 5' in line
    assert "\x1b[" not in line

    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(app, "_colors_enabled", lambda: True)
    colored = app._format_request_log("GET", "/x", 404, 5)
    assert "\x1b[" in colored
    assert app._ANSI["yellow"] in colored
    uncolored = app._format_request_log("GET", "/x", 150, 5)
    assert "\x1b[" not in uncolored


def test_maybe_log_request_disabled(app_factory, capsys):
    app = app_factory()
    app._maybe_log_request("GET", "/x", b"", 200, 3)
    assert capsys.readouterr().out == ""


def test_maybe_log_request_prints_without_loop(app_factory, capsys):
    app = app_factory()
    app._show_requests = True
    app._maybe_log_request("GET", "/x", b"a=1", 200, 3)
    out = capsys.readouterr().out
    assert "/x?a=1" in out


@pytest.mark.asyncio
async def test_maybe_log_request_schedules_task(app_factory):
    app = app_factory()
    app._show_requests = True
    app._maybe_log_request("GET", "/x", b"", 200, 3)
    await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# temp-dir cleanup hooks
# ---------------------------------------------------------------------------


def test_app_enables_temp_cleanup_by_default(app_factory):
    app = app_factory()
    assert app.temp_dir is None
    assert app._temp_sweeper is not None
    assert app._temp_sweeper.interval_seconds == 30 * 60
    assert app._temp_sweeper.threshold_seconds == 30 * 60
    assert app._temp_sweeper._task is None


def test_enable_temp_cleanup_without_running_loop(app_factory):
    app = app_factory()
    app.enable_temp_cleanup()
    assert app._temp_sweeper is not None
    assert app._temp_sweeper._task is None


@pytest.mark.asyncio
async def test_enable_temp_cleanup_idempotent(app_factory):
    app = app_factory()
    app.enable_temp_cleanup(interval_seconds=3600, threshold_seconds=3600)
    first = app._temp_sweeper
    app.enable_temp_cleanup(interval_seconds=120, threshold_seconds=60)
    assert app._temp_sweeper is first
    assert first.interval_seconds == 120
    assert first.threshold_seconds == 60
    await asyncio.sleep(0.1)
    await app.disable_temp_cleanup()


@pytest.mark.asyncio
async def test_enable_temp_cleanup_rejects_invalid_updates(app_factory):
    app = app_factory()
    with pytest.raises(ValueError, match="interval_seconds"):
        app.enable_temp_cleanup(interval_seconds=0)
    with pytest.raises(ValueError, match="threshold_seconds"):
        app.enable_temp_cleanup(threshold_seconds=0)


@pytest.mark.asyncio
async def test_start_temp_sweeper_schedules_without_waiting(app_factory, monkeypatch):
    import flameo.cleanup as cleanup

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_start(self):
        started.set()
        await release.wait()

    monkeypatch.setattr(cleanup.TempSweeper, "start", blocked_start)
    app = app_factory()
    await asyncio.wait_for(app._start_temp_sweeper(), timeout=0.2)
    await asyncio.wait_for(started.wait(), timeout=0.2)
    release.set()
    await asyncio.sleep(0)
    await app.disable_temp_cleanup()


@pytest.mark.asyncio
async def test_start_temp_sweeper_branches(app_factory):
    from flameo.cleanup import TempSweeper

    app = app_factory()
    await app.disable_temp_cleanup()
    await app._start_temp_sweeper()
    app._temp_sweeper = TempSweeper(interval_seconds=3600, threshold_seconds=3600)
    await app._start_temp_sweeper()
    await asyncio.sleep(0.2)
    assert app._temp_sweeper._task is not None
    await app._start_temp_sweeper()
    await app.disable_temp_cleanup()
    assert app._temp_sweeper is None
    await app.disable_temp_cleanup()


def test_start_temp_sweeper_without_loop(app_factory):
    from flameo.cleanup import TempSweeper

    app = app_factory()
    app._temp_sweeper = TempSweeper(interval_seconds=3600, threshold_seconds=3600)
    coro = app._start_temp_sweeper()
    try:
        coro.send(None)
    except StopIteration:
        pass
    else:
        coro.close()
        raise AssertionError("sweeper start should return without a loop")
    assert app._temp_sweeper._task is None


def test_start_temp_sweeper_no_running_loop_returns(app_factory, monkeypatch):
    import asyncio as _asyncio

    from flameo.cleanup import TempSweeper

    app = app_factory()
    app._temp_sweeper = TempSweeper(interval_seconds=3600, threshold_seconds=3600)

    def _boom():
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(_asyncio, "get_running_loop", _boom)
    coro = app._start_temp_sweeper()
    try:
        coro.send(None)
    except StopIteration:
        pass
    else:
        coro.close()
        raise AssertionError("sweeper start should return without a loop")


@pytest.mark.asyncio
async def test_temp_dir_ctor_opts_into_cleanup(app_factory, tmp_path):
    import flameo.parser as parser

    previous = parser.TEMP_DIR
    try:
        app = app_factory(temp_dir=str(tmp_path / "spills"))
        assert app.temp_dir == str(tmp_path / "spills")
        assert app._temp_sweeper is not None
        assert app._temp_sweeper.interval_seconds == 30 * 60
        assert app._temp_sweeper.threshold_seconds == 30 * 60
        await app.disable_temp_cleanup()
    finally:
        parser.set_temp_dir(previous)


# ---------------------------------------------------------------------------
# startup / shutdown hooks
# ---------------------------------------------------------------------------


def test_startup_shutdown_registration_forms(app_factory):
    app = app_factory()

    async def direct():
        pass

    assert app.startup(direct) is direct

    @app.startup()
    async def decorated():
        pass

    def sync_hook():
        pass

    assert app.shutdown(sync_hook) is sync_hook

    @app.shutdown()
    async def decorated_down():
        pass

    assert len(app._startup_handlers) == 2
    assert len(app._shutdown_handlers) == 2


@pytest.mark.asyncio
async def test_startup_hooks_run_once_with_debug(app_factory, capsys):
    app = app_factory(debug=True)
    calls = []

    def sync_hook():
        calls.append("sync")

    @app.startup
    async def first():
        calls.append("async")

    app._startup_handlers.insert(0, sync_hook)
    await app._run_startup_hooks()
    await app._run_startup_hooks()
    assert calls == ["sync", "async"]
    assert "startup hook" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_shutdown_hooks_run_once_with_debug(app_factory, capsys):
    app = app_factory(debug=True)
    calls = []

    def sync_hook():
        calls.append("sync")

    @app.shutdown
    async def first():
        calls.append("async")

    app._shutdown_handlers.insert(0, sync_hook)
    await app._run_shutdown_hooks()
    await app._run_shutdown_hooks()
    assert calls == ["sync", "async"]
    assert "shutdown hook" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_lifespan_full_cycle_with_sweeper_and_requests(app_factory):
    app = app_factory()
    events = []

    @app.startup()
    async def _up():
        events.append("startup")

    @app.shutdown()
    def _down():
        events.append("shutdown")

    app.enable_temp_cleanup(interval_seconds=3600, threshold_seconds=3600)
    await asyncio.sleep(0.1)

    async def _done():
        return None

    task = asyncio.ensure_future(_done())
    await task
    app._active_requests.add(task)

    incoming = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent = []

    async def receive():
        return next(incoming)

    async def send(event):
        sent.append(event)

    await app.lifespan({"type": "lifespan"}, receive, send)
    assert events == ["startup", "shutdown"]
    assert [e["type"] for e in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert app._temp_sweeper is None


# ---------------------------------------------------------------------------
# pool shutdown variants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_db_pool_wait_closed_style(app_factory):
    app = app_factory()

    class AsyncMyPool:
        def __init__(self):
            self.waited = False

        def close(self):
            return None

        async def wait_closed(self):
            self.waited = True

    pool = AsyncMyPool()
    app.DB_POOL = pool
    await app.shutdown_db_pool()
    assert pool.waited is True
    assert app.DB_POOL is None
    await app.shutdown_db_pool()


@pytest.mark.asyncio
async def test_shutdown_cache_pool_variants(app_factory):
    app = app_factory()
    await app.shutdown_cache_pool()

    class Pool:
        def __init__(self):
            self.done = False

        async def disconnect(self):
            self.done = True

    class ModernClient:
        def __init__(self):
            self.aclosed = False

        async def aclose(self):
            self.aclosed = True

    pool = Pool()
    client = ModernClient()
    app.CACHE, app.CACHE_POOL = client, pool
    await app.shutdown_cache_pool()
    assert client.aclosed is True
    assert pool.done is True
    assert app.CACHE is None and app.CACHE_POOL is None

    class LegacyClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    legacy = LegacyClient()
    app.CACHE, app.CACHE_POOL = legacy, None
    await app.shutdown_cache_pool()
    assert legacy.closed is True


@pytest.mark.asyncio
async def test_init_cache_pool_requires_redis(app_factory, monkeypatch):
    monkeypatch.setitem(sys.modules, "redis.asyncio", None)
    app = app_factory(cache_config={
        "cache_type": CacheType.Redis,
        "max_connections": 5,
        "cache_url": "redis://localhost:6379",
    })
    with pytest.raises(ValueError, match="redis"):
        await app.init_cache_pool()


@pytest.mark.asyncio
async def test_run_cpu_task(app_factory):
    app = app_factory()
    assert await app.run_cpu_task(_forty_two) == 42


# ---------------------------------------------------------------------------
# serve_static (legacy middleware) + cwd rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_static_serves_and_falls_through(app_factory, asgi_client, tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "a.txt").write_text("legacy bytes", encoding="utf-8")
    app = app_factory()
    app.serve_static("/legacy", str(root))

    hit = await asgi_client(app, path="/legacy/a.txt")
    assert hit["status"] == 200
    assert hit["body"] == b"legacy bytes"

    miss = await asgi_client(app, path="/other/a.txt")
    assert miss["status"] == 404


@pytest.mark.asyncio
async def test_serve_static_rejects_and_reports(app_factory, asgi_client, tmp_path):
    root = tmp_path / "legacy2"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    (root / "sub").mkdir()
    app = app_factory()
    app.serve_static("/files", str(root))

    assert (await asgi_client(app, path="/files/../secret.txt"))["status"] == 403
    assert (await asgi_client(app, path="/files/missing.txt"))["status"] == 404
    assert (await asgi_client(app, path="/files/sub"))["status"] == 404


@pytest.mark.asyncio
async def test_serve_static_conditional_get(app_factory, asgi_client, tmp_path):
    root = tmp_path / "legacy3"
    root.mkdir()
    (root / "a.txt").write_text("conditional", encoding="utf-8")
    app = app_factory()
    app.serve_static("/s", str(root))

    first = await asgi_client(app, path="/s/a.txt")
    assert first["status"] == 200
    etag = first["headers"]["etag"][0]
    again = await asgi_client(app, path="/s/a.txt", headers={"If-None-Match": etag})
    assert again["status"] == 304
    assert again["body"] == b""
    wildcard = await asgi_client(app, path="/s/a.txt", headers={"If-None-Match": "*"})
    assert wildcard["status"] == 304


def test_serve_static_rejects_cwd(app_factory):
    app = app_factory()
    with pytest.warns(UserWarning, match="current working directory"):
        app.serve_static("/x", __import__("os").getcwd())
    assert app.static_middleware__ is None
    with pytest.warns(UserWarning, match="current directory"):
        app.serve_static("/x", "")
    assert app.static_middleware__ is None
    assert app._reject_cwd_static(123, "serve_static") is False


# ---------------------------------------------------------------------------
# use_static middleware branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_static_prefix_miss_falls_through(app_factory, asgi_client, tmp_path):
    root = tmp_path / "pub"
    root.mkdir()
    (root / "a.txt").write_text("hi", encoding="utf-8")
    app = app_factory()
    app.use_static("/assets", str(root))
    assert (await asgi_client(app, path="/elsewhere/a.txt"))["status"] == 404


@pytest.mark.asyncio
async def test_use_static_directory_and_traversal(app_factory, asgi_client, tmp_path):
    root = tmp_path / "pub2"
    root.mkdir()
    (root / "a.txt").write_text("hi", encoding="utf-8")
    (root / "sub").mkdir()
    app = app_factory()
    app.use_static("/assets", str(root))
    assert (await asgi_client(app, path="/assets/sub"))["status"] == 403
    assert (await asgi_client(app, path="/assets/../secret.txt"))["status"] == 403


@pytest.mark.asyncio
async def test_use_static_commonpath_error_is_403(app_factory, asgi_client, tmp_path, monkeypatch):
    root = tmp_path / "pub3"
    root.mkdir()
    (root / "a.txt").write_text("hi", encoding="utf-8")
    app = app_factory()
    app.use_static("/assets", str(root))

    def boom(*args):
        raise ValueError("no common path")

    monkeypatch.setattr(os.path, "commonpath", boom)
    assert (await asgi_client(app, path="/assets/a.txt"))["status"] == 403


@pytest.mark.asyncio
async def test_use_static_stat_errors(app_factory, asgi_client, tmp_path, monkeypatch):
    root = tmp_path / "pub4"
    root.mkdir()
    (root / "gone.txt").write_text("bye", encoding="utf-8")
    target = str(root / "gone.txt")
    app = app_factory()
    app.use_static("/assets", str(root))

    assert (await asgi_client(app, path="/assets/missing.txt"))["status"] == 404

    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if os.path.abspath(str(path)) == target:
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    assert (await asgi_client(app, path="/assets/gone.txt"))["status"] == 403


@pytest.mark.asyncio
async def test_use_static_non_regular_file_is_403(app_factory, asgi_client, tmp_path, monkeypatch):
    root = tmp_path / "pub5"
    root.mkdir()
    (root / "weird").write_text("x", encoding="utf-8")
    target = os.path.abspath(str(root / "weird"))
    app = app_factory()
    app.use_static("/assets", str(root))

    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if os.path.abspath(str(path)) == target:
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_size=1, st_mtime_ns=1)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    assert (await asgi_client(app, path="/assets/weird"))["status"] == 403


@pytest.mark.asyncio
async def test_use_static_last_modified_validators(app_factory, asgi_client, tmp_path):
    root = tmp_path / "pub6"
    root.mkdir()
    (root / "a.txt").write_text("validators", encoding="utf-8")
    app = app_factory()
    app.use_static("/assets", str(root))

    first = await asgi_client(app, path="/assets/a.txt")
    assert first["status"] == 200
    last_modified = first["headers"]["last-modified"][0]
    etag = first["headers"]["etag"][0]

    fresh = await asgi_client(app, path="/assets/a.txt", headers={"If-Modified-Since": last_modified})
    assert fresh["status"] == 304

    stale = await asgi_client(
        app, path="/assets/a.txt", headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"}
    )
    assert stale["status"] == 200

    broken = await asgi_client(app, path="/assets/a.txt", headers={"If-Modified-Since": "not-a-date"})
    assert broken["status"] == 200

    mismatch = await asgi_client(
        app, path="/assets/a.txt",
        headers={"If-None-Match": '"bogus"', "If-Modified-Since": last_modified},
    )
    assert mismatch["status"] == 200
    assert etag.startswith('W/"')


@pytest.mark.asyncio
async def test_use_static_head_request(app_factory, asgi_client, tmp_path):
    root = tmp_path / "pub7"
    root.mkdir()
    (root / "a.txt").write_text("head-body", encoding="utf-8")
    app = app_factory()
    app.use_static("/assets", str(root))
    result = await asgi_client(app, "HEAD", path="/assets/a.txt")
    assert result["status"] == 200


def test_use_static_rejects_cwd(app_factory):
    app = app_factory()
    before = len(app.global_middlewares)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        app.use_static("/x", os.getcwd())
        app.use_static("/x", "")
    assert len(app.global_middlewares) == before


# ---------------------------------------------------------------------------
# dispatch: middleware short-circuit, 500s, db slots, not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_middleware_short_circuits(app_factory, asgi_client):
    app = app_factory()
    reached = []

    async def early(req, res):
        await res.send("early")

    app.use(early)

    @app.get("/never")
    async def never(req, res):
        reached.append(True)
        await res.send("late")

    first = await asgi_client(app, path="/never")
    second = await asgi_client(app, path="/never")
    assert first["body"] == b"early" == second["body"]
    assert reached == []


@pytest.mark.asyncio
async def test_handler_without_response_is_500(app_factory, asgi_client):
    app = app_factory()

    @app.get("/silent")
    async def silent(req, res):
        return None

    result = await asgi_client(app, path="/silent")
    assert result["status"] == 500
    assert b"No response set by handler" in result["body"]


@pytest.mark.asyncio
async def test_handler_exception_is_500(app_factory, asgi_client):
    app = app_factory()

    @app.get("/boom")
    async def boom(req, res):
        raise RuntimeError("kaput")

    result = await asgi_client(app, path="/boom")
    assert result["status"] == 500
    assert b"Internal Server Error" in result["body"]


@pytest.mark.asyncio
async def test_handler_exception_debug_reports_code(app_factory, asgi_client, capsys):
    app = app_factory(debug=True)

    class Oracleish(Exception):
        def __init__(self):
            super().__init__("ORA-00001: unique constraint")
            self.code = 1

    @app.get("/ora")
    async def ora(req, res):
        raise Oracleish()

    result = await asgi_client(app, path="/ora")
    assert result["status"] == 500
    err = capsys.readouterr().err
    assert "Oracleish" in err
    assert "oracledb.code = 1" in err


@pytest.mark.asyncio
async def test_stream_then_raise_closes_body(app_factory, asgi_client):
    app = app_factory()

    @app.get("/flaky")
    async def flaky(req, res):
        await res.stream("part")
        raise RuntimeError("mid-stream")

    result = await asgi_client(app, path="/flaky")
    bodies = [e.get("body", b"") for e in result["events"] if e["type"] == "http.response.body"]
    assert bodies[0] == b"part"
    assert bodies[-1] == b""


@pytest.mark.asyncio
async def test_send_failure_in_error_path_is_swallowed(app_factory, asgi_client, monkeypatch):
    import flameo.flameo as flameo_mod

    app = app_factory()

    @app.get("/boom")
    async def boom(req, res):
        raise RuntimeError("kaput")

    async def broken_send(self, *args, **kwargs):
        raise RuntimeError("wire down")

    monkeypatch.setattr(flameo_mod.Response, "send", broken_send)
    result = await asgi_client(app, path="/boom")
    assert result["status"] is None


@pytest.mark.asyncio
async def test_finalize_errors_are_swallowed(app_factory, asgi_client):
    app = app_factory()

    async def clobber(req, res):
        def boom():
            raise RuntimeError("finalizer down")

        res._finalize_session__ = boom

    app.use(clobber)

    @app.get("/ok")
    async def ok(req, res):
        await res.send("fine")

    result = await asgi_client(app, path="/ok")
    assert result["status"] == 200
    assert result["body"] == b"fine"


@pytest.mark.asyncio
async def test_finishers_run_when_handler_raises(app_factory, asgi_client):
    app = app_factory()
    ran = []

    @app.get("/fail")
    async def fail(req, res):
        res.finish(lambda: ran.append("cleaned"))
        raise RuntimeError("kaput")

    result = await asgi_client(app, path="/fail")
    assert result["status"] == 500
    for _ in range(50):
        if ran:
            break
        await asyncio.sleep(0.01)
    assert ran == ["cleaned"]


@pytest.mark.asyncio
async def test_shutting_down_returns_503(app_factory, asgi_client):
    app = app_factory()

    @app.get("/ok")
    async def ok(req, res):
        await res.send("fine")

    app._shutting_down = True
    try:
        result = await asgi_client(app, path="/ok")
    finally:
        app._shutting_down = False
    assert result["status"] == 503
    assert result["body"] == b"Server shutting down"


@pytest.mark.asyncio
async def test_db_state_handler_without_pool_is_503(app_factory, asgi_client):
    app = app_factory()

    @app.get("/needs-db")
    async def needs_db(req, res, db, state):
        await res.send("unreachable")

    result = await asgi_client(app, path="/needs-db")
    assert result["status"] == 503
    assert b"Database not available" in result["body"]


@pytest.mark.asyncio
async def test_db_only_handler_gets_none(app_factory, asgi_client):
    app = app_factory()
    seen = {}

    @app.get("/db-optional")
    async def optional(req, res, db):
        seen["db"] = db
        await res.send("ok")

    result = await asgi_client(app, path="/db-optional")
    assert result["status"] == 200
    assert seen["db"] is None


@pytest.mark.asyncio
async def test_custom_not_found_variants(app_factory, asgi_client):
    app = app_factory()

    @app.not_found
    async def custom(req, res):
        await res.send("custom-missing", status=404)

    result = await asgi_client(app, path="/nope")
    assert result["status"] == 404
    assert result["body"] == b"custom-missing"

    other = app_factory()

    @other.not_found
    async def broken(req, res):
        raise RuntimeError("handler bug")

    fallback = await asgi_client(other, path="/nope")
    assert fallback["status"] == 404
    assert b"Cannot GET /nope" in fallback["body"]


@pytest.mark.asyncio
async def test_send_default_not_found_early_return(app_factory):
    app = app_factory()
    req = SimpleNamespace(method="GET", path="/x")
    res = SimpleNamespace(_sent=True)
    assert await app._send_default_not_found(req, res) is None


# ---------------------------------------------------------------------------
# pydantic 422 envelopes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pydantic_value_error_is_422(app_factory, asgi_client):
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str
        quantity: int

    app = app_factory()

    @app.post("/items")
    async def create(data: Item, res):
        await res.json({"name": data.name})

    result = await asgi_client(
        app, "POST", "/items",
        headers={"Content-Type": "application/json"},
        body=b'{"name":"tea","quantity":"many"}',
    )
    assert result["status"] == 422
    assert json.loads(result["body"])["error"] == "ValidationError"


@pytest.mark.asyncio
async def test_pydantic_unparseable_body_is_422(app_factory, asgi_client):
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str

    app = app_factory()

    @app.post("/items")
    async def create(data: Item, res):
        await res.json({"name": data.name})

    result = await asgi_client(
        app, "POST", "/items",
        headers={"Content-Type": "application/json"},
        body=b"{not valid json",
    )
    assert result["status"] == 422
    assert b"json_invalid" in result["body"]


@pytest.mark.asyncio
async def test_pydantic_files_error_tolerated_as_422(app_factory, asgi_client, monkeypatch):
    from pydantic import BaseModel

    from flameo import File
    from flameo.cycles import Request

    class Upload(BaseModel):
        title: str
        document: File

    def _boom(self):
        raise RuntimeError("files exploded")

    monkeypatch.setattr(Request, "files", property(_boom))

    app = app_factory()

    @app.post("/upload")
    async def upload(data: Upload, res):
        await res.json({"title": data.title})

    result = await asgi_client(
        app, "POST", "/upload",
        headers={"Content-Type": "multipart/form-data; boundary=zzz"},
        body=(b"--zzz\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
              b"report\r\n--zzz--\r\n"),
    )
    assert result["status"] == 422
    assert json.loads(result["body"])["error"] == "ValidationError"


# ---------------------------------------------------------------------------
# session commit / skip / clear through the ASGI pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_mutation_commits_cookie(app_factory, asgi_client):
    app = app_factory()
    app.use_session(secret_key="a-test-secret-long-enough", max_age=3600)

    @app.get("/write")
    async def write(req, res):
        req.session["count"] = 1
        await res.send("written")

    result = await asgi_client(app, path="/write")
    assert result["status"] == 200
    assert "set-cookie" in result["headers"]


@pytest.mark.asyncio
async def test_session_read_only_skips_cookie(app_factory, asgi_client):
    app = app_factory()
    app.use_session(secret_key="a-test-secret-long-enough", max_age=3600)

    @app.get("/read")
    async def read(req, res):
        await res.send("count=%r" % (req.session.get("count"),))

    result = await asgi_client(app, path="/read")
    assert result["status"] == 200
    assert "set-cookie" not in result["headers"]


@pytest.mark.asyncio
async def test_session_clear_expires_cookie(app_factory, asgi_client):
    app = app_factory()
    app.use_session(secret_key="a-test-secret-long-enough", max_age=3600)

    @app.get("/write")
    async def write(req, res):
        req.session["count"] = 1
        await res.send("written")

    @app.get("/clear")
    async def clear(req, res):
        req.session["count"] = 1
        req.session.clear()
        await res.send("cleared")

    first = await asgi_client(app, path="/write")
    cookie = first["headers"]["set-cookie"][0].split(";", 1)[0]
    cleared = await asgi_client(app, path="/clear", headers={"Cookie": cookie})
    assert "Expires=Thu, 01 Jan 1970 00:00:00 GMT" in cleared["headers"]["set-cookie"][0]


# ---------------------------------------------------------------------------
# RSGI shutdown drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rsgi_shutdown_drains_and_cleans_up(app_factory, rsgi_client):
    app = app_factory()
    events = []

    @app.shutdown
    async def _down():
        events.append("down")

    @app.get("/ok")
    async def ok(req, res):
        await res.json({"ok": True})

    async def _done():
        return None

    task = asyncio.ensure_future(_done())
    await task
    app._active_requests.add(task)
    app._shutting_down = True
    try:
        result = await rsgi_client(app, path="/ok")
    finally:
        app._shutting_down = False
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"ok": True}
    assert events == ["down"]


# ---------------------------------------------------------------------------
# second wave: remaining lifecycle branches
# ---------------------------------------------------------------------------


def test_accepts_gzip_multi_part_edges(app_factory):
    app = app_factory()
    assert app.accepts_gzip(_req({"accept-encoding": "deflate, gzip"})) is True
    assert app.accepts_gzip(_req({"accept-encoding": "gzip;foo=1;q=0.5"})) is True
    assert app.accepts_gzip(_req({"accept-encoding": "identity;q=1.0, *;q=0.5"})) is False


def test_parse_accept_encoding_skips(app_factory):
    app = app_factory()
    parsed = app._parse_accept_encoding(_req({"accept-encoding": "gzip,, br"}))
    assert parsed["gzip"] == 1.0
    assert parsed["br"] == 1.0
    assert app._parse_accept_encoding(_req({"accept-encoding": "gzip; level=1"}))["gzip"] == 1.0


def test_maybe_compress_empty_compressors(app_factory):
    app = app_factory()
    app.use_compression(min_size=1)
    app._compressors = {}
    big = b"x" * 2048
    assert app._maybe_compress(_req({"accept-encoding": "gzip"}), big) == (big, None)


def test_stream_deflate_factory_roundtrip(app_factory):
    app = app_factory()
    app.use_compression(min_size=1)
    encoding, factory = app.stream_compress_factory(
        _req({"accept-encoding": "deflate"}), "text/plain", 5000
    )
    assert encoding == "deflate"
    compress, finalize, header = factory()
    assert header == b""
    data = b"deflate-me " * 500
    assert zlib.decompress(compress(data) + finalize(), -15) == data


def test_stream_gzip_factory_roundtrip(app_factory):
    import zlib as _zlib

    app = app_factory()
    app.use_compression(min_size=1)
    encoding, factory = app.stream_compress_factory(
        _req({"accept-encoding": "gzip"}), "text/plain", 5000
    )
    assert encoding == "gzip"
    compress, finalize, header = factory()
    assert header == b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"
    data = b"gzip-me " * 500
    out = header + compress(data) + finalize()
    assert out.startswith(header)
    assert _zlib.decompress(out[len(header):-8], -15) == data
    assert int.from_bytes(out[-4:], "little") == len(data)


@pytest.mark.asyncio
async def test_shutdown_db_pool_sync_close_without_wait(app_factory):
    app = app_factory()

    class SyncPool:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True
            return None

    pool = SyncPool()
    app.DB_POOL = pool
    await app.shutdown_db_pool()
    assert pool.closed is True
    assert app.DB_POOL is None


@pytest.mark.asyncio
async def test_handler_exception_debug_without_code(app_factory, asgi_client, capsys):
    app = app_factory(debug=True)

    @app.get("/plain")
    async def plain(req, res):
        raise ValueError("plain failure")

    assert (await asgi_client(app, path="/plain"))["status"] == 500
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "oracledb.code" not in err


@pytest.mark.asyncio
async def test_handler_raise_after_send_keeps_response(app_factory, asgi_client):
    app = app_factory()

    @app.get("/late")
    async def late(req, res):
        await res.send("partial")
        raise RuntimeError("after send")

    result = await asgi_client(app, path="/late")
    assert result["status"] == 200
    assert result["body"] == b"partial"


@pytest.mark.asyncio
async def test_stream_close_failure_is_swallowed(app_factory):
    app = app_factory()
    events = []

    @app.get("/flaky")
    async def flaky(req, res):
        await res.stream("part")
        raise RuntimeError("mid-stream")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        if event["type"] == "http.response.body" and not event.get("more_body", False):
            raise RuntimeError("client gone")
        events.append(event)

    scope = {
        "type": "http", "method": "GET", "path": "/flaky", "query_string": b"",
        "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
    }
    await app(scope, receive, send)
    assert events[0]["type"] == "http.response.start"
    assert events[1]["body"] == b"part"


@pytest.mark.asyncio
async def test_error_path_logs_when_enabled(app_factory, asgi_client):
    app = app_factory()
    app._show_requests = True

    @app.get("/boom")
    async def boom(req, res):
        raise RuntimeError("kaput")

    assert (await asgi_client(app, path="/boom"))["status"] == 500
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_dynamic_route_params_are_bound(app_factory, asgi_client):
    app = app_factory()

    @app.get("/users/{user_id:int}")
    async def user(req, res):
        await res.json({"id": req.params["user_id"]})

    result = await asgi_client(app, path="/users/7")
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"id": 7}


@pytest.mark.asyncio
async def test_global_middleware_with_db_slot(app_factory, asgi_client):
    app = app_factory()
    seen = {}

    async def mw(req, res, db):
        seen["db"] = db

    app.use(mw)

    @app.get("/ok")
    async def ok(req, res):
        await res.send("fine")

    assert (await asgi_client(app, path="/ok"))["status"] == 200
    assert seen == {"db": None}


@pytest.mark.asyncio
async def test_pydantic_db_state_handler_without_pool_is_503(app_factory, asgi_client):
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str

    app = app_factory()

    @app.post("/items")
    async def create(data: Item, res, db, state):
        await res.json({"name": data.name})

    result = await asgi_client(
        app, "POST", "/items",
        headers={"Content-Type": "application/json"},
        body=b'{"name":"tea"}',
    )
    assert result["status"] == 503
    assert b"Database not available" in result["body"]


@pytest.mark.asyncio
async def test_send_default_not_found_direct_without_set_header(app_factory):
    app = app_factory()
    sent = {}

    class BareRes:
        _sent = False

        async def _send_body(self, body, content_type, status):
            sent.update(body=body, content_type=content_type, status=status)

    req = SimpleNamespace(method="GET", path="/x")
    await app._send_default_not_found(req, BareRes())
    assert sent["status"] == 404
    assert sent["content_type"] == b"text/html; charset=utf-8"
    assert b"Cannot GET /x" in sent["body"]


@pytest.mark.asyncio
async def test_validate_pydantic_case_sensitive_default(app_factory):
    from pydantic import BaseModel

    from flameo.flameo import _validate_pydantic

    class Item(BaseModel):
        name: str

    async def _body():
        return {"name": "x"}

    class Res:
        async def json(self, payload, status=None):
            raise AssertionError("must not be called")

    req = SimpleNamespace(app=None)
    req.body = _body()
    instance = await _validate_pydantic(req, Res(), Item)
    assert instance.name == "x"


@pytest.mark.asyncio
async def test_validate_pydantic_header_error(app_factory):
    from pydantic import BaseModel

    from flameo.flameo import _validate_pydantic

    class Item(BaseModel):
        name: str

    async def _body():
        return {"name": "x"}

    class FakeReq:
        def __init__(self, app):
            self.app = app
            self._body_coro = _body()

        @property
        def body(self):
            return self._body_coro

        @property
        def headers(self):
            raise RuntimeError("headers down")

    class Res:
        async def json(self, payload, status=None):
            raise AssertionError("must not be called")

    instance = await _validate_pydantic(FakeReq(app_factory()), Res(), Item)
    assert instance.name == "x"


def test_rsgi_init_task_branches(app_factory):
    app = app_factory()

    class FakeLoop:
        def __init__(self):
            self.tasks = []

        def create_task(self, coro):
            coro.close()
            task = object()
            self.tasks.append(task)
            return task

    loop = FakeLoop()
    app._rsgi_init_task__ = object()
    app.__rsgi_init__(loop)
    assert loop.tasks == []

    app._rsgi_init_task__ = None
    app_with_db = app_factory(db_config={"db_type": "x"})
    app_with_db.__rsgi_init__(loop)
    assert len(loop.tasks) == 1

    app_hooks = app_factory()
    app_hooks._startup_handlers.append(lambda: None)
    app_hooks.__rsgi_init__(loop)
    assert len(loop.tasks) == 2


@pytest.mark.asyncio
async def test_init_db_pool_async_direct(app_factory, monkeypatch):
    app = app_factory()
    await app._init_db_pool_async__()

    async def boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(app, "init_cache_pool", boom)
    with pytest.raises(RuntimeError, match="pool down"):
        await app._init_db_pool_async__()


@pytest.mark.asyncio
async def test_rsgi_awaits_pending_init_task(app_factory, rsgi_client):
    app = app_factory()

    @app.get("/ok")
    async def ok(req, res):
        await res.json({"ok": True})

    task = asyncio.get_running_loop().create_future()
    task.set_result(None)
    app._rsgi_init_task__ = task
    result = await rsgi_client(app, path="/ok")
    assert result["status"] == 200
    assert app._rsgi_init_task__ is None


@pytest.mark.asyncio
async def test_rsgi_without_response_bytes_returns_tuple(app_factory):
    app = app_factory()

    @app.get("/ok")
    async def ok(req, res):
        await res.json({"ok": True})

    scope = SimpleNamespace(
        method="GET", path="/ok", query_string=b"", headers={},
        scheme="http", server=("testserver", 80), client=("127.0.0.1", 12345),
    )
    status, headers, chunks = await app.__rsgi__(scope, None)
    assert status == 200
    assert json.loads(b"".join(chunks)) == {"ok": True}


@pytest.mark.asyncio
async def test_lifespan_ignores_unknown_events(app_factory):
    app = app_factory()
    events = []

    @app.startup()
    async def _up():
        events.append("startup")

    @app.shutdown()
    async def _down():
        events.append("shutdown")

    incoming = iter([
        {"type": "lifespan.unknown"},
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ])
    sent = []

    async def receive():
        return next(incoming)

    async def send(event):
        sent.append(event)

    await app.lifespan({"type": "lifespan"}, receive, send)
    assert events == ["startup", "shutdown"]
    assert [e["type"] for e in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


@pytest.mark.asyncio
async def test_call_delegates_lifespan_scope(app_factory):
    app = app_factory()
    events = []

    @app.startup()
    async def _up():
        events.append("startup")

    @app.shutdown()
    async def _down():
        events.append("shutdown")

    incoming = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent = []

    async def receive():
        return next(incoming)

    async def send(event):
        sent.append(event)

    await app({"type": "lifespan"}, receive, send)
    assert events == ["startup", "shutdown"]
    assert [e["type"] for e in sent] == [
        "lifespan.startup.complete", "lifespan.shutdown.complete",
    ]


def test_not_found_rejects_non_callable(app_factory):
    app = app_factory()
    with pytest.raises(TypeError, match="must be a callable"):
        app.not_found("nope")


def test_rsgi_init_schedules_default_sweeper(app_factory):
    app = app_factory()

    class FakeLoop:
        def __init__(self):
            self.tasks = []

        def create_task(self, coro):
            coro.close()
            task = object()
            self.tasks.append(task)
            return task

    loop = FakeLoop()
    app.__rsgi_init__(loop)
    assert app._rsgi_init_task__ is not None
    assert len(loop.tasks) == 1


@pytest.mark.asyncio
async def test_rsgi_handler_exception_debug_is_500(app_factory, rsgi_client, capsys):
    app = app_factory(debug=True)

    @app.get("/boom")
    async def boom(req, res):
        raise RuntimeError("rsgi kaput")

    result = await rsgi_client(app, path="/boom")
    assert result["status"] == 500
    assert b"Internal Server Error" in result["body"]
    assert "RuntimeError" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_rsgi_handler_exception_debug_reports_code(app_factory, rsgi_client, capsys):
    app = app_factory(debug=True)

    class Oracleish(Exception):
        def __init__(self):
            super().__init__("ORA-00001: unique constraint")
            self.code = 1

    @app.get("/ora")
    async def ora(req, res):
        raise Oracleish()

    result = await rsgi_client(app, path="/ora")
    assert result["status"] == 500
    assert "oracledb.code = 1" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_rsgi_shutdown_without_active_requests(app_factory, rsgi_client):
    app = app_factory()
    events = []

    @app.shutdown()
    async def _down():
        events.append("down")

    @app.get("/ok")
    async def ok(req, res):
        await res.json({"ok": True})

    assert len(app._active_requests) == 0
    app._shutting_down = True
    try:
        result = await rsgi_client(app, path="/ok")
    finally:
        app._shutting_down = False
    assert result["status"] == 200
    assert events == ["down"]
    assert app._temp_sweeper is None


def test_accepts_gzip_repeated_codings(app_factory):
    app = app_factory()
    assert app.accepts_gzip(_req({"accept-encoding": "gzip;q=0.8, gzip;q=0.2"})) is True
    assert app.accepts_gzip(_req({"accept-encoding": "identity;q=0.9, identity;q=0.1"})) is False
    assert app.accepts_gzip(_req({"accept-encoding": "*;q=0.9, *;q=0.1"})) is True


@pytest.mark.asyncio
async def test_rsgi_compressed_response_preserves_existing_vary(app_factory, rsgi_client):
    app = app_factory()
    app.use_compression(min_size=1)

    @app.get("/vary")
    async def vary(req, res):
        res.setHeader("Vary", "Origin")
        await res.send("x" * 5000)

    result = await rsgi_client(app, path="/vary", headers={"Accept-Encoding": "gzip"})
    assert result["headers"]["content-encoding"] == ["gzip"]
    assert result["headers"]["vary"] == ["Accept-Encoding"]


@pytest.mark.asyncio
async def test_hot_handler_swallows_session_finalizer_error(app_factory):
    app = app_factory()

    @app.get("/finalizer")
    async def finalizer(req, res):
        await res.send("ok")

    scope = {
        "type": "http", "method": "GET", "path": "/finalizer",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": ("127.0.0.1", 1),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    events = []

    async def send(event):
        events.append(event)

    req = Request(scope, receive, 10 * 1024 * 1024)
    req._app = app
    res = Response(send, app=app, req=req)

    def broken_finalizer():
        raise RuntimeError("finalizer")

    res._finalize_session__ = broken_finalizer
    await app._handle_asgi_hot(req, res)
    assert any(event["type"] == "http.response.body" for event in events)
