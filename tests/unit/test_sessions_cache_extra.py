from __future__ import annotations

import base64
import json
import types

import pytest

import flameo.endpoint_cache as ec
import flameo.sessions as sess
from flameo.endpoint_cache import (
    EndpointCacheSpec,
    MemoryCacheStore,
    _cache_get,
    _decode_entry,
    _encode_entry,
    _replay_entry,
    bind_redis_json_helpers,
    cached_invoke,
    warn_uncacheable_method,
)
from flameo.sessions import (
    Session,
    _b64url_decode,
    _b64url_encode,
    _coerce_options,
    _decode_session,
    _encode_session,
    make_session_middleware,
)

SECRET = "long-enough-secret-for-tests"


# ---------- sessions: _decode_session edge cases ----------

def test_decode_empty_and_missing_dot():
    assert _decode_session(SECRET, "", 60) is None
    assert _decode_session(SECRET, "no-dot-here", 60) is None
    assert _decode_session(SECRET, "abc.", 60) is None
    assert _decode_session(SECRET, ".sigpart", 60) is None


def test_decode_bad_b64():
    # single-char base64 raises binascii.Error (invalid length)
    assert _decode_session(SECRET, "a.eA", 60) is None
    assert _decode_session(SECRET, "eA.a", 60) is None


def test_decode_bad_json():
    payload_b64 = _b64url_encode(b"not-json")
    sig_b64 = _b64url_encode(b"sig")
    assert _decode_session(SECRET, f"{payload_b64}.{sig_b64}", 60) is None


def test_decode_non_dict_obj():
    import json as _json
    payload_b64 = _b64url_encode(_json.dumps([1, 2]).encode())
    sig_b64 = _b64url_encode(b"x")
    assert _decode_session(SECRET, f"{payload_b64}.{sig_b64}", 60) is None


def test_decode_bad_ts_data_types():
    import json as _json
    import hmac as _hmac, hashlib as _hl
    for obj in (
        {"ts": "bad", "data": {}},
        {"ts": 123, "data": "bad"},
        {"ts": 123},
        {"data": {}},
    ):
        raw = _json.dumps(obj).encode()
        pb = _b64url_encode(raw)
        sig = _b64url_encode(_hmac.new(SECRET.encode(), f"1.{pb}".encode(), _hl.sha256).digest())
        # ts wrong type or missing -> None regardless of sig
        assert _decode_session(SECRET, f"{pb}.{sig}", 60) is None


def test_decode_max_age_variants(monkeypatch):
    import time as _realtime
    real_now = int(_realtime.time())
    token = _encode_session(SECRET, {"n": 1})
    # max_age None -> no expiry check
    assert _decode_session(SECRET, token, None) == {"n": 1}
    # max_age 0 -> treated as no expiry
    monkeypatch.setattr(sess.time, "time", lambda: 3_000_000_000)
    assert _decode_session(SECRET, token, 0) == {"n": 1}
    # not expired
    monkeypatch.setattr(sess.time, "time", lambda: real_now + 10)
    assert _decode_session(SECRET, token, 3600) == {"n": 1}
    # expired
    monkeypatch.setattr(sess.time, "time", lambda: real_now + 4000)
    assert _decode_session(SECRET, token, 3600) is None


def test_decode_tampered_sig():
    token = _encode_session(SECRET, {"a": 1})
    head, sig = token.rsplit(".", 1)
    bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert _decode_session(SECRET, f"{head}.{bad_sig}", 60) is None


# ---------- sessions: Session mutation methods ----------

def test_session_delitem():
    s = Session({"a": 1})
    s.mark_clean()
    del s["a"]
    assert s == {}
    assert s._dirty is True


def test_session_setdefault():
    s = Session()
    s.mark_clean()
    assert s.setdefault("k", 5) == 5
    assert s._dirty is True
    s.mark_clean()
    assert s.setdefault("k", 99) == 5
    assert s._dirty is False


def test_session_pop():
    s = Session({"a": 1})
    s.mark_clean()
    assert s.pop("a") == 1
    assert s._dirty is True
    s.mark_clean()
    assert s.pop("missing", "dflt") == "dflt"
    assert s._dirty is False


def test_session_popitem():
    s = Session({"a": 1})
    s.mark_clean()
    k, v = s.popitem()
    assert (k, v) == ("a", 1)
    assert s._dirty is True
    with pytest.raises(KeyError):
        Session().popitem()


def test_session_clear_empty_stays_clean():
    s = Session()
    assert s._dirty is False
    s.clear()
    assert s == {}
    assert s._dirty is False
    s2 = Session({"x": 1})
    s2.mark_clean()
    s2.clear()
    assert s2._dirty is True


def test_session_update_marks_dirty():
    s = Session()
    s.mark_clean()
    s.update({"a": 1})
    assert s["a"] == 1
    assert s._dirty is True
    s.mark_clean()
    s.update(a=2)
    assert s["a"] == 2
    assert s._dirty is True


# ---------- sessions: options coercion & validation ----------

def test_coerce_empty_and_unknown_and_camel():
    assert _coerce_options({}) == {}
    assert _coerce_options(None) == {}
    out = _coerce_options({"bogus": 1, "secret_key": SECRET})
    assert out == {"secret_key": SECRET}
    out2 = _coerce_options({
        "secretKey": SECRET, "maxAge": 10, "httpsOnly": True,
        "sameSite": "strict", "path": "/x", "name": "n",
    })
    assert out2 == {"secret_key": SECRET, "max_age": 10, "https_only": True,
                    "same_site": "strict", "path": "/x", "name": "n"}


def test_middleware_validation_errors():
    app = object()
    with pytest.raises(ValueError, match="secret_key"):
        make_session_middleware(app)
    with pytest.raises(ValueError, match="secret_key"):
        make_session_middleware(app, secret_key="short")
    with pytest.raises(ValueError, match="max_age"):
        make_session_middleware(app, secret_key=SECRET, max_age=-1)
    with pytest.raises(ValueError, match="max_age"):
        make_session_middleware(app, secret_key=SECRET, max_age="bad")
    with pytest.raises(ValueError, match="same_site"):
        make_session_middleware(app, secret_key=SECRET, same_site="bogus")
    with pytest.raises(ValueError, match="path"):
        make_session_middleware(app, secret_key=SECRET, path="")
    with pytest.raises(ValueError, match="path"):
        make_session_middleware(app, secret_key=SECRET, path=123)
    with pytest.raises(ValueError, match="name"):
        make_session_middleware(app, secret_key=SECRET, name="")
    with pytest.raises(ValueError, match="name"):
        make_session_middleware(app, secret_key=SECRET, name=123)


def test_middleware_defaults_and_camel_and_samesite_none():
    app = object()
    mw = make_session_middleware(app, secret_key=SECRET)
    assert callable(mw)
    mw2 = make_session_middleware(app, secretKey=SECRET, maxAge=50, sameSite="STRICT")
    assert callable(mw2)
    with pytest.warns(UserWarning, match="sameSite"):
        make_session_middleware(app, secret_key=SECRET, same_site="none")
    # none + httpsOnly -> no warning
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make_session_middleware(app, secret_key=SECRET, same_site="none", https_only=True)


@pytest.mark.asyncio
async def test_session_valid_cookie_roundtrip_writes_back(app_factory, asgi_client):
    app = app_factory()
    app.use_session(secret_key=SECRET)

    @app.get("/inc")
    async def inc(req, res):
        req.session["count"] = req.session.get("count", 0) + 1
        await res.json({"count": req.session["count"]})

    first = await asgi_client(app, path="/inc")
    assert first["status"] == 200
    assert "set-cookie" in first["headers"]  # dirty + non-empty -> Set-Cookie
    cookie = first["headers"]["set-cookie"][0].split(";", 1)[0]
    second = await asgi_client(app, path="/inc", headers={"Cookie": cookie})
    assert json.loads(second["body"]) == {"count": 2}


@pytest.mark.asyncio
async def test_session_no_mutation_emits_no_cookie(app_factory, asgi_client):
    app = app_factory()
    app.use_session(secret_key=SECRET)

    @app.get("/ro")
    async def ro(req, res):
        await res.send("ok")

    r = await asgi_client(app, path="/ro")
    assert r["status"] == 200
    assert "set-cookie" not in r["headers"]


@pytest.mark.asyncio
async def test_session_bad_cookie_recovers_and_custom_name_path(app_factory, asgi_client):
    app = app_factory()
    app.use_session(secret_key=SECRET, name="mine", path="/sub", same_site="strict")

    @app.get("/who")
    async def who(req, res):
        await res.json({"sess": dict(req.session)})

    r = await asgi_client(app, path="/who", headers={"Cookie": "mine=garbage!!!"})
    assert r["status"] == 200
    assert json.loads(r["body"]) == {"sess": {}}


@pytest.mark.asyncio
async def test_session_set_then_clear_expires_cookie(app_factory, asgi_client):
    app = app_factory()
    app.use_session(secret_key=SECRET)

    @app.get("/setclear")
    async def sc(req, res):
        req.session["a"] = 1
        req.session.clear()
        await res.send("ok")

    r = await asgi_client(app, path="/setclear")
    assert r["status"] == 200
    assert "Expires=Thu, 01 Jan 1970 00:00:00 GMT" in r["headers"]["set-cookie"][0]


@pytest.mark.asyncio
async def test_session_commit_exception_swallowed(app_factory, asgi_client, monkeypatch):
    import flameo.cycles as cycles
    app = app_factory()
    app.use_session(secret_key=SECRET)

    @app.get("/w")
    async def w(req, res):
        req.session["x"] = 1
        await res.send("ok")

    monkeypatch.setattr(cycles.Response, "cookie",
                        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = await asgi_client(app, path="/w")
    assert r["status"] == 200


# ---------- endpoint cache: MemoryCacheStore ----------

@pytest.mark.asyncio
async def test_memory_store_evict_overflow_with_small_max(monkeypatch):
    monkeypatch.setattr(ec, "_MEMORY_STORE_MAX", 3)
    c = MemoryCacheStore()
    await c.set("a", "1")
    await c.set("b", "2")
    await c.set("c", "3")
    await c.set("d", "4")
    assert len(c._data) <= 3
    assert await c.get("a") is None
    assert await c.get("d") == "4"


@pytest.mark.asyncio
async def test_memory_store_evict_purges_expired_first(monkeypatch):
    monkeypatch.setattr(ec, "_MEMORY_STORE_MAX", 3)
    now = [100.0]
    monkeypatch.setattr(ec.time, "monotonic", lambda: now[0])
    c = MemoryCacheStore()
    await c.set("e1", "x", ex=1)
    await c.set("e2", "y", ex=1)
    now[0] += 5
    await c.set("k1", "1")
    await c.set("k2", "2")
    await c.set("k3", "3")
    await c.set("k4", "4")
    assert await c.get("e1") is None
    assert await c.get("e2") is None
    assert len(c._data) <= 3


@pytest.mark.asyncio
async def test_memory_store_get_types_and_json_none():
    c = MemoryCacheStore()
    await c.set("i", 42)
    assert await c.get("i") == "42"
    await c.set("b", b"hello")
    assert await c.get("b") == "hello"
    await c.set("ba", bytearray(b"abc"))
    assert await c.get("ba") == "abc"
    assert await c.get("missing") is None
    assert await c.get_json("nope") is None
    assert await c.exists("i") is True
    assert await c.exists("missing") is False
    assert await c.set("i", "other", nx=True) is None
    await c.set_json("j", {"ok": True})
    assert await c.get_json("j") == {"ok": True}


@pytest.mark.asyncio
async def test_memory_store_nx_after_expiry(monkeypatch):
    now = [200.0]
    monkeypatch.setattr(ec.time, "monotonic", lambda: now[0])
    c = MemoryCacheStore()
    assert await c.set("k", "1", ex=1, nx=True) is True
    now[0] += 2
    assert await c.get("k") is None
    assert await c.set("k", "2", nx=True) is True
    assert await c.get("k") == "2"


@pytest.mark.asyncio
async def test_memory_store_incr_variants():
    c = MemoryCacheStore()
    with pytest.raises(KeyError):
        await c.incr("missing")
    await c.set("bytes-k", b"10")
    assert await c.incr("bytes-k") == 11
    await c.set("int-k", 5)
    assert await c.incr("int-k", 2) == 7
    await c.set("str-k", "7")
    assert await c.incr("str-k") == 8
    await c.set("f", 1.5)
    with pytest.raises(ValueError, match="not an integer"):
        await c.incr("f")
    await c.set("bad", "abc")
    with pytest.raises(ValueError, match="not an integer"):
        await c.incr("bad")
    await c.set("lst", ["x"])
    with pytest.raises(ValueError, match="not an integer"):
        await c.incr("lst")


@pytest.mark.asyncio
async def test_memory_store_decr():
    c = MemoryCacheStore()
    await c.set("n", "10")
    assert await c.decr("n") == 9
    assert await c.decr("n", 3) == 6


@pytest.mark.asyncio
async def test_redis_json_helpers_bind():
    class FakeRedis:
        def __init__(self):
            self.d = {}
        async def set(self, key, value, ex=None, nx=False):
            self.d[key] = value
            return True
        async def get(self, key):
            return self.d.get(key)
    cli = FakeRedis()
    bind_redis_json_helpers(cli)
    assert await cli.set_json("j", {"a": 1}) is True
    assert await cli.get_json("j") == {"a": 1}
    assert await cli.get_json("missing") is None


def test_spec_resolve_and_key():
    mem = MemoryCacheStore()
    resp_mem = MemoryCacheStore()
    host = types.SimpleNamespace(CACHE=None, _memory_cache=mem, _response_memory_cache=resp_mem)
    spec = EndpointCacheSpec(ttl=30.7)
    assert spec.ex_redis == 30
    a, b = spec.resolve(host)
    assert a is mem and b is resp_mem
    fake_cache = object()
    host2 = types.SimpleNamespace(CACHE=fake_cache, _memory_cache=mem, _response_memory_cache=resp_mem)
    c1, c2 = spec.resolve(host2)
    assert c1 is fake_cache and c2 is fake_cache
    assert spec._client is fake_cache
    req = types.SimpleNamespace(method="GET", originalUrl="/x?y=1")
    assert spec.key_for(req) == "flameo:epcache:GET:/x?y=1"


def test_warn_uncacheable():
    with pytest.warns(UserWarning, match="Caching may not work"):
        warn_uncacheable_method()


def test_entry_codec_roundtrip_and_bytes_and_bad():
    entry = (200, [("x-a", "1")], "text/plain", b"hi")
    payload = _encode_entry(entry)
    assert _decode_entry(payload) == entry
    assert _decode_entry(bytes(payload)) == entry
    assert _decode_entry("garbage!!!") is None
    assert _decode_entry('{"s":200}') is None
    import orjson
    bad = orjson.dumps({"s": 200, "h": [], "ct": "text/plain", "b": "!!!notb64!!!"})
    assert _decode_entry(bad) is None


@pytest.mark.asyncio
async def test_cache_get_swallow():
    class Boom:
        async def get(self, k):
            raise RuntimeError("down")
    assert await _cache_get(Boom(), "k") is None
    c = MemoryCacheStore()
    await c.set("k", "v")
    assert await _cache_get(c, "k") == "v"


@pytest.mark.asyncio
async def test_replay_asgi_with_list_header():
    entry = (200, [("x-m", ["a", "b"]), ("x-s", "s")], "text/plain", b"body")
    sent = {}
    class FakeApp:
        _base_header_map = {}
    class FakeRes:
        def __init__(self):
            self._header_map = {}
            self._app = FakeApp()
            self.headers_set = {}
        def setHeader(self, k, v):
            self.headers_set[k] = v
        async def _send_body(self, body, ct, status):
            sent.update(body=body, ct=ct, status=status)
    res = FakeRes()
    await _replay_entry(res, entry)
    assert res.headers_set.get("X-Flameo-Cache") == "HIT"
    assert sent["body"] == b"body"
    assert sent["status"] == 200
    assert res._header_map[b"x-m"] == [b"a", b"b"]


@pytest.mark.asyncio
async def test_replay_rsgi_branch():
    entry = (201, [("x-a", "1")], "text/plain", b"zz")
    sent = {}
    class FakeRes:
        def __init__(self):
            self._hdr = []
        def setHeader(self, k, v):
            self._hdr.append((k, v))
        async def _send_body(self, body, ct, status):
            sent.update(body=body, ct=ct, status=status)
    res = FakeRes()
    await _replay_entry(res, entry)
    assert ("X-Flameo-Cache", "HIT") in res._hdr
    assert sent["status"] == 201


@pytest.mark.asyncio
async def test_maybe_store_branches():
    spec = EndpointCacheSpec(ttl=10)
    store = MemoryCacheStore()

    class R:
        def __init__(self):
            self._sent = True
            self._streamed = False
            self._streaming = False
    # empty capture
    await ec._maybe_store(spec, store, "k0", {}, R())
    assert await store.get("k0") is None
    # not sent
    r = R(); r._sent = False
    await ec._maybe_store(spec, store, "k1", {"status": 200}, r)
    # streamed
    r2 = R(); r2._streamed = True
    await ec._maybe_store(spec, store, "k2", {"status": 200, "headers": [], "ct": "text/plain", "body": b"x"}, r2)
    assert await store.get("k2") is None
    r3 = R(); r3._streaming = True
    await ec._maybe_store(spec, store, "k3", {"status": 200, "headers": [], "ct": "text/plain", "body": b"x"}, r3)
    assert await store.get("k3") is None
    # bad statuses
    for bad_status in (None, 404, 500, 199):
        await ec._maybe_store(spec, store, f"bad{bad_status}",
                              {"status": bad_status, "headers": [], "ct": "text/plain", "body": b"x"}, R())
        assert await store.get(f"bad{bad_status}") is None
    # headers None
    await ec._maybe_store(spec, store, "hn", {"status": 200, "headers": None, "ct": "text/plain", "body": b"x"}, R())
    assert await store.get("hn") is None
    # set-cookie skip
    await ec._maybe_store(spec, store, "ck",
                          {"status": 200, "headers": [("Set-Cookie", "a=1")], "ct": "text/plain", "body": b"x"}, R())
    assert await store.get("ck") is None
    # encode failure (body not bytes)
    await ec._maybe_store(spec, store, "enc",
                          {"status": 200, "headers": [], "ct": "text/plain", "body": "not-bytes"}, R())
    assert await store.get("enc") is None
    # set failure swallowed
    class FailStore:
        async def set(self, *a, **k):
            raise RuntimeError("down")
    await ec._maybe_store(spec, FailStore(), "f",
                          {"status": 200, "headers": [], "ct": "text/plain", "body": b"x"}, R())
    # success with MemoryCacheStore uses ttl
    await ec._maybe_store(spec, store, "ok",
                          {"status": 200, "headers": [], "ct": "text/plain", "body": b"x"}, R())
    assert await store.get("ok") is not None


@pytest.mark.asyncio
async def test_maybe_store_ex_redis_for_remote():
    spec = EndpointCacheSpec(ttl=33.9)
    seen = {}
    class Remote:
        async def set(self, key, payload, ex=None):
            seen["ex"] = ex
            return True
    class R:
        _sent = True
        _streamed = False
        _streaming = False
    await ec._maybe_store(spec, Remote(), "k",
                          {"status": 200, "headers": [], "ct": "text/plain", "body": b"x"}, R())
    assert seen["ex"] == spec.ex_redis == 33


@pytest.mark.asyncio
async def test_cached_invoke_hit_and_miss_and_errors():
    spec = EndpointCacheSpec(ttl=30)
    store = MemoryCacheStore()
    entry = (200, [], "text/plain", b"cached-body")
    key = "k-hit"
    await store.set(key, _encode_entry(entry), ex=60)

    class FakeApp:
        _base_header_map = {}
    class FakeRes:
        def __init__(self):
            self._header_map = {}
            self._app = FakeApp()
            self._sent = True
            self._streamed = False
            self._streaming = False
            self._cache_capture__ = None
            self.hits = []
        def setHeader(self, k, v):
            self.hits.append((k, v))
        async def _send_body(self, body, ct, status):
            self.body = body
    res = FakeRes()
    called = []
    async def invoke():
        called.append(1)
        return "never"
    out = await cached_invoke(spec, store, key, res, invoke)
    assert out is None
    assert called == []
    assert res.body == b"cached-body"

    # corrupt payload -> miss path runs handler
    await store.set("k-bad", "not-json", ex=60)
    res2 = FakeRes()
    async def invoke2():
        return "ran"
    out2 = await cached_invoke(spec, store, "k-bad", res2, invoke2)
    assert out2 == "ran"

    # setHeader raising is swallowed (MISS header)
    class BadHeaderRes(FakeRes):
        def setHeader(self, k, v):
            raise RuntimeError("hdr down")
    res3 = BadHeaderRes()
    async def invoke3():
        return "ok3"
    assert await cached_invoke(spec, store, "k-miss-3", res3, invoke3) == "ok3"

    # invoke raising propagates, no store
    async def boom():
        raise RuntimeError("handler boom")
    res4 = FakeRes()
    with pytest.raises(RuntimeError, match="handler boom"):
        await cached_invoke(spec, store, "k-boom", res4, boom)
    assert res4._cache_capture__ is None


@pytest.mark.asyncio
async def test_endpoint_cache_key_variants_query(app_factory, asgi_client):
    app = app_factory()
    calls = {"n": 0}

    @app.cache(ttl=30)
    @app.get("/q")
    async def q(req, res):
        calls["n"] += 1
        await res.json({"n": calls["n"], "q": req.query})

    a = await asgi_client(app, path="/q", query=b"x=1")
    b = await asgi_client(app, path="/q", query=b"x=1")
    c = await asgi_client(app, path="/q", query=b"x=2")
    assert json.loads(a["body"]) == json.loads(b["body"])
    assert b["headers"]["x-flameo-cache"] == ["HIT"]
    assert json.loads(c["body"])["n"] == 2


@pytest.mark.asyncio
async def test_endpoint_cache_skips_non2xx_and_cookie_routes(app_factory, asgi_client):
    app = app_factory()

    @app.cache(ttl=30)
    @app.get("/nf")
    async def nf(req, res):
        await res.status(404).send("nope")

    first = await asgi_client(app, path="/nf")
    second = await asgi_client(app, path="/nf")
    assert first["status"] == 404
    # second must also be MISS (never cached)
    assert second["headers"]["x-flameo-cache"] == ["MISS"]

    @app.cache(ttl=30)
    @app.get("/ck")
    async def ck(req, res):
        res.cookie("sess", "abc")
        await res.send("with-cookie")

    r1 = await asgi_client(app, path="/ck")
    r2 = await asgi_client(app, path="/ck")
    assert r2["headers"]["x-flameo-cache"] == ["MISS"]
