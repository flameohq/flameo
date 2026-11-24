import json
import os
from types import SimpleNamespace

import pytest

import flameo.cycles as cycles
from flameo import Flameo
from flameo.cors import make_cors_middleware
from flameo.cycles import Request
from flameo.parser import RequestParser, cleanup_request_temp_files, parse_multipart_body_streaming
from flameo.flameo import File


async def _chunks(data):
    yield data


class _Res:
    def __init__(self):
        self.headers = {}
        self.sent = None

    def setHeader(self, key, value):
        self.headers[key] = value

    async def send(self, body, status=None):
        self.sent = (body, status)


class _Req:
    def __init__(self, method="GET", headers=None):
        self.method = method
        self.headers = headers or {}


@pytest.fixture
def parser_tmp(monkeypatch, tmp_path):
    import flameo.parser as parser
    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
def clean_tracked():
    yield


@pytest.fixture
def fake_symlink(monkeypatch):
    realpath = os.path.realpath
    mapping = {}

    def fake(path):
        return mapping.get(os.path.normpath(str(path)), realpath(path))

    monkeypatch.setattr(os.path, "realpath", fake)
    return mapping


async def test_upload_to_rejects_escaping_names(tmp_path, clean_tracked):
    dest = tmp_path / "uploads"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    names = (
        "../escaped.txt",
        "sub/../../escaped.txt",
        str(outside / "absolute.txt"),
    )
    if os.name == "nt":
        names += ("..\\escaped.txt",)
    for name in names:
        with pytest.raises(ValueError, match="inside the upload directory"):
            await File(file_content=b"x", filename="a.txt").upload_to(str(dest), name=name)
    assert not (tmp_path / "escaped.txt").exists()
    assert not (outside / "absolute.txt").exists()
    assert list(dest.iterdir()) == []


async def test_upload_to_rejects_client_filename_traversal(tmp_path, clean_tracked):
    dest = tmp_path / "uploads"
    dest.mkdir()
    with pytest.raises(ValueError, match="inside the upload directory"):
        await File(file_content=b"x", filename="../../evil.txt").upload_to(str(dest))
    assert list(dest.iterdir()) == []


def test_multipart_parser_sanitizes_client_filename(parser_tmp, clean_tracked):
    for raw in ("../../evil.txt", "..\\..\\evil.txt", "C:\\secret\\evil.txt"):
        body = (
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="'
            + raw.encode()
            + b'"\r\nContent-Type: text/plain\r\n\r\nhi\r\n--B--\r\n'
        )
        _, files = RequestParser.parse_multipart_body(
            "multipart/form-data; boundary=B", body
        )
        assert files["f"].filename == "evil.txt"


async def test_streaming_multipart_sanitizes_client_filename(parser_tmp, clean_tracked):
    body = (
        b'--B\r\nContent-Disposition: form-data; name="f"; filename="../../evil.txt"\r\n'
        b"\r\nhi\r\n--B--\r\n"
    )
    _, files, paths = await parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _chunks(body)
    )
    try:
        assert files["f"].filename == "evil.txt"
    finally:
        await cleanup_request_temp_files(paths)


async def test_trusted_proxy_ignores_client_spoofed_forwarded_prefix():
    async def receive():
        return {"type": "http.disconnect"}

    scope = {
        "type": "http", "method": "GET", "path": "/",
        "query_string": b"", "server": ("srv", 80), "client": ("10.0.0.1", 1234),
        "headers": [
            (b"x-forwarded-for", b"9.9.9.9, 1.2.3.4"),
            (b"x-forwarded-proto", b"http, https"),
        ],
    }
    req = Request(scope, receive, 1024, ["10.0.0.1"])
    assert req.ip == "1.2.3.4"
    assert req.secure is True
    assert req.protocol == "https"


def test_trust_ip_accepts_only_an_ip_list():
    assert Flameo(trust_ip=["10.0.0.1", "::1"]).trust_ip == frozenset({"10.0.0.1", "::1"})
    with pytest.raises(ValueError, match="trust_ip"):
        Flameo(trust_ip=True)
    with pytest.raises(ValueError, match="trust_ip"):
        Flameo(trust_ip="10.0.0.1")
    with pytest.raises(ValueError, match="invalid IP"):
        Flameo(trust_ip=["not-an-ip"])


def test_cors_rejects_wildcard_with_credentials():
    with pytest.raises(ValueError, match="credentials"):
        make_cors_middleware(None, credentials=True)
    with pytest.raises(ValueError, match="credentials"):
        make_cors_middleware(None, origins="*", credentials=True)


async def test_cors_preflight_does_not_reflect_disallowed_headers():
    mw = make_cors_middleware(
        None, origins=["https://a.example"], headers=["Content-Type"]
    )
    denied = _Res()
    await mw(
        _Req("OPTIONS", {
            "origin": "https://a.example",
            "access-control-request-headers": "X-Token",
        }),
        denied,
    )
    assert denied.headers["Access-Control-Allow-Headers"] == "Content-Type"

    allowed = _Res()
    await mw(
        _Req("OPTIONS", {
            "origin": "https://a.example",
            "access-control-request-headers": "Content-Type",
        }),
        allowed,
    )
    assert allowed.headers["Access-Control-Allow-Headers"] == "Content-Type"


async def _register_ip_route(app):
    @app.get("/ip")
    async def ip(req, res):
        await res.json({
            "ip": req.ip,
            "ips": req.ips,
            "secure": req.secure,
            "protocol": req.protocol,
        })


async def test_ip_from_rsgi_scope_string_client():
    class Proto:
        def response_bytes(self, *, status, headers, body):
            self.response = (status, headers, body)

    app = Flameo()

    @app.get("/ip")
    async def ip(req, res):
        await res.json({"ip": req.ip})

    scope = SimpleNamespace(
        method="GET", path="/ip", query_string=b"", headers={},
        scheme="http", server="srv:80", client="203.0.113.5:1234",
    )
    proto = Proto()
    await app.__rsgi__(scope, proto)
    assert json.loads(proto.response[2]) == {"ip": "203.0.113.5"}


async def test_forwarded_headers_ignored_by_default(app_factory, asgi_client):
    app = app_factory()
    await _register_ip_route(app)
    result = await asgi_client(
        app,
        path="/ip",
        headers={"X-Forwarded-For": "1.2.3.4", "X-Forwarded-Proto": "https"},
    )
    assert json.loads(result["body"]) == {
        "ip": "127.0.0.1", "ips": [], "secure": False, "protocol": "http",
    }


async def test_forwarded_headers_honored_when_trusted(app_factory, asgi_client):
    app = app_factory(trust_ip=["127.0.0.1"])
    await _register_ip_route(app)
    result = await asgi_client(
        app,
        path="/ip",
        headers={"X-Forwarded-For": "1.2.3.4", "X-Forwarded-Proto": "https"},
    )
    assert json.loads(result["body"]) == {
        "ip": "1.2.3.4", "ips": ["1.2.3.4"], "secure": True, "protocol": "https",
    }


async def test_forwarded_headers_honored_only_from_trusted_proxy():
    async def receive():
        return {"type": "http.disconnect"}

    def request(client):
        scope = {
            "type": "http", "method": "GET", "path": "/",
            "query_string": b"", "server": ("srv", 80), "client": client,
            "headers": [(b"x-forwarded-for", b"1.2.3.4")],
        }
        return Request(scope, receive, 1024, ["10.0.0.1"])

    trusted = request(("10.0.0.1", 1234))
    assert trusted.ip == "1.2.3.4"
    assert trusted.ips == ["1.2.3.4"]
    untrusted = request(("127.0.0.1", 1234))
    assert untrusted.ip == "127.0.0.1"
    assert untrusted.ips == []


async def test_use_static_rejects_symlink_outside_root(
    app_factory, asgi_client, tmp_path, fake_symlink
):
    root = tmp_path / "public"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    link = root / "leak.txt"
    link.write_text("placeholder")
    fake_symlink[os.path.normpath(str(link))] = str(secret)

    app = app_factory()
    app.use_static("/assets", str(root))
    result = await asgi_client(app, path="/assets/leak.txt")
    assert result["status"] == 403
    assert b"top secret" not in result["body"]


async def test_serve_static_rejects_symlink_outside_root(
    app_factory, asgi_client, tmp_path, fake_symlink
):
    root = tmp_path / "public"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    link = root / "leak.txt"
    link.write_text("placeholder")
    fake_symlink[os.path.normpath(str(link))] = str(secret)

    app = app_factory()
    app.serve_static("/legacy", str(root))
    result = await asgi_client(app, path="/legacy/leak.txt")
    assert result["status"] == 403
    assert b"top secret" not in result["body"]


def test_resolve_safe_path_rejects_symlink_swapped_after_cache(
    tmp_path, monkeypatch, fake_symlink
):
    handler = tmp_path / "handler.py"
    handler.write_text("", encoding="utf-8")
    frame = SimpleNamespace(filename=str(handler))
    monkeypatch.setattr(cycles.inspect, "stack", lambda: [frame, frame])
    cycles._PATH_CHECK_CACHE.clear()
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "file.txt"
    target.write_text("ok", encoding="utf-8")
    assert cycles._resolve_safe_path("sub/file.txt")[2] is None

    secret = tmp_path.parent / (tmp_path.name + ".secret")
    secret.write_text("secret", encoding="utf-8")
    fake_symlink[os.path.normpath(str(target))] = str(secret)
    assert cycles._resolve_safe_path("sub/file.txt")[2] == "escape"
    cycles._PATH_CHECK_CACHE.clear()
