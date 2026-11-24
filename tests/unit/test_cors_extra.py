from types import SimpleNamespace

import pytest

from flameo.cors import _as_list, _coerce_options, make_cors_middleware


def _req(method="GET", headers=None):
    return SimpleNamespace(method=method, headers=dict(headers or {}))


class _Res:
    def __init__(self, headers=None):
        self.headers = dict(headers or {})
        self.set_calls = {}
        self.sent = None

    def setHeader(self, name, value):
        self.set_calls[name] = value
        self.headers[name] = value

    async def send(self, body, status=200):
        self.sent = (body, status)


def test_coerce_defaults_when_empty():
    assert _coerce_options({})["origins"] == ["*"]
    assert _coerce_options({})["max_age"] == 600


def test_coerce_drops_unknown_keys_and_supports_aliases():
    out = _coerce_options({"bogus": 1, "allow_origins": ["https://a.example"],
                           "allow_credentials": True, "allow_methods": ["GET"],
                           "allow_headers": ["Content-Type"], "exposeHeaders": ["X-Id"],
                           "maxAge": 10})
    assert "bogus" not in out
    assert out["origins"] == ["https://a.example"]
    assert out["credentials"] is True
    assert out["methods"] == ["GET"]
    assert out["headers"] == ["Content-Type"]
    assert out["expose_headers"] == ["X-Id"]
    assert out["max_age"] == 10


def test_as_list_variants():
    assert _as_list(None) == []
    assert _as_list("a, b ,,c") == ["a", "b", "c"]
    assert _as_list("*") == ["*"]
    assert _as_list("GET") == ["GET"]
    assert _as_list(["GET", 1]) == ["GET", "1"]
    assert _as_list(("a", "b")) == ["a", "b"]
    assert _as_list(123) == ["123"]


def test_max_age_rejects_non_int():
    with pytest.raises(ValueError, match="max_age"):
        make_cors_middleware(None, max_age="600")


@pytest.mark.asyncio
async def test_default_wildcard_origin_without_credentials():
    mw = make_cors_middleware(None)
    res = _Res()
    await mw(_req(headers={"origin": "https://any.example"}), res)
    assert res.set_calls["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in res.set_calls
    assert res.set_calls["Vary"] == "Origin"
    assert res.set_calls["Access-Control-Max-Age"] == "600"
    assert res.sent is None


@pytest.mark.asyncio
async def test_no_origin_skips_entirely():
    mw = make_cors_middleware(None)
    res = _Res()
    await mw(_req(headers={}), res)
    assert res.set_calls == {}


@pytest.mark.asyncio
async def test_disallowed_origin_skips():
    mw = make_cors_middleware(None, origins=["https://a.example"])
    res = _Res()
    await mw(_req(headers={"origin": "https://evil.example"}), res)
    assert res.set_calls == {}


@pytest.mark.asyncio
async def test_vary_merges_with_existing():
    mw = make_cors_middleware(None, origins=["https://a.example"])
    res = _Res(headers={"Vary": "Accept-Encoding"})
    await mw(_req(headers={"origin": "https://a.example"}), res)
    assert res.set_calls["Vary"] == "accept-encoding, origin"


@pytest.mark.asyncio
async def test_expose_headers_and_wildcard_methods():
    mw = make_cors_middleware(None, origins=["https://a.example"],
                              methods="*", expose_headers=["X-Req-Id"])
    res = _Res()
    await mw(_req("OPTIONS", {"origin": "https://a.example"}), res)
    assert res.set_calls["Access-Control-Expose-Headers"] == "X-Req-Id"
    assert res.set_calls["Access-Control-Allow-Methods"] == "*"
    assert res.set_calls["Access-Control-Allow-Headers"] == "*"
    assert res.sent == ("", 204)


@pytest.mark.asyncio
async def test_preflight_wildcard_headers_echoes_requested():
    mw = make_cors_middleware(None)
    res = _Res()
    await mw(_req("OPTIONS", {"origin": "https://any.example",
                              "access-control-request-headers": "X-Custom"}), res)
    assert res.set_calls["Access-Control-Allow-Headers"] == "X-Custom"
    assert res.sent == ("", 204)


@pytest.mark.asyncio
async def test_preflight_specific_headers_without_requested():
    mw = make_cors_middleware(None, origins=["https://a.example"],
                              headers=["Content-Type"])
    res = _Res()
    await mw(_req("OPTIONS", {"origin": "https://a.example"}), res)
    assert res.set_calls["Access-Control-Allow-Headers"] == "Content-Type"


@pytest.mark.asyncio
async def test_max_age_none_sets_no_max_age_header():
    mw = make_cors_middleware(None, max_age=None)
    preflight_res = _Res()
    await mw(_req("OPTIONS", {"origin": "https://any.example"}), preflight_res)
    assert "Access-Control-Max-Age" not in preflight_res.set_calls
    assert preflight_res.sent == ("", 204)
    get_res = _Res()
    await mw(_req(headers={"origin": "https://any.example"}), get_res)
    assert "Access-Control-Max-Age" not in get_res.set_calls


@pytest.mark.asyncio
async def test_string_origins_option_and_missing_headers_attr():
    mw = make_cors_middleware(None, origins="https://a.example, https://b.example")
    res = _Res()
    await mw(_req(headers={"origin": "https://b.example"}), res)
    assert res.set_calls["Access-Control-Allow-Origin"] == "https://b.example"
    bare_req = SimpleNamespace(method="GET", headers=None)
    res2 = _Res()
    await mw(bare_req, res2)
    assert res2.set_calls == {}


@pytest.mark.asyncio
async def test_integration_no_origin_and_max_age_none(app_factory, asgi_client):
    app = app_factory()
    app.use_cors(max_age=None)

    @app.get("/r")
    async def r(req, res):
        await res.send("ok")

    plain = await asgi_client(app, path="/r")
    assert "access-control-allow-origin" not in plain["headers"]
    preflight = await asgi_client(app, "OPTIONS", "/r", headers={"Origin": "https://x.example"})
    assert preflight["status"] == 204
    assert preflight["headers"]["access-control-allow-origin"] == ["*"]
    assert "access-control-max-age" not in preflight["headers"]
