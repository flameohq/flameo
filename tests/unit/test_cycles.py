from __future__ import annotations

import asyncio
import json
import zlib
from types import SimpleNamespace

import pytest

import flameo.cycles as cycles
from flameo.cycles import (
    ConverterRegistry,
    Request,
    Response,
    RSGIResponse,
    RouterMap,
    _drain_finishers,
    _extract_files_from_form_fields,
    _is_fresh,
    _looks_like_file_path,
    _media_matches,
    _mime_for,
    _normalize_headers,
    _orjson_default,
    _orjson_dumps_fast,
    _parse_accept,
    _parse_cookie_header,
    _parse_range_header,
    _resolve_safe_path,
    _response_header,
    _strip_port,
    _to_bool,
    _to_date,
    _to_datetime,
    _to_float,
    _to_int,
    _to_path,
    _to_slug,
    _to_uuid,
)


async def _empty_receive():
    return {"type": "http.disconnect"}


def make_request(
    *,
    method="GET",
    path="/",
    headers=None,
    query=b"",
    scheme="http",
    server=("testserver", 80),
    client=("127.0.0.1", 12345),
    rsgi=False,
    max_body=1024 * 1024,
    receive=None,
    trust_ip=(),
):
    raw_pairs = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    if rsgi:
        scope = SimpleNamespace(
            method=method,
            path=path,
            query_string=query,
            headers={k: v for k, v in raw_pairs},
            scheme=scheme,
            server=server,
            client=client,
        )
    else:
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query,
            "headers": raw_pairs,
            "scheme": scheme,
            "server": server,
            "client": client,
        }
    return Request(scope, receive or _empty_receive, max_body, trust_ip)


def request_with_body(body, content_type=None, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    if content_type is not None:
        headers["Content-Type"] = content_type
    req = make_request(headers=headers, **kwargs)
    req._body = body
    return req


def make_response(app, req=None):
    events = []

    async def send(event):
        events.append(event)

    return Response(send, app=app, req=req), events


def body_events(events):
    return [e for e in events if e["type"] == "http.response.body"]


def response_body(events):
    return b"".join(e.get("body", b"") for e in body_events(events))


def header_dict(events):
    start = next(e for e in events if e["type"] == "http.response.start")
    return dict(start.get("headers", []))


class FakeStream:
    def __init__(self, status, headers):
        self.status = status
        self.headers = headers
        self.chunks = []

    async def send_bytes(self, chunk):
        self.chunks.append(bytes(chunk))


class FakeProto:
    def __init__(self):
        self.streams = []

    def response_stream(self, status, headers):
        stream = FakeStream(status, headers)
        self.streams.append(stream)
        return stream


class AwaitableChunks:
    def __init__(self, chunks, exc=None):
        self.chunks = list(chunks)
        self.index = 0
        self.exc = exc

    def __await__(self):
        async def _next():
            if self.exc is not None:
                raise self.exc
            if self.index < len(self.chunks):
                value = self.chunks[self.index]
                self.index += 1
                return value
            return b""

        return _next().__await__()


class HeaderBag:
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        return iter(self.data)

    def get(self, name):
        return self.data.get(name)


def patch_safe_path(monkeypatch, path):
    monkeypatch.setattr(
        cycles,
        "_resolve_safe_path",
        lambda _p: (str(path), str(path.parent), None),
    )


def test_resolve_safe_path_success_cache_and_errors(tmp_path, monkeypatch):
    handler = tmp_path / "handler.py"
    handler.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        cycles.inspect,
        "stack",
        lambda: [
            SimpleNamespace(filename=str(handler)),
            SimpleNamespace(filename=str(handler)),
        ],
    )
    target = tmp_path / "sub" / "file.txt"
    target.parent.mkdir()
    target.write_text("hi", encoding="utf-8")

    assert _resolve_safe_path("sub/file.txt") == (str(target), str(tmp_path), None)
    assert _resolve_safe_path("sub/file.txt") == (str(target), str(tmp_path), None)
    target.unlink()
    assert _resolve_safe_path("sub/file.txt")[2] == "not_found"

    local = tmp_path / "local.txt"
    local.write_text("x", encoding="utf-8")
    assert _resolve_safe_path("local.txt")[2] == "same_dir"
    assert _resolve_safe_path("../outside.txt")[2] == "escape"
    assert _resolve_safe_path("sub/missing.txt")[2] == "not_found"


def test_resolve_safe_path_commonpath_failure_and_cache_eviction(tmp_path, monkeypatch):
    handler = tmp_path / "handler.py"
    handler.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        cycles.inspect,
        "stack",
        lambda: [
            SimpleNamespace(filename=str(handler)),
            SimpleNamespace(filename=str(handler)),
        ],
    )
    monkeypatch.setattr(
        cycles.os.path,
        "commonpath",
        lambda _paths: (_ for _ in ()).throw(ValueError("different drive")),
    )
    assert _resolve_safe_path("sub/file.txt")[2] == "escape"

    monkeypatch.undo()
    handler.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        cycles.inspect,
        "stack",
        lambda: [
            SimpleNamespace(filename=str(handler)),
            SimpleNamespace(filename=str(handler)),
        ],
    )
    target = tmp_path / "sub" / "cached.txt"
    target.parent.mkdir(exist_ok=True)
    target.write_text("y", encoding="utf-8")
    cache = cycles._PATH_CHECK_CACHE
    cache.clear()
    for index in range(cycles._PATH_CHECK_CACHE_MAX):
        cache[("filler", str(index))] = (str(target), str(tmp_path))
    assert _resolve_safe_path("sub/cached.txt")[2] is None
    assert len(cache) == cycles._PATH_CHECK_CACHE_MAX
    assert ("filler", "0") not in cache
    cache.clear()


def test_resolve_safe_path_introspection_fallbacks(tmp_path, monkeypatch):
    handler = tmp_path / "handler.py"
    handler.write_text("", encoding="utf-8")
    target = tmp_path / "sub" / "file.txt"
    target.parent.mkdir()
    target.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(
        cycles.inspect,
        "stack",
        lambda: [
            SimpleNamespace(filename=""),
            SimpleNamespace(filename=cycles.__file__),
            SimpleNamespace(filename=str(handler)),
        ],
    )
    assert _resolve_safe_path("sub/file.txt")[2] is None

    cycles._PATH_CHECK_CACHE.clear()
    monkeypatch.setattr(
        cycles.inspect,
        "stack",
        lambda: (_ for _ in ()).throw(RuntimeError("no stack")),
    )
    result = _resolve_safe_path("file.txt")
    assert len(result) == 3
    cycles._PATH_CHECK_CACHE.clear()


def test_normalize_headers_shapes_and_decoders():
    assert _normalize_headers(None) == {}
    assert _normalize_headers({b"X-A": b"1", "X-B": ["2", "3"]}) == {
        "x-a": "1",
        "x-b": "2",
    }
    assert _normalize_headers([]) == {}
    assert _normalize_headers([(b"X-A", b"1"), (b"X-B", b"2", b"ignored")]) == {
        "x-a": "1",
        "x-b": "2",
    }
    assert _normalize_headers([{b"X-A": b"1"}]) == {"x-a": "1"}
    assert _normalize_headers([(b"only-name",)]) == {}
    assert _normalize_headers([object()]) == {}
    assert _normalize_headers(HeaderBag({"X-Test": "yes"})) == {"x-test": "yes"}
    assert _normalize_headers([b"not-a-pair"]) == {"not-a-pair": "None"}


def test_header_and_cookie_and_address_helpers():
    assert cycles._decode_header_name(b"abc") == "abc"
    assert cycles._decode_header_name(5) == "5"
    assert cycles._decode_header_value([b"first", b"second"]) == "first"
    assert cycles._decode_header_value([]) == ""
    assert cycles._decode_header_value(7) == "7"

    assert _parse_cookie_header("") == {}
    assert _parse_cookie_header("a=1; b=\"two\"; broken; =; c=") == {
        "a": "1",
        "b": "two",
        "c": "",
    }

    assert _strip_port("") == ""
    assert _strip_port("127.0.0.1:60050") == "127.0.0.1"
    assert _strip_port("[::1]:5000") == "[::1]"
    assert _strip_port("[::1") == "[::1"
    assert _strip_port("::1") == "::1"
    assert _strip_port("host") == "host"


def test_mime_accept_and_media_matching():
    assert _mime_for("json") == "application/json"
    assert _mime_for(".css") == "text/css"
    assert _mime_for("text/plain") == "text/plain"
    assert isinstance(_mime_for("unknown-ext"), str)

    assert _parse_accept("application/json;q=0.5, text/html, */*;q=0") == [
        ("text/html", 1.0),
        ("application/json", 0.5),
    ]
    assert _parse_accept("text/html;q=bad, , application/xml;q=0.2") == [
        ("application/xml", 0.2)
    ]
    assert _parse_accept("") == []

    assert _media_matches("*/*", "text/html")
    assert _media_matches("text/*", "text/html")
    assert _media_matches("text/html", "text/html")
    assert not _media_matches("text/plain", "text")
    assert not _media_matches("application/json", "text/html")


def test_response_header_lookup_and_freshness():
    res = SimpleNamespace(
        _header_map={b"etag": b'"abc"', b"x-list": [b"one", b"two"]},
        _hdr=[("last-modified", "Wed, 21 Oct 2015 07:28:00 GMT")],
    )
    assert _response_header(None, "etag") == ""
    assert _response_header(res, "etag") == '"abc"'
    assert _response_header(res, "x-list") == "one"
    assert _response_header(res, "last-modified") == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert _response_header(res, "missing") == ""

    assert _is_fresh({}, res) is False
    assert _is_fresh({"if-none-match": '"abc"'}, res) is True
    assert _is_fresh({"if-none-match": 'W/"abc"'}, res) is True
    assert _is_fresh({"if-none-match": '"other"'}, res) is False
    assert _is_fresh({"if-none-match": "*"}, res) is True
    assert _is_fresh({"cache-control": "no-cache", "if-none-match": '"abc"'}, res) is False
    assert _is_fresh({"if-modified-since": "Wed, 21 Oct 2015 07:28:01 GMT"}, res) is True
    assert _is_fresh({"if-modified-since": "Wed, 21 Oct 2015 07:27:00 GMT"}, res) is False
    assert _is_fresh({"if-modified-since": "not-a-date"}, res) is False
    assert _is_fresh({"if-none-match": '"abc"'}, SimpleNamespace(_hdr=[], _header_map={})) is False
    assert _is_fresh({"if-modified-since": "Wed, 21 Oct 2015 07:28:01 GMT"},
                     SimpleNamespace(_hdr=[], _header_map={})) is False


def test_range_parsing_and_request_range_combine():
    assert _parse_range_header("", 10) is None
    assert _parse_range_header("items=0-1", 10) == -2
    assert _parse_range_header("bytes=a-b", 10) == -2
    assert _parse_range_header("bytes=0-1", "bad") == -2
    assert _parse_range_header("bytes=0-1", 0) == -1
    assert _parse_range_header("bytes=", 10) == -2
    assert _parse_range_header("bytes=abc", 10) == -2
    assert _parse_range_header("bytes=-", 10) == -2
    assert _parse_range_header("bytes=-0", 10) == -1
    assert _parse_range_header("bytes=5-2", 10) == -1
    assert _parse_range_header("bytes=20-30", 10) == -1
    assert _parse_range_header("bytes=-2", 10) == [{"start": 8, "end": 9}]
    assert _parse_range_header("bytes=0-100", 10) == [{"start": 0, "end": 9}]
    parsed = _parse_range_header("bytes=0-1,2-3,10-11", 20)
    assert parsed == [
        {"start": 0, "end": 1},
        {"start": 2, "end": 3},
        {"start": 10, "end": 11},
    ]

    req = make_request(headers={"Range": "bytes=0-1,2-3,10-11"})
    assert req.range(20) == parsed
    assert req.range(20, {"combine": True}) == [
        {"start": 0, "end": 3},
        {"start": 10, "end": 11},
    ]


def test_file_path_heuristics_and_form_file_extraction():
    assert _looks_like_file_path("C:\\fakepath\\a.png") is True
    assert _looks_like_file_path("/home/user/a.png") is True
    assert _looks_like_file_path("photo.png") is True
    assert _looks_like_file_path("plain text") is False
    assert _looks_like_file_path("") is False
    assert _looks_like_file_path(123) is False

    fields, files = _extract_files_from_form_fields(
        {"title": "hello", "photo": "C:\\fakepath\\a.png"}
    )
    assert fields == {"title": "hello"}
    assert files["photo"].filename == "a.png"
    assert files["photo"].content == b""


def test_converter_registry_validation_and_custom_converter():
    registry = ConverterRegistry()
    assert "int" in registry.available()
    with pytest.raises(ValueError):
        registry.get("missing")
    with pytest.raises(ValueError):
        registry.register("", r".+", str)
    with pytest.raises(ValueError):
        registry.register("not valid", r".+", str)
    with pytest.raises(ValueError):
        registry.register("int", r".+", str)
    with pytest.raises(ValueError):
        registry.register("custom", "", str)
    with pytest.raises(TypeError):
        registry.register("custom", r".+", "not callable")
    registry.register("hex", r"[0-9a-f]+", lambda value: int(value, 16))
    assert registry.get("hex")[1]("ff") == 255
    assert "hex" in registry.available()

    routes = RouterMap()
    routes.register_converter("hex", r"[0-9a-f]+", lambda value: int(value, 16))
    handler = object()
    routes.add_route("GET", "/hex/{value:hex}", handler)
    assert routes.match("GET", "/hex/ff")[0] is handler
    assert routes.match("GET", "/hex/ff")[1]["value"] == 255


def test_converter_scalar_functions():
    assert _to_int("-3") == -3
    assert _to_float("1.5") == 1.5
    assert str(_to_uuid("12345678-1234-1234-1234-123456789abc")) == "12345678-1234-1234-1234-123456789abc"
    assert _to_date("2020-01-02").isoformat() == "2020-01-02"
    assert _to_datetime("2020-01-02T03:04:05").isoformat() == "2020-01-02T03:04:05"
    assert _to_bool("YES") is True
    assert _to_bool("off") is False
    with pytest.raises(ValueError):
        _to_bool("maybe")
    assert _to_slug("a-b") == "a-b"
    assert _to_path("a/b") == "a/b"


def test_datetime_fallback_branch(monkeypatch):
    class FakeDatetime:
        @classmethod
        def fromisoformat(cls, value):
            raise ValueError("unsupported")

        @classmethod
        def strptime(cls, value, fmt):
            return (value, fmt)

    monkeypatch.setattr(cycles, "datetime", FakeDatetime)
    assert _to_datetime("2020-01-01T00:00:00") == (
        "2020-01-01T00:00:00",
        "%Y-%m-%dT%H:%M:%S",
    )


def test_router_map_typed_routes_wildcards_and_cache():
    routes = RouterMap()
    static_handler = object()
    routes.add_route("get", "/static", static_handler)
    assert routes.match("GET", "/static")[0] is static_handler
    assert routes.match("GET", "/static")[0] is static_handler

    typed = {}
    routes.add_route("GET", "/orders/{order_id:int}", typed)
    assert routes.match("GET", "/orders/7")[1]["order_id"] == 7
    assert routes.match("GET", "/orders/-3")[1]["order_id"] == -3
    assert routes.match("GET", "/orders/nope")[0] is None

    routes.add_route("GET", "/users/:user_id", typed)
    assert routes.match("GET", "/users/42")[1]["user_id"] == "42"

    routes.add_route("GET", "/flags/{flag:bool}", typed)
    assert routes.match("GET", "/flags/true")[1]["flag"] is True
    assert routes.match("GET", "/flags/maybe")[0] is None

    routes.add_route("GET", "/floats/{value:float}", typed)
    assert routes.match("GET", "/floats/1.25")[1]["value"] == 1.25

    routes.add_route("GET", "/dates/{value:date}", typed)
    assert routes.match("GET", "/dates/2020-01-02")[1]["value"].isoformat() == "2020-01-02"

    routes.add_route("GET", "/slugs/{value:slug}", typed)
    assert routes.match("GET", "/slugs/a-b_c")[1]["value"] == "a-b_c"
    assert routes.match("GET", "/slugs/abc123")[1]["value"] == "abc123"

    wildcard_handler = object()
    routes.add_route("GET", "/files/*{rest:path}", wildcard_handler)
    matched = routes.match("GET", "/files/a/b/c")
    assert matched[0] is wildcard_handler
    assert matched[1]["rest"] == "a/b/c"

    routes.add_route("GET", "/*{rest:path}", wildcard_handler)
    merged = routes.match("GET", "/users/9")
    assert merged[1]["user_id"] == "9"
    assert routes.match("GET", "/other/x")[0] is wildcard_handler

    for bad in (
        "/*:bad-name",
        "/*:rest:path",
        "/*<rest:path>",
        "/*{rest",
        "/bad/:value:nope",
        "/bad/:value:int",
        "/bad/{value:nope}",
        "/bad/<value:int>",
        "/bad/{value",
        "/bad/{value>",
        "/bad/{value:time}",
        "/bad/{value:alphanum}",
    ):
        with pytest.raises(ValueError):
            routes.add_route("GET", bad, static_handler)

    bare = RouterMap()
    bare.add_route("GET", "/v/*rest", static_handler)
    assert bare.match("GET", "/v/abc")[1]["rest"] == "abc"
    assert bare.match("GET", "/v/a/b")[0] is None

    overflow = RouterMap()
    for index in range(2050):
        overflow._cache_result(("GET", f"/{index}"), (None, {}, []))
    assert len(overflow._cache) == 2048


def test_route_param_formats():
    routes = RouterMap()
    handler = object()

    # :name -> str
    routes.add_route("GET", "/colon/:id", handler)
    assert routes.match("GET", "/colon/42")[1]["id"] == "42"

    # {name} -> str
    routes.add_route("GET", "/brace/{id}", handler)
    assert routes.match("GET", "/brace/42")[1]["id"] == "42"

    # {name:type} -> typed
    routes.add_route("GET", "/typed/{id:int}", handler)
    assert routes.match("GET", "/typed/42")[1]["id"] == 42
    assert routes.match("GET", "/typed/abc")[0] is None


def test_route_param_rejected_formats():
    routes = RouterMap()
    handler = object()
    for path in (
        "/bad/:id:int",
        "/bad/<id>",
        "/bad/<id:int>",
        "/bad/{id",
        "/bad/<id}",
        "/bad/:id>",
        "/*<rest:path>",
        "/*:rest:path",
    ):
        with pytest.raises(ValueError):
            routes.add_route("GET", path, handler)


def test_request_lazy_properties_asgi_and_rsgi_scopes():
    req = make_request(headers={"Host": "example.com", "Cookie": "a=1"}, query=b"x=1&x=2")
    assert req.headers["host"] == "example.com"
    assert req.headers is req.headers
    assert req.query == {"x": ["1", "2"]}
    assert req.cookies == {"a": "1"}
    assert req.originalUrl == "/?x=1&x=2"
    with pytest.raises(AttributeError):
        req.not_a_property

    single = make_request(query=b"x=1")
    assert single.query == {"x": "1"}
    str_query = make_request(query="a=1")
    assert str_query._query_raw == b"a=1"
    assert str_query.originalUrl == "/?a=1"
    no_query = make_request()
    assert no_query.originalUrl == "/"

    rsgi = make_request(rsgi=True, headers={"Host": "rsgi.test"}, query=b"x=9")
    assert rsgi.headers["host"] == "rsgi.test"
    assert rsgi.query == {"x": "9"}
    assert rsgi.originalUrl == "/?x=9"


def test_request_url_parts_variants():
    req = make_request(
        path="/x",
        trust_ip=("127.0.0.1",),
        headers={
            "Host": "example.com:8080",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "1.2.3.4, 5.6.7.8",
        },
    )
    assert req.protocol == "https"
    assert req.secure is True
    assert req.hostname == "example.com"
    assert req.ip == "5.6.7.8"
    assert req.url == "https://example.com:8080/x"
    assert req.url == "https://example.com:8080/x"

    default_port = make_request(path="/x", headers={"Host": "example.com:80"})
    assert default_port.url == "http://example.com/x"

    ipv6 = make_request(path="/x", headers={"Host": "[::1]:5000"})
    assert ipv6.hostname == "[::1]"
    assert ipv6.url == "http://[::1]:5000/x"

    fallback = make_request(path="/x", server=("srv.example", 9000), client=None)
    assert fallback.hostname == "srv.example"
    assert fallback.ip == ""
    assert fallback.url == "http://srv.example:9000/x"

    dict_server = make_request(path="/x", server={"hostname": "dict.example", "port": 81}, client={"ip": "9.9.9.9:7"})
    assert dict_server.hostname == "dict.example"
    assert dict_server.ip == "9.9.9.9"
    assert dict_server.url == "http://dict.example:81/x"

    str_client = make_request(path="/x", client="8.8.8.8:5")
    assert str_client.ip == "8.8.8.8"

    forwarded_proto = make_request(path="/x", trust_ip=("127.0.0.1",), headers={"X-Forwarded-Proto": "http, HTTPS"})
    assert forwarded_proto.protocol == "https"

    rsgi = make_request(rsgi=True, path="/x", headers={"Host": "rsgi.example:7000"}, client={"host": "7.7.7.7:1"})
    assert rsgi.url == "http://rsgi.example:7000/x"
    assert rsgi.ip == "7.7.7.7"

    no_host = make_request(path="/x", server=None, client=None)
    assert no_host.hostname == ""
    assert no_host.url == "http:///x"

    bad_port = make_request(path="/x", headers={"Host": "h.example:not-a-port"})
    assert bad_port.hostname == "h.example"
    assert bad_port.url == "http://h.example/x"


def test_request_metadata_helpers_and_negotiation():
    req = make_request(
        path="/x",
        query=b"q=1",
        trust_ip=("127.0.0.1",),
        headers={
            "Host": "a.b.example.com",
            "Accept": "application/json;q=0.9, text/html",
            "Content-Type": "application/json; charset=utf-8",
            "X-Forwarded-For": "1.1.1.1, 2.2.2.2",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://ref.example",
        },
    )
    req._base_url = "/api"
    assert req.host == "a.b.example.com"
    assert req.ips == ["1.1.1.1", "2.2.2.2"]
    assert req.xhr is True
    assert req.baseUrl == "/api"
    assert req.base_url == "/api"
    assert req.pathname == "/x"
    assert req.original_url == "/x?q=1"
    assert req.remote_addr == "2.2.2.2"
    assert req.subdomains == ["b", "a"]
    assert req.get(b"Referer") == "https://ref.example"
    assert req.header("referrer") == "https://ref.example"

    assert req.accepts() == ["text/html", "application/json"]
    assert req.accepts("html") == "html"
    assert req.accepts(["json"]) == "json"
    assert req.accepts("xml") is False
    assert req.is_() == "application/json"
    assert req.is_("json") == "json"
    assert req.is_(["xml", "json"]) == "json"
    assert req.is_("xml") is False
    assert req.is_type("json") == "json"

    no_content_type = make_request()
    assert no_content_type.is_() is None
    assert no_content_type.subdomains == []


def test_request_parse_body_content_types(tmp_path, monkeypatch):
    import flameo.parser as parser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)

    assert request_with_body(b"", "application/json").parse_body() == ({}, {})
    assert request_with_body(b"\xef\xbb\xbf{\"a\":1}", "application/json").parse_body()[0] == {"a": 1}
    assert request_with_body(b"\xef\xbb\xbf", "application/json").parse_body() == ({}, {})
    with pytest.raises(ValueError):
        request_with_body(b"{bad", "application/json").parse_body()

    fields, files = request_with_body(
        b"title=hi&photo=C%3A%5Cfakepath%5Ca.png",
        "application/x-www-form-urlencoded",
    ).parse_body()
    assert fields == {"title": "hi"}
    assert files["photo"].filename == "a.png"
    assert files["photo"].content == b""

    text_fields, text_files = request_with_body(
        b"name=Ada\ncity=Paris", "text/plain"
    ).parse_body()
    assert text_fields == {"name": "Ada", "city": "Paris"}
    assert text_files["body"].content == b"name=Ada\ncity=Paris"

    raw_fields, raw_files = request_with_body(b"raw text", "text/plain").parse_body()
    assert raw_fields == {"body": "raw text"}
    assert raw_files["body"].content == b"raw text"

    invalid_fields, invalid_files = request_with_body(b"\xff\xfe", "text/plain").parse_body()
    assert invalid_fields == {"body": "\ufffd\ufffd"}
    assert invalid_files["body"].content == b"\xff\xfe"

    assert request_with_body(b"anything", "application/octet-stream").parse_body() == (
        "anything",
        {},
    )

    multipart = (
        b"--multi\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
        b"hello\r\n--multi\r\n"
        b"Content-Disposition: form-data; name=\"upload\"; filename=\"a.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\ndata\r\n--multi--\r\n"
    )
    body_fields, multipart_files = request_with_body(
        multipart, "multipart/form-data; boundary=multi"
    ).parse_body()
    assert body_fields == {"title": "hello"}
    uploaded = multipart_files["upload"]
    assert uploaded.filename == "a.txt"
    assert uploaded.size == 4
    assert uploaded.temp_path is None
    assert uploaded.content == b"data"


async def test_request_body_load_buffered_and_large(monkeypatch):
    req = make_request(method="POST", headers={"Content-Type": "application/json"})
    messages = iter(
        [
            {"type": "http.request", "body": b'{"a":', "more_body": True},
            {"type": "http.request", "body": b"1}", "more_body": False},
        ]
    )

    async def receive():
        return next(messages)

    req.receive = receive
    assert await req.body == {"a": 1}
    assert await req.body == {"a": 1}
    assert await req.files == {}

    payload = json.dumps({"blob": "x" * 140000}).encode()
    large = make_request(
        method="POST",
        headers={"Content-Type": "application/json"},
        max_body=10_000_000,
    )

    async def large_receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    large.receive = large_receive
    parsed = await large.body
    assert len(parsed["blob"]) == 140000

    skipped = make_request(method="POST", max_body=5)
    skipped_messages = iter(
        [
            {"type": "other"},
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def skipped_receive():
        return next(skipped_messages)

    skipped.receive = skipped_receive
    with pytest.raises(ValueError):
        await skipped.body

    per_chunk = make_request(method="POST", max_body=2)

    async def per_chunk_receive():
        return {"type": "http.request", "body": b"123", "more_body": False}

    per_chunk.receive = per_chunk_receive
    with pytest.raises(ValueError):
        await per_chunk.body


async def test_request_body_load_rsgi_shapes_and_errors():
    req = make_request(method="POST", rsgi=True)
    req.receive = AwaitableChunks([b"he", b"llo", b""])
    assert await req.body == "hello"

    broken = make_request(method="POST", rsgi=True)
    broken.receive = AwaitableChunks([], exc=RuntimeError("gone"))
    assert await broken.body == {}

    odd = make_request(method="POST")
    odd.receive = AwaitableChunks(["garbage"])
    with pytest.raises(RuntimeError):
        await odd.body

    per_chunk = make_request(method="POST", max_body=5)
    per_chunk.receive = AwaitableChunks([b"123456"])
    with pytest.raises(ValueError):
        await per_chunk.body

    cumulative = make_request(method="POST", max_body=5)
    cumulative.receive = AwaitableChunks([b"123", b"456"])
    with pytest.raises(ValueError):
        await cumulative.body


async def test_request_multipart_streaming_body_and_errors(tmp_path, monkeypatch):
    import flameo.parser as parser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)
    boundary = "split"
    body = (
        b"--split\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
        b"hi\r\n--split\r\n"
        b"Content-Disposition: form-data; name=\"upload\"; filename=\"a.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\ndata\r\n--split--\r\n"
    )
    chunks = [body[start:start + 5] for start in range(0, len(body), 5)]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": True}
        for chunk in chunks
    ] + [{"type": "http.request", "body": b"", "more_body": False}]
    iterator = iter(messages)

    async def receive():
        return next(iterator)

    req = make_request(
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        receive=receive,
    )
    fields = await req.body
    files = await req.files
    assert fields == {"title": "hi"}
    uploaded = files["upload"]
    assert uploaded.size == 4
    assert uploaded.temp_path is None
    assert uploaded.content == b"data"

    rsgi_req = make_request(
        method="POST",
        rsgi=True,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    rsgi_req.receive = AwaitableChunks(chunks + [b""])
    assert (await rsgi_req.files)["upload"].size == 4
    assert (await rsgi_req.files)["upload"].content == b"data"

    limited = make_request(
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        max_body=4,
    )
    limited.receive = AwaitableChunks([b"12345"])
    with pytest.raises(ValueError):
        await limited.body

    unexpected = make_request(
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    unexpected.receive = AwaitableChunks(["bad"])
    with pytest.raises(RuntimeError):
        await unexpected.body

    cumulative = make_request(
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        max_body=10,
    )
    cumulative.receive = AwaitableChunks([b"abcd", b"efgh", b"ijkl"])
    with pytest.raises(ValueError):
        await cumulative.body

    skipped = make_request(
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    class SkippingReceive:
        def __init__(self):
            self.messages = [
                {"type": "other"},
                {"type": "http.request", "body": b"", "more_body": False},
            ]

        async def __call__(self):
            return self.messages.pop(0)

    skipped.receive = SkippingReceive()
    await skipped.body

    failing = make_request(
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    failing.receive = AwaitableChunks([], exc=RuntimeError("gone"))
    await failing.body


def test_drain_finishers_runs_sync_async_and_ignores_failures():
    async def run():
        events = []
        res = SimpleNamespace(_finishers__=[])
        res._finishers__ = [
            lambda: events.append("sync"),
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        ]

        async def async_finisher():
            events.append("async")

        res._finishers__.append(async_finisher)
        _drain_finishers(res)
        assert res._finishers__ == []
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert "sync" in events
        assert "async" in events
        _drain_finishers(res)

    asyncio.run(run())


async def test_drain_finishers_runtime_error_branch(monkeypatch):
    res = SimpleNamespace(_finishers__=[lambda: object()])
    monkeypatch.setattr(cycles._asyncio, "iscoroutine", lambda _value: True)
    monkeypatch.setattr(
        cycles._asyncio,
        "ensure_future",
        lambda _value: (_ for _ in ()).throw(RuntimeError("no loop")),
    )
    _drain_finishers(res)
    assert res._finishers__ == []


def test_response_lazy_attributes_status_and_finish(app_factory):
    app = app_factory()
    req = make_request()
    res = Response(None, app=app, req=req)
    assert req._res is res
    assert res.req is req

    send_file = res.sendFile
    assert callable(send_file)
    assert res.sendFile is send_file
    download = res.download
    assert callable(download)
    assert res.download is download
    with pytest.raises(AttributeError):
        res.unknown_attribute

    with pytest.raises(TypeError):
        res.finish(123)
    assert res.finish(lambda: None) is res
    assert res.after(lambda: None) is res

    assert res.status(201) is res
    assert res._status == 201
    with pytest.raises(ValueError):
        res.status(99)
    with pytest.raises(ValueError):
        res.status("200")
    res._sent = True
    assert res.status(500) is res
    assert res._status == 201


def test_response_cookies_clear_and_redirect(app_factory):
    app = app_factory()
    res = Response(None, app=app)
    res.cookie(
        "theme",
        "dark",
        maxAge=60,
        expires="soon",
        domain="example.com",
        path="/app",
        httpOnly=True,
        secure=True,
        sameSite="none",
        partitioned=True,
        priority="high",
    )
    value = res._header_map[b"set-cookie"][0].decode()
    for token in (
        "theme=dark",
        "Max-Age=60",
        "Expires=soon",
        "Domain=example.com",
        "Path=/app",
        "HttpOnly",
        "Secure",
        "SameSite=None",
        "Partitioned",
        "Priority=High",
    ):
        assert token in value
    with pytest.raises(ValueError):
        res.cookie("bad", "1", priority="urgent")

    res.clearCookie(
        "theme",
        domain="example.com",
        httpOnly=True,
        secure=True,
        sameSite="lax",
        partitioned=True,
        priority="low",
        path="/app",
    )
    cleared = res._header_map[b"set-cookie"][0].decode()
    assert "theme=" in cleared and "Priority=Low" in cleared
    with pytest.raises(ValueError):
        res.clearCookie("theme", priority="urgent")

    req = make_request(headers={"Cookie": "a=1; b=2"})
    multi = Response(None, app=app, req=req)
    multi.clearAllCookies(domain="example.com", httpOnly=True, secure=True,
                          sameSite="none", partitioned=True, priority="medium")
    assert len(multi._header_map[b"set-cookie"]) == 2
    assert all(b"Priority=Medium" in chunk for chunk in multi._header_map[b"set-cookie"])
    with pytest.raises(ValueError):
        multi.clearAllCookies(priority="urgent")

    empty = Response(None, app=app)
    assert empty.clearAllCookies() is empty
    assert empty._header_map is None
    empty._sent = True
    assert empty.clearAllCookies() is empty

    res._sent = True
    assert res.cookie("x", "1") is res
    assert res.clearCookie("x") is res
    assert res.clearAllCookies() is res


async def test_response_redirect_and_jsonp(app_factory):
    app = app_factory()
    res, events = make_response(app)
    await res.redirect("/target", code=307)
    assert events[0]["status"] == 307
    assert header_dict(events)[b"location"] == b"/target"
    assert response_body(events) == b""

    req = make_request(query=b"callback=cb")
    jsonp_res, jsonp_events = make_response(app, req)
    await jsonp_res.jsonp({"a": 1}, status=202)
    assert jsonp_events[0]["status"] == 202
    assert response_body(jsonp_events).startswith(b"/**/ typeof cb ")
    assert b"cb(" in response_body(jsonp_events)

    list_req = make_request(query=b"callback=a&callback=b")
    list_res, list_events = make_response(app, list_req)
    await list_res.jsonp({"x": 1})
    assert response_body(list_events).startswith(b"/**/ typeof a ")

    invalid_req = make_request(query=b"callback=bad-name")
    invalid_res, invalid_events = make_response(app, invalid_req)
    await invalid_res.jsonp({"x": 1})
    assert json.loads(response_body(invalid_events)) == {"x": 1}

    no_req_res, no_req_events = make_response(app)
    await no_req_res.jsonp({"x": 2})
    assert json.loads(response_body(no_req_events)) == {"x": 2}


async def test_response_write_and_end_buffered(app_factory):
    app = app_factory()
    res, events = make_response(app)
    assert await res.write(None) is True
    assert await res.write("hi") is True
    assert await res.write(b" there") is True
    assert await res.write(bytearray(b"!")) is True
    assert await res.write(5) is True
    assert events == []
    await res.end()
    assert response_body(events) == b"hi there!5"
    assert header_dict(events)[b"content-type"].startswith(b"text/plain")
    assert header_dict(events)[b"x-content-type-options"] == b"nosniff"
    await res.end()
    assert await res.write("ignored") is False

    preset, preset_events = make_response(app)
    preset.setHeader("Content-Type", "text/custom")
    preset.setHeader("X-Content-Type-Options", "custom")
    await preset.write("x")
    await preset.end("y")
    assert response_body(preset_events) == b"xy"
    assert header_dict(preset_events)[b"content-type"] == b"text/custom"
    assert header_dict(preset_events)[b"x-content-type-options"] == b"custom"

    for chunk, expected in (("end", b"end"), (b"raw", b"raw"), (123, b"123")):
        chunk_res, chunk_events = make_response(app)
        await chunk_res.end(chunk)
        assert response_body(chunk_events) == expected

    empty_res, empty_events = make_response(app)
    await empty_res.end()
    assert empty_events[0]["status"] == 200
    assert response_body(empty_events) == b""
    assert header_dict(empty_events)[b"content-type"].startswith(b"text/plain")

    manual, manual_events = make_response(app)
    manual._chunks.append(b"manual")
    await manual.end()
    assert response_body(manual_events) == b"manual"
    assert header_dict(manual_events)[b"content-type"] == b"text/plain; charset=utf-8"


async def test_response_stream_and_streaming_write(app_factory):
    app = app_factory()
    res, events = make_response(app)
    assert await res.stream("a") is True
    assert header_dict(events)[b"content-type"].startswith(b"text/event-stream")
    assert header_dict(events)[b"cache-control"] == b"no-cache"
    assert header_dict(events)[b"connection"] == b"keep-alive"
    assert header_dict(events)[b"x-accel-buffering"] == b"no"
    assert await res.stream(123) is True
    await res.end("z")
    assert response_body(events) == b"a123z"
    await res.end()
    assert await res.stream("late") is False

    custom, custom_events = make_response(app)
    custom.setHeader("Content-Type", "text/custom")
    await custom.stream("x")
    assert header_dict(custom_events)[b"content-type"] == b"text/custom"

    sse, sse_events = make_response(app)
    sse.setHeader("Content-Type", "text/event-stream")
    assert await sse.write("one") is True
    assert await sse.write("two") is True
    await sse.end(3)
    assert response_body(sse_events) == b"onetwo3"
    starts = [e for e in sse_events if e["type"] == "http.response.start"]
    assert len(starts) == 1

    multi_header, multi_events = make_response(app)
    multi_header._streaming = True
    multi_header._header_map = {b"set-cookie": [b"a=1", b"b=2"]}
    await multi_header.write("x")
    multi_start = next(e for e in multi_events if e["type"] == "http.response.start")
    cookies = [value for key, value in multi_start["headers"] if key == b"set-cookie"]
    assert cookies == [b"a=1", b"b=2"]

    no_send = Response(None, app=app)
    assert await no_send.stream("buffered") is True
    await no_send.end()
    assert no_send._sent_body == b"buffered"

    async def failing_send(event):
        raise RuntimeError("closed")

    failing = Response(failing_send, app=app)
    with pytest.raises(RuntimeError):
        await failing.stream("x")
    assert failing._ended is True

    failing_write = Response(failing_send, app=app)
    failing_write._streaming = True
    with pytest.raises(RuntimeError):
        await failing_write.write("x")
    assert failing_write._ended is True

    async def failing_body_send(event):
        if event["type"] == "http.response.body":
            raise RuntimeError("closed")
        return None

    streaming_body_failure = Response(failing_body_send, app=app)
    with pytest.raises(RuntimeError):
        await streaming_body_failure.stream("x")
    assert streaming_body_failure._ended is True


async def test_response_terminal_methods(app_factory, tmp_path):
    app = app_factory()
    res, events = make_response(app)
    await res.json({"x": 1})
    assert json.loads(response_body(events)) == {"x": 1}
    await res.send("ignored")
    assert json.loads(response_body(events)) == {"x": 1}

    status_res, status_events = make_response(app)
    await status_res.sendStatus(404)
    assert status_events[0]["status"] == 404
    assert response_body(status_events) == b"Not Found"
    invalid_status, _ = make_response(app)
    with pytest.raises(ValueError):
        await invalid_status.sendStatus(99)

    unknown_status, unknown_events = make_response(app)
    await unknown_status.sendStatus(599)
    assert unknown_events[0]["status"] == 599
    assert response_body(unknown_events) == b""

    export_res, export_events = make_response(app)
    await export_res.export("text", "a.txt")
    assert response_body(export_events) == b"text"
    assert header_dict(export_events)[b"content-type"].startswith(b"text/plain")
    assert b"attachment" in header_dict(export_events)[b"content-disposition"]

    bytes_res, bytes_events = make_response(app)
    await bytes_res.export(b"bytes", "a.zzz", headers={"X-Extra": "1"})
    assert response_body(bytes_events) == b"bytes"
    assert header_dict(bytes_events)[b"x-extra"] == b"1"
    assert header_dict(bytes_events)[b"content-type"] == b"application/octet-stream"

    bytearray_res, bytearray_events = make_response(app)
    await bytearray_res.export(bytearray(b"z"), "archive.tar.gz")
    assert response_body(bytearray_events) == b"z"

    bad_export, _ = make_response(app)
    with pytest.raises(TypeError):
        await bad_export.export(123, "a.txt")

    no_template, _ = make_response(app)
    with pytest.raises(RuntimeError):
        await no_template.render("missing.html")

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "hello.html").write_text("{{ name | shout }}", encoding="utf-8")
    template_app = app_factory()
    template_app.use_template(str(template_dir))

    def shout(value):
        return str(value).upper()

    render_res, render_events = make_response(template_app)
    await render_res.render("hello.html", {"name": "flameo"}, filters=[shout])
    assert response_body(render_events) == b"FLAMEO"


async def test_use_template_globals_merged_on_render(app_factory, tmp_path):
    template_dir = tmp_path / "globals_templates"
    template_dir.mkdir()
    (template_dir / "greet.html").write_text(
        "{{ greeting }}, {{ name }}!", encoding="utf-8"
    )

    app = app_factory()
    with pytest.raises(TypeError):
        app.use_template(str(template_dir), globals="nope")

    app.use_template(
        str(template_dir), globals={"greeting": "Hello", "name": "base"}
    )

    # Base dict used when render() supplies no context.
    res, events = make_response(app)
    await res.render("greet.html")
    assert response_body(events) == b"Hello, base!"

    # Duplicate key: render()'s context wins over use_template() globals.
    override_res, override_events = make_response(app)
    await override_res.render("greet.html", {"name": "flameo"})
    assert response_body(override_events) == b"Hello, flameo!"

    # Globals only apply to render(); other responses are untouched.
    json_res, json_events = make_response(app)
    await json_res.json({"greeting": "ignored"})
    assert json.loads(response_body(json_events)) == {"greeting": "ignored"}

    # RSGI mirror behaves identically.
    rsgi_res = RSGIResponse(app=app)
    await rsgi_res.render("greet.html", {"name": "rsgi"})
    assert rsgi_res._body == b"Hello, rsgi!"


def test_orjson_default_and_numpy_serialization(app_factory):
    class Plain:
        def __init__(self):
            self.value = 3

    class Slotted:
        __slots__ = ()

    assert _orjson_default(Plain()) == {"value": 3}
    assert isinstance(_orjson_default(Slotted()), str)

    np = pytest.importorskip("numpy")

    assert json.loads(_orjson_dumps_fast(np.int64(7))) == 7

    app = app_factory()
    res, events = make_response(app)
    asyncio.run(res.json({"plain": Plain(), "numpy": np.int64(5)}))
    assert json.loads(response_body(events)) == {"plain": {"value": 3}, "numpy": "5"}


async def test_response_send_body_cache_compression_and_errors(app_factory):
    app = app_factory()
    app.use_compression(min_size=1)
    req = make_request(headers={"Accept-Encoding": "gzip"})
    res, events = make_response(app, req)
    capture = {}
    res._cache_capture__ = capture
    res._finalize_session__ = lambda: (_ for _ in ()).throw(RuntimeError("session"))
    res.setHeader("Vary", "Origin")
    res.setHeader("Content-Type", "text/custom")
    res.cookie("sid", "1")
    payload = "hello" * 100
    await res.send(payload)
    assert capture["status"] == 200
    assert capture["ct"] == "text/plain; charset=utf-8"
    assert capture["body"] == payload.encode()
    cookie_header = dict(capture["headers"])["set-cookie"]
    assert cookie_header == ["sid=1; Path=/"]
    headers = header_dict(events)
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"vary"] == b"Accept-Encoding"
    assert headers[b"content-type"] == b"text/custom"
    assert zlib.decompress(response_body(events), 16 + zlib.MAX_WBITS) == payload.encode()

    plain_app = app_factory()
    plain_app.use_compression(min_size=1)
    plain_req = make_request(headers={"Accept-Encoding": "gzip"})
    plain_res, plain_events = make_response(plain_app, plain_req)
    await plain_res.json({"blob": "x" * 200})
    plain_headers = header_dict(plain_events)
    assert plain_headers[b"content-encoding"] == b"gzip"
    assert plain_headers[b"vary"] == b"Accept-Encoding"

    show_app = app_factory()
    show_app._show_requests = True
    show_res, _ = make_response(show_app)
    await show_res.send("shown")
    assert show_res._sent_headers

    none_res = Response(None, app=app_factory())
    await none_res.send("x")
    assert none_res._sent is True

    async def failing_send(event):
        raise RuntimeError("closed")

    failing = Response(failing_send, app=app_factory())
    with pytest.raises(ValueError):
        await failing.send("x")


async def test_response_send_file_and_download_small(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    target = tmp_path / "hello.txt"
    target.write_bytes(b"hello world")
    patch_safe_path(monkeypatch, target)

    res, events = make_response(app)
    res.setHeader("Content-Disposition", "old")
    await res.sendFile(str(target))
    headers = header_dict(events)
    assert events[0]["status"] == 200
    assert headers[b"content-type"].startswith(b"text/plain")
    assert b"inline" in headers[b"content-disposition"]
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"content-length"] == b"11"
    assert response_body(events) == b"hello world"

    download_res, download_events = make_response(app)
    await download_res.download(str(target))
    download_headers = header_dict(download_events)
    assert download_headers[b"content-type"] == b"application/octet-stream"
    assert b"attachment" in download_headers[b"content-disposition"]
    assert b'filename="hello.txt"' in download_headers[b"content-disposition"]
    assert response_body(download_events) == b"hello world"

    unknown = tmp_path / "blob.zzz"
    unknown.write_bytes(b"unknown")
    patch_safe_path(monkeypatch, unknown)
    unknown_res, unknown_events = make_response(app)
    await unknown_res.sendFile(str(unknown))
    assert header_dict(unknown_events)[b"content-type"] == b"text/plain; charset=utf-8"

    app._show_requests = True
    multi_res, multi_events = make_response(app)
    multi_res._header_map = {b"set-cookie": [b"a=1", b"b=2"]}
    await multi_res.sendFile(str(target))
    multi_cookies = [value for key, value in multi_events[0]["headers"] if key == b"set-cookie"]
    assert multi_cookies == [b"a=1", b"b=2"]
    assert multi_res._sent_headers

    named_res, named_events = make_response(app)
    await named_res.download(str(target), filename="naïve.txt")
    disposition = header_dict(named_events)[b"content-disposition"].decode("latin-1")
    assert "attachment" in disposition
    assert "filename*=UTF-8''" in disposition


@pytest.mark.parametrize(
    "error,status,fragment",
    [
        ("same_dir", 403, b"caller's directory"),
        ("escape", 403, b"escapes"),
        ("not_found", 404, b"File not found"),
    ],
)
async def test_response_send_file_errors(app_factory, monkeypatch, error, status, fragment):
    app = app_factory()
    monkeypatch.setattr(
        cycles,
        "_resolve_safe_path",
        lambda path: (path, "", error),
    )
    res, events = make_response(app)
    await res.sendFile("missing.txt")
    assert events[0]["status"] == status
    assert fragment in response_body(events)

    download_res, download_events = make_response(app)
    await download_res.download("missing.txt")
    assert download_events[0]["status"] == status
    assert fragment in response_body(download_events)


async def test_response_send_file_streaming_sizes(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    mid = tmp_path / "mid.txt"
    data = b"a" * 70000
    mid.write_bytes(data)
    patch_safe_path(monkeypatch, mid)
    app._show_requests = True
    res, events = make_response(app)
    res._header_map = {b"set-cookie": [b"a=1", b"b=2"]}
    await res.sendFile(str(mid), chunk_size=8192)
    assert header_dict(events)[b"content-length"] == str(len(data)).encode()
    cookies = [value for key, value in events[0]["headers"] if key == b"set-cookie"]
    assert cookies == [b"a=1", b"b=2"]
    assert res._sent_headers
    chunks = [e.get("body", b"") for e in body_events(events)]
    assert len([chunk for chunk in chunks if chunk]) >= 2
    assert b"".join(chunks) == data

    small = tmp_path / "large.bin"
    small.write_bytes(b"z" * 10)
    patch_safe_path(monkeypatch, small)
    monkeypatch.setattr(cycles.os.path, "getsize", lambda _path: 5 * 1024 * 1024)
    large_res, large_events = make_response(app)
    await large_res.sendFile(str(small))
    assert response_body(large_events) == b"z" * 10

    monkeypatch.setattr(
        cycles.os.path,
        "getsize",
        lambda _path: (_ for _ in ()).throw(OSError("gone")),
    )
    unknown_res, unknown_events = make_response(app)
    await unknown_res.sendFile(str(small))
    assert b"content-length" not in header_dict(unknown_events)
    assert response_body(unknown_events) == b"z" * 10

    no_send = Response(None, app=app)
    await no_send._send_file_streaming(
        resolved_path=str(mid),
        content_type=b"text/plain",
        status=200,
        chunk_size=10,
        file_size=len(data),
        extra_headers=(),
    )


async def test_response_send_file_compressed_paths(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    app.use_compression(min_size=1)
    req = make_request(headers={"Accept-Encoding": "gzip"})

    small = tmp_path / "small.txt"
    small_data = b"abc" * 10
    small.write_bytes(small_data)
    patch_safe_path(monkeypatch, small)
    res, events = make_response(app, req)
    await res.sendFile(str(small))
    headers = header_dict(events)
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"vary"] == b"Accept-Encoding"
    assert zlib.decompress(response_body(events), 16 + zlib.MAX_WBITS) == small_data

    large = tmp_path / "large.txt"
    large_data = b"abcdef" * 20000
    large.write_bytes(large_data)
    patch_safe_path(monkeypatch, large)
    large_res, large_events = make_response(app, req)
    await large_res.sendFile(str(large), chunk_size=4096)
    large_headers = header_dict(large_events)
    assert large_headers[b"content-encoding"] == b"gzip"
    assert b"content-length" not in large_headers
    assert zlib.decompress(response_body(large_events), 16 + zlib.MAX_WBITS) == large_data


async def test_rsgi_send_file_and_download(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    target = tmp_path / "hello.txt"
    target.write_bytes(b"data")
    patch_safe_path(monkeypatch, target)

    res = RSGIResponse(app=app)
    await res.sendFile(str(target))
    headers = dict(res._hdr)
    assert res._sent is True
    assert res._body == b"data"
    assert headers["content-type"].startswith("text/plain")
    assert "inline" in headers["content-disposition"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-length"] == "4"

    download_res = RSGIResponse(app=app)
    await download_res.download(str(target), filename="naïve.txt")
    download_headers = dict(download_res._hdr)
    assert download_headers["content-type"] == "application/octet-stream"
    assert "attachment" in download_headers["content-disposition"]
    assert "filename*=UTF-8''" in download_headers["content-disposition"]

    unknown = tmp_path / "blob.zzz"
    unknown.write_bytes(b"unknown")
    patch_safe_path(monkeypatch, unknown)
    unknown_res = RSGIResponse(app=app)
    await unknown_res.sendFile(str(unknown))
    assert dict(unknown_res._hdr)["content-type"] == "text/plain; charset=utf-8"

    no_length = RSGIResponse(app=app)
    await no_length._send_file_streaming(
        resolved_path=str(target),
        content_type=b"text/plain",
        status=200,
        chunk_size=10,
        file_size=None,
        extra_headers=(),
    )
    assert "content-length" not in dict(no_length._hdr)

    for error, status, fragment in (
        ("same_dir", 403, b"caller's directory"),
        ("escape", 403, b"escapes"),
        ("not_found", 404, b"File not found"),
    ):
        monkeypatch.setattr(
            cycles,
            "_resolve_safe_path",
            lambda path, _error=error: (path, "", _error),
        )
        error_res = RSGIResponse(app=app)
        await error_res.sendFile("missing.txt")
        assert error_res._status == status
        assert fragment in error_res._body

        download_error = RSGIResponse(app=app)
        await download_error.download("missing.txt")
        assert download_error._status == status
        assert fragment in download_error._body


def test_rsgi_response_lazy_status_and_finish(app_factory):
    app = app_factory()
    req = make_request()
    res = RSGIResponse(req=req, app=app)
    assert req._res is res
    assert res.req is req
    send_file = res.sendFile
    assert callable(send_file)
    assert res.sendFile is send_file
    download = res.download
    assert res.download is download
    with pytest.raises(AttributeError):
        res.unknown_attribute
    with pytest.raises(TypeError):
        res.finish(1)
    assert res.finish(lambda: None) is res
    assert res.status(201) is res
    with pytest.raises(ValueError):
        res.status(-1)
    with pytest.raises(ValueError):
        res.status("201")
    res._sent = True
    assert res.status(500) is res
    assert res._status == 201


def test_rsgi_cookies_headers_and_clear(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    res.setHeader("X-Test", "one")
    res.setHeader("x-test", "two")
    assert [value for key, value in res._hdr if key == "x-test"] == ["two"]
    res.setHeader("Content-Type", "text/event-stream")
    assert res._streaming is True

    res.cookie(
        "theme",
        "dark",
        maxAge=30,
        expires="soon",
        domain="example.com",
        path="/app",
        httpOnly=True,
        secure=True,
        sameSite="none",
        partitioned=True,
        priority="medium",
    )
    value = dict(res._hdr)["set-cookie"]
    for token in (
        "theme=dark",
        "Max-Age=30",
        "Expires=soon",
        "Domain=example.com",
        "Path=/app",
        "HttpOnly",
        "Secure",
        "SameSite=None",
        "Partitioned",
        "Priority=Medium",
    ):
        assert token in value
    with pytest.raises(ValueError):
        res.cookie("bad", "1", priority="urgent")

    res.clearCookie(
        "theme",
        domain="example.com",
        httpOnly=True,
        secure=True,
        sameSite="lax",
        partitioned=True,
        priority="low",
        path="/app",
    )
    cleared = dict(res._hdr)["set-cookie"]
    assert "theme=" in cleared and "Priority=Low" in cleared
    with pytest.raises(ValueError):
        res.clearCookie("theme", priority="urgent")

    req = make_request(headers={"Cookie": "a=1; b=2"})
    multi = RSGIResponse(req=req, app=app)
    multi.clearAllCookies(domain="example.com", httpOnly=True, secure=True,
                          sameSite="none", partitioned=True, priority="medium")
    assert len([value for key, value in multi._hdr if key == "set-cookie"]) == 2
    with pytest.raises(ValueError):
        multi.clearAllCookies(priority="urgent")

    empty = RSGIResponse(app=app)
    assert empty.clearAllCookies() is empty
    empty._sent = True
    assert empty.clearAllCookies() is empty
    res._sent = True
    assert res.cookie("x", "1") is res
    assert res.clearCookie("x") is res


async def test_rsgi_redirect_and_jsonp(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    await res.redirect("/target", code=301)
    assert res._status == 301
    assert dict(res._hdr)["location"] == "/target"
    assert res._body == b""

    req = make_request(query=b"callback=cb")
    jsonp_res = RSGIResponse(req=req, app=app)
    await jsonp_res.jsonp({"a": 1}, status=202)
    assert jsonp_res._status == 202
    assert jsonp_res._body.startswith(b"/**/ typeof cb ")
    assert dict(jsonp_res._hdr)["content-type"] == "application/javascript; charset=utf-8"

    list_req = make_request(query=b"callback=a&callback=b")
    list_res = RSGIResponse(req=list_req, app=app)
    await list_res.jsonp({"x": 1})
    assert list_res._body.startswith(b"/**/ typeof a ")

    invalid_req = make_request(query=b"callback=bad-name")
    invalid_res = RSGIResponse(req=invalid_req, app=app)
    await invalid_res.jsonp({"x": 1})
    assert json.loads(invalid_res._body) == {"x": 1}

    no_req_res = RSGIResponse(app=app)
    await no_req_res.jsonp({"x": 2})
    assert json.loads(no_req_res._body) == {"x": 2}


async def test_rsgi_write_end_and_stream(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    assert await res.write(None) is True
    assert await res.write("a") is True
    assert await res.write(b"b") is True
    assert await res.write(1) is True
    assert res._chunks == [b"a", b"b", b"1"]
    await res.end()
    assert res._sent is True
    assert res._body == b"ab1"
    await res.end()
    assert await res.write("ignored") is False

    preset = RSGIResponse(app=app)
    preset.setHeader("Content-Type", "text/custom")
    preset.setHeader("X-Content-Type-Options", "custom")
    await preset.write("x")
    assert dict(preset._hdr)["content-type"] == "text/custom"
    assert dict(preset._hdr)["x-content-type-options"] == "custom"

    chunked = RSGIResponse(app=app)
    await chunked.end("tail")
    assert chunked._body == b"tail"
    assert dict(chunked._hdr)["content-type"] == "text/plain; charset=utf-8"

    numeric = RSGIResponse(app=app)
    await numeric.end(123)
    assert numeric._body == b"123"

    empty = RSGIResponse(app=app)
    await empty.end()
    assert empty._sent is True
    assert empty._body == b""

    manual = RSGIResponse(app=app)
    manual._chunks.append(b"manual")
    await manual.end()
    assert manual._body == b"manual"

    proto = FakeProto()
    streamed = RSGIResponse(app=app, proto=proto)
    assert await streamed.stream("one") is True
    assert proto.streams[0].chunks == [b"one"]
    assert dict(proto.streams[0].headers)["content-type"].startswith("text/event-stream")
    assert await streamed.write("two") is True
    await streamed.end("three")
    assert proto.streams[0].chunks == [b"one", b"two", b"three"]
    assert streamed._transport is None

    proto2 = FakeProto()
    streamed_close = RSGIResponse(app=app, proto=proto2)
    await streamed_close.stream("x")
    await streamed_close.end()
    assert streamed_close._transport is None

    custom_stream = RSGIResponse(app=app, proto=FakeProto())
    custom_stream.setHeader("Content-Type", "text/custom")
    await custom_stream.stream(5)
    assert dict(custom_stream._transport.headers)["content-type"] == "text/custom"

    buffered = RSGIResponse(app=app)
    await buffered.stream("buffered")
    await buffered.end()
    assert buffered._body == b"buffered"

    streamed._ended = True
    assert await streamed.stream("late") is False


async def test_rsgi_terminal_methods(app_factory, tmp_path):
    app = app_factory()
    res = RSGIResponse(app=app)
    await res.json({"x": 1})
    assert json.loads(res._body) == {"x": 1}
    await res.send("ignored")
    assert json.loads(res._body) == {"x": 1}

    status_res = RSGIResponse(app=app)
    await status_res.sendStatus(404)
    assert status_res._status == 404
    assert status_res._body == b"Not Found"
    invalid_status = RSGIResponse(app=app)
    with pytest.raises(ValueError):
        await invalid_status.sendStatus(99)

    unknown_status = RSGIResponse(app=app)
    await unknown_status.sendStatus(599)
    assert unknown_status._status == 599
    assert unknown_status._body == b""

    export_res = RSGIResponse(app=app)
    await export_res.export("text", "a.txt")
    assert export_res._body == b"text"
    assert dict(export_res._hdr)["content-type"].startswith("text/plain")
    assert "attachment" in dict(export_res._hdr)["content-disposition"]

    bytes_res = RSGIResponse(app=app)
    await bytes_res.export(b"bytes", "a.zzz", headers={"X-Extra": "1"})
    assert bytes_res._body == b"bytes"
    assert dict(bytes_res._hdr)["x-extra"] == "1"
    assert dict(bytes_res._hdr)["content-type"] == "application/octet-stream"

    bytearray_res = RSGIResponse(app=app)
    await bytearray_res.export(bytearray(b"z"), "archive.tar.gz")
    assert bytearray_res._body == b"z"

    bad_export = RSGIResponse(app=app)
    with pytest.raises(TypeError):
        await bad_export.export(123, "a.txt")

    no_template = RSGIResponse(app=app)
    with pytest.raises(RuntimeError):
        await no_template.render("missing.html")

    template_dir = tmp_path / "rsgi_templates"
    template_dir.mkdir()
    (template_dir / "hello.html").write_text("{{ name | shout }}", encoding="utf-8")
    template_app = app_factory()
    template_app.use_template(str(template_dir))

    def shout(value):
        return str(value).upper()

    render_res = RSGIResponse(app=template_app)
    await render_res.render("hello.html", {"name": "flameo"}, filters=[shout])
    assert render_res._body == b"FLAMEO"


async def test_rsgi_send_body_cache_and_headers(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    capture = {}
    res._cache_capture__ = capture
    res._finalize_session__ = lambda: (_ for _ in ()).throw(RuntimeError("session"))
    res.setHeader("Content-Type", "text/custom")
    res.setHeader("Content-Length", "999")
    await res.send("hello")
    assert capture["status"] == 200
    assert capture["ct"] == "text/plain; charset=utf-8"
    assert capture["body"] == b"hello"
    headers = dict(res._hdr)
    assert headers["content-type"] == "text/custom"
    assert headers["content-length"] == "5"
    assert res._into_bytes() == (200, res._hdr, b"hello")
    await res.send("again")
    assert res._body == b"hello"

    plain = RSGIResponse(app=app)
    await plain.json({"a": 1})
    assert dict(plain._hdr)["content-type"] == "application/json"
    assert dict(plain._hdr)["content-length"] == "7"

    for callable_finish in (True, False):
        finished = []
        finisher_res = RSGIResponse(app=app)
        if callable_finish:
            finisher_res.finish(lambda: finished.append("sync"))
        else:
            async def async_finisher():
                finished.append("async")
            finisher_res.finish(async_finisher)
        await finisher_res.send("x")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert finished
