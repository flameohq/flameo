from __future__ import annotations

import asyncio

import pytest

import flameo.cycles as cycles
from flameo.cycles import (
    Request,
    Response,
    RSGIResponse,
    RouterMap,
    _normalize_headers,
    _parse_accept,
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
    receive=None,
):
    raw_pairs = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
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
    return Request(scope, receive or _empty_receive, 1024 * 1024)


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


def patch_safe_path(monkeypatch, path):
    monkeypatch.setattr(
        cycles,
        "_resolve_safe_path",
        lambda _p: (str(path), str(path.parent), None),
    )


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


class NameHeaders:
    def __init__(self, names, values, fail_get=False, fail_item=False):
        self._names = names
        self._values = values
        self._fail_get = fail_get
        self._fail_item = fail_item

    def __iter__(self):
        return iter(self._names)

    def get(self, name):
        if self._fail_get:
            raise RuntimeError("get unavailable")
        return self._values.get(name)

    def __getitem__(self, name):
        if self._fail_item:
            raise KeyError(name)
        return self._values[name]

    def items(self):
        return self._values.items()


def test_normalize_headers_non_iterable_and_none_first():
    assert _normalize_headers(123) == {}
    assert _normalize_headers([None]) == {}


def test_normalize_headers_name_only_fetch_fallbacks():
    out = _normalize_headers(NameHeaders(["x-a"], {"x-a": "1"}, fail_get=True))
    assert out == {"x-a": "1"}
    out = _normalize_headers(
        NameHeaders(
            ["x-b"], {"other": "0", "x-b": "2"}, fail_get=True, fail_item=True
        )
    )
    assert out == {"x-b": "2"}
    out = _normalize_headers(
        NameHeaders(["x-c"], {}, fail_get=True, fail_item=True)
    )
    assert out == {"x-c": "None"}


def test_parse_accept_bad_q_and_extra_params():
    assert _parse_accept("text/html;q=oops") == []
    parsed = _parse_accept("text/html;level=1;q=0.5, application/json")
    assert parsed[0][0] == "application/json"
    assert parsed[1] == ("text/html", 0.5)
    req = make_request(headers={"Accept": "text/html;q=oops"})
    assert req.accepts("html") is False


def test_router_compile_empty_segment_and_caster_failure():
    handler = object()
    router = RouterMap()
    router.add_route("GET", "/x//{vid}", handler)
    found, params, _ = router.match("GET", "/x//123")
    assert found is handler
    assert params["vid"] == "123"

    router.add_route("GET", "/n/{v:int}", handler)
    assert router.match("GET", "/n/abc")[0] is None
    found, params, _ = router.match("GET", "/n/42")
    assert found is handler
    assert params["v"] == 42


def test_router_caster_failure_treated_as_no_match():
    def boom(value):
        raise ValueError("bad value")

    router = RouterMap()
    router.converters.register("boom", r"[^/]+", boom)
    handler = object()
    router.add_route("GET", "/c/{x:boom}", handler)
    assert router.match("GET", "/c/anything")[0] is None


def test_request_compute_unknown_and_url_parts_lazy_none(app_factory):
    app = app_factory()
    req = make_request()
    assert req._lazy is None
    protocol, hostname, ip, host, port = req._url_parts(
        "http", ("example.com", 80), ("1.2.3.4", 5)
    )
    assert (protocol, hostname, ip) == ("http", "example.com", "1.2.3.4")
    assert (host, port) == ("example.com", 80)
    with pytest.raises(AttributeError):
        make_request()._compute("bogus-prop")


async def test_request_load_early_return_and_derived_props(app_factory):
    app = app_factory()
    req = request_with_body(b"{}", content_type="application/json")
    await req._load()
    assert req._body == b"{}"

    assert req.ips == []
    assert make_request(server=("", 0)).subdomains == []

    res, _ = make_response(app, req)
    assert req._res is res
    assert req.fresh is False
    assert req.stale is True


def test_response_init_req_without_slots(app_factory):
    async def send(event):
        pass

    app = app_factory()
    marker = object()
    res = Response(send, app=app, req=marker)
    assert res._req is marker


def test_rsgi_init_req_without_slots(app_factory):
    app = app_factory()
    marker = object()
    res = RSGIResponse(req=marker, app=app)
    assert res._req is marker


def test_asgi_clear_all_cookies_branches(app_factory):
    app = app_factory()

    async def send(event):
        pass

    req = make_request(headers={"Cookie": "a=1; b=2"})
    res = Response(send, app=app, req=req)
    assert res.clearAllCookies(
        domain="example.com",
        httpOnly=True,
        secure=True,
        sameSite="lax",
        partitioned=True,
        priority="high",
        path="/x",
    ) is res
    chunks = res._header_map[b"set-cookie"]
    assert len(chunks) == 2
    assert b"Domain=example.com" in chunks[0]
    assert b"HttpOnly" in chunks[0]
    assert b"Secure" in chunks[0]
    assert b"SameSite=Lax" in chunks[0]
    assert b"Partitioned" in chunks[0]
    assert b"Priority=High" in chunks[0]
    assert b"Path=/x" in chunks[0]

    res2 = Response(send, app=app, req=make_request(headers={"Cookie": "a=1"}))
    res2.clearAllCookies(sameSite="none", priority="low")
    assert b"SameSite=None" in res2._header_map[b"set-cookie"][0]

    res3 = Response(send, app=app, req=make_request(headers={"Cookie": "a=1"}))
    res3.clearAllCookies(priority="medium")
    assert b"Priority=Medium" in res3._header_map[b"set-cookie"][0]

    with pytest.raises(ValueError):
        res3.clearAllCookies(priority="ultra")

    no_cookies = Response(send, app=app, req=make_request())
    assert no_cookies.clearAllCookies() is no_cookies

    plain = Response(send, app=app, req=make_request(headers={"Cookie": "a=1"}))
    assert plain.clearAllCookies() is plain
    assert len(plain._header_map[b"set-cookie"]) == 1

    no_req = Response(send, app=app, req=None)
    assert no_req.clearAllCookies() is no_req

    sent = Response(send, app=app, req=req)
    sent._sent = True
    assert sent.clearAllCookies() is sent


def test_asgi_clear_all_cookies_existing_header_map(app_factory):
    async def send(event):
        pass

    app = app_factory()
    req = make_request(headers={"Cookie": "a=1"})
    res = Response(send, app=app, req=req)
    res.cookie("x", "y")
    assert res.clearAllCookies() is res
    assert len(res._header_map[b"set-cookie"]) == 1


def test_rsgi_clear_all_cookies_branches(app_factory):
    app = app_factory()
    req = make_request(headers={"Cookie": "a=1; b=2"})
    res = RSGIResponse(req=req, app=app)
    assert res.clearAllCookies(
        domain="example.com",
        httpOnly=True,
        secure=True,
        sameSite="none",
        partitioned=True,
        priority="high",
    ) is res
    assert len(res._hdr) == 2
    assert "SameSite=None" in res._hdr[0][1]
    with pytest.raises(ValueError):
        res.clearAllCookies(priority="ultra")
    plain = RSGIResponse(req=make_request(headers={"Cookie": "a=1"}), app=app)
    assert plain.clearAllCookies() is plain
    assert len(plain._hdr) == 1
    assert RSGIResponse(app=app).clearAllCookies() is not None
    sent = RSGIResponse(req=req, app=app)
    sent._sent = True
    assert sent.clearAllCookies() is sent


async def test_asgi_redirect_and_jsonp_when_sent(app_factory):
    app = app_factory()
    res, events = make_response(app, make_request())
    res._sent = True
    assert await res.redirect("/x") is res
    await res.jsonp({"a": 1})
    assert events == []


class ExplodingQuery:
    @property
    def query(self):
        raise RuntimeError("boom")


class NoGetMap:
    def __init__(self, value):
        self._value = value

    def __getitem__(self, key):
        assert key == "callback"
        return self._value


def _stub_req(query):
    from types import SimpleNamespace

    return SimpleNamespace(query=query)


async def test_asgi_jsonp_query_shapes(app_factory):
    app = app_factory()

    res, events = make_response(app, ExplodingQuery())
    await res.jsonp({"a": 1})
    assert header_dict(events)[b"content-type"] == b"application/json"
    assert response_body(events) == b'{"a":1}'

    res, events = make_response(app, _stub_req(NoGetMap("cb")))
    await res.jsonp({"a": 1})
    body = response_body(events)
    assert b"typeof cb" in body
    assert header_dict(events)[b"content-type"] == b"application/javascript; charset=utf-8"

    res, events = make_response(app, _stub_req(NoGetMap(["cb"])))
    await res.jsonp({"a": 1})
    assert b"typeof cb" in response_body(events)

    res, events = make_response(app, _stub_req(NoGetMap([])))
    await res.jsonp({"a": 1})
    assert header_dict(events)[b"content-type"] == b"application/json"

    res, events = make_response(app, _stub_req(NoGetMap("alert(1)")))
    await res.jsonp({"a": 1})
    assert header_dict(events)[b"content-type"] == b"application/json"

    res, events = make_response(app, None)
    await res.jsonp({"a": 1})
    assert header_dict(events)[b"content-type"] == b"application/json"


async def test_rsgi_redirect_jsonp_sent_and_query_error(app_factory):
    app = app_factory()
    sent = RSGIResponse(app=app)
    sent._sent = True
    assert await sent.redirect("/x") is sent
    await sent.jsonp({"a": 1})
    assert sent._body == b""

    res = RSGIResponse(req=ExplodingQuery(), app=app)
    await res.jsonp({"a": 1})
    assert res._body == b'{"a":1}'
    assert res._sent is True


async def test_asgi_write_streaming_first_flush_no_header_map(app_factory):
    app = app_factory()
    res, events = make_response(app, make_request())
    res._streaming = True
    res._streamed = False
    res._wrote_head = True
    res._header_map = None
    assert await res.write(b"chunk1") is True
    start = next(e for e in events if e["type"] == "http.response.start")
    assert start["headers"] == []
    assert response_body(events) == b"chunk1"


async def test_asgi_write_streaming_no_send_buffers(app_factory):
    app = app_factory()
    res = Response(None, app=app, req=make_request())
    res._streaming = True
    assert await res.write(b"x") is True
    assert res._chunks == [b"x"]
    assert res._streamed is True


async def test_asgi_end_streamed_variants(app_factory):
    app = app_factory()

    res, events = make_response(app, make_request())
    await res.stream("a")
    await res.end("tail")
    bodies = [e.get("body", b"") for e in body_events(events)]
    assert b"".join(bodies) == b"atail"
    assert bodies[-1] == b""
    assert res._sent is True

    nosend = Response(None, app=app, req=make_request())
    nosend._streamed = True
    await nosend.end(b"tail")
    assert nosend._sent is True
    assert nosend._chunks == []


async def test_asgi_end_buffered_head_install(app_factory):
    app = app_factory()
    res, events = make_response(app, make_request())
    await res.end("hi")
    assert response_body(events) == b"hi"
    headers = header_dict(events)
    assert headers[b"content-type"] == b"text/plain; charset=utf-8"
    assert headers[b"x-content-type-options"] == b"nosniff"

    res, events = make_response(app, make_request())
    res.setHeader("Content-Type", "text/plain")
    await res.end(b"x")
    assert header_dict(events)[b"x-content-type-options"] == b"nosniff"

    res, events = make_response(app, make_request())
    res.setHeader("Content-Type", "text/plain")
    res.setHeader("X-Content-Type-Options", "nosniff")
    await res.end(b"x")
    assert response_body(events) == b"x"


async def test_asgi_stream_int_chunk_and_list_headers(app_factory):
    app = app_factory()
    res, events = make_response(app, make_request())
    await res.stream(123)
    assert b"123" in response_body(events)

    res, events = make_response(app, make_request())
    await res.stream(b"by")
    assert response_body(events) == b"by"

    res, events = make_response(app, make_request())
    res.cookie("a", "b")
    assert await res.stream("x") is True
    assert b"x" in response_body(events)


async def test_asgi_stream_header_map_none(app_factory, monkeypatch):
    app = app_factory()
    res, events = make_response(app, make_request())
    monkeypatch.setattr(Response, "setHeader", lambda self, k, v: self)
    assert res._header_map is None
    assert await res.stream("hello") is True
    start = next(e for e in events if e["type"] == "http.response.start")
    assert start["headers"] == []
    assert response_body(events) == b"hello"


async def test_asgi_terminal_sent_early_return(app_factory):
    app = app_factory()

    async def send(event):
        pass

    res = Response(send, app=app, req=make_request())
    res._sent = True
    assert await res.sendStatus(404) is res
    await res.export(b"x", "f.bin")
    await res.render("t.html")


async def test_asgi_export_with_preset_headers(app_factory):
    app = app_factory()
    res, events = make_response(app, make_request())
    res.setHeader("X-Custom", "1")
    await res.export(b"data", "f.txt", headers={"X-Extra": "2"})
    headers = header_dict(events)
    assert headers[b"x-custom"] == b"1"
    assert headers[b"x-extra"] == b"2"
    assert b"attachment" in headers[b"content-disposition"]
    assert response_body(events) == b"data"


async def test_asgi_send_body_cache_capture_empty_and_finalize_error(app_factory):
    app = app_factory()
    res, events = make_response(app, make_request())
    res._cache_capture__ = {}
    await res.json({"a": 1})
    assert res._cache_capture__["headers"] == []
    assert res._cache_capture__["body"] == b'{"a":1}'

    def bad():
        raise RuntimeError("nope")

    res2, events2 = make_response(app, make_request())
    res2._finalize_session__ = bad
    await res2.json({"ok": True})
    assert response_body(events2) == b'{"ok":true}'

    called = []
    res3, events3 = make_response(app, make_request())
    res3._finalize_session__ = lambda: called.append(1)
    await res3.json({"ok": True})
    assert called == [1]
    assert response_body(events3) == b'{"ok":true}'


async def test_asgi_send_body_send_error_wraps_value_error(app_factory):
    app = app_factory()

    async def boom(event):
        raise RuntimeError("wire down")

    res = Response(boom, app=app, req=make_request())
    with pytest.raises(ValueError):
        await res.json({"a": 1})


async def test_asgi_send_body_compressed_vary_replace(app_factory):
    app = app_factory()
    app.use_compression(min_size=1)
    req = make_request(headers={"Accept-Encoding": "gzip"})
    res, events = make_response(app, req)
    res.setHeader("Vary", "old")
    await res.json({"a": 1})
    headers = header_dict(events)
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"vary"] == b"Accept-Encoding"
    assert [v for k, v in events[0]["headers"] if k == b"vary"] == [
        b"Accept-Encoding"
    ]


async def test_asgi_send_body_compressed_no_preset_vary(app_factory):
    app = app_factory()
    app.use_compression(min_size=1)
    req = make_request(headers={"Accept-Encoding": "gzip"})
    res, events = make_response(app, req)
    res.setHeader("X-Custom", "1")
    await res.json({"a": 1})
    headers = header_dict(events)
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"vary"] == b"Accept-Encoding"


async def test_asgi_send_file_small_vary_replace(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    app.use_compression(min_size=1)
    req = make_request(headers={"Accept-Encoding": "gzip"})
    target = tmp_path / "tiny.txt"
    target.write_bytes(b"hello world")
    patch_safe_path(monkeypatch, target)
    res, events = make_response(app, req)
    res.setHeader("Vary", "old")
    await res.sendFile(str(target))
    headers = header_dict(events)
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"vary"] == b"Accept-Encoding"


async def test_asgi_send_file_small_send_error(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    target = tmp_path / "tiny.txt"
    target.write_bytes(b"hello world")
    patch_safe_path(monkeypatch, target)

    async def flaky(event):
        raise RuntimeError("down")

    res = Response(flaky, app=app, req=make_request())
    with pytest.raises(RuntimeError):
        await res.sendFile(str(target))


async def test_asgi_send_file_streaming_non_list_headers(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    mid = tmp_path / "mid.txt"
    data = b"b" * 70000
    mid.write_bytes(data)
    patch_safe_path(monkeypatch, mid)
    res, events = make_response(app, make_request())
    res.setHeader("X-A", "b")
    await res.sendFile(str(mid), chunk_size=8192)
    headers = header_dict(events)
    assert headers[b"x-a"] == b"b"
    assert headers[b"content-length"] == str(len(data)).encode()
    assert response_body(events) == data


async def test_asgi_send_file_streaming_vary_replace(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    app.use_compression(min_size=1)
    req = make_request(headers={"Accept-Encoding": "gzip"})
    large = tmp_path / "large.txt"
    data = b"abcdef" * 25000
    large.write_bytes(data)
    patch_safe_path(monkeypatch, large)
    res, events = make_response(app, req)
    res.setHeader("Vary", "stale")
    await res.sendFile(str(large), chunk_size=4096)
    headers = header_dict(events)
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"vary"] == b"Accept-Encoding"
    assert b"content-length" not in headers


async def test_asgi_send_file_streaming_codec_header_empty(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    mid = tmp_path / "mid.txt"
    data = b"c" * 70000
    mid.write_bytes(data)
    patch_safe_path(monkeypatch, mid)
    monkeypatch.setattr(
        app,
        "stream_compress_factory",
        lambda req, ct, fs: ("x-enc", lambda: (lambda c: c, lambda: b"", b"")),
    )
    res, events = make_response(app, make_request())
    await res.sendFile(str(mid), chunk_size=8192)
    headers = header_dict(events)
    assert headers[b"content-encoding"] == b"x-enc"
    assert b"content-length" not in headers
    assert response_body(events) == data


async def test_asgi_send_file_streaming_empty_wire_and_trailer(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    mid = tmp_path / "mid.txt"
    data = b"d" * 70000
    mid.write_bytes(data)
    patch_safe_path(monkeypatch, mid)
    state = {"n": 0}

    def comp(chunk):
        state["n"] += 1
        return b"" if state["n"] == 1 else chunk

    monkeypatch.setattr(
        app,
        "stream_compress_factory",
        lambda req, ct, fs: ("x-enc", lambda: (comp, lambda: b"TRAIL", b"")),
    )
    res, events = make_response(app, make_request())
    await res.sendFile(str(mid), chunk_size=8192)
    assert response_body(events) == data[8192:] + b"TRAIL"


async def test_asgi_send_file_streaming_send_error(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    mid = tmp_path / "mid.txt"
    mid.write_bytes(b"e" * 70000)
    patch_safe_path(monkeypatch, mid)
    events = []
    state = {"n": 0}

    async def flaky(event):
        state["n"] += 1
        events.append(event)
        if state["n"] > 1:
            raise RuntimeError("down")

    res = Response(flaky, app=app, req=make_request())
    with pytest.raises(RuntimeError):
        await res.sendFile(str(mid), chunk_size=8192)


async def test_rsgi_download_default_filename(app_factory, tmp_path, monkeypatch):
    app = app_factory()
    target = tmp_path / "hello.txt"
    target.write_bytes(b"data")
    patch_safe_path(monkeypatch, target)

    res = RSGIResponse(app=app)
    await res.download(str(target))
    headers = dict(res._hdr)
    assert 'filename="hello.txt"' in headers["content-disposition"]

    monkeypatch.setattr(cycles, "_resolve_safe_path", lambda _p: ("", "", None))
    monkeypatch.setattr(cycles, "_read_whole_file", lambda _p: b"data")
    res2 = RSGIResponse(app=app)
    await res2.download("anything")
    assert 'filename="download"' in dict(res2._hdr)["content-disposition"]


async def test_rsgi_write_streaming_no_transport(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    res._streaming = True
    assert await res.write(b"x") is True
    assert res._chunks == [b"x"]


async def test_rsgi_end_streaming_with_transport_bytes_chunk(app_factory):
    app = app_factory()
    proto = FakeProto()
    res = RSGIResponse(app=app, proto=proto)
    res._streaming = True
    await res.end(b"tail")
    assert proto.streams[0].chunks == [b"tail"]
    assert res._transport is None


async def test_rsgi_end_streaming_fallback_buffer(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    res._streaming = True
    await res.end(b"x")
    assert res._chunks == [b"x"]
    assert res._streaming is False
    assert res._wrote_head is True
    assert res._body == b"x"


async def test_rsgi_end_streaming_fallback_head_branches(app_factory):
    app = app_factory()

    res = RSGIResponse(app=app)
    await res.write(b"a")
    res._streaming = True
    await res.end(b"b")
    assert res._body == b"ab"

    res = RSGIResponse(app=app)
    res.setHeader("Content-Type", "text/plain")
    res._streaming = True
    await res.end(b"x")
    assert res._body == b"x"

    res = RSGIResponse(app=app)
    res.setHeader("Content-Type", "text/plain")
    res.setHeader("X-Content-Type-Options", "nosniff")
    res._streaming = True
    await res.end(b"x")
    assert res._body == b"x"


async def test_rsgi_end_buffered_head_branches(app_factory):
    app = app_factory()

    res = RSGIResponse(app=app)
    await res.write(b"a")
    await res.end(b"b")
    assert res._body == b"ab"

    res = RSGIResponse(app=app)
    res.setHeader("Content-Type", "text/plain")
    await res.end(b"x")
    assert res._body == b"x"

    res = RSGIResponse(app=app)
    res.setHeader("Content-Type", "text/plain")
    res.setHeader("X-Content-Type-Options", "nosniff")
    await res.end(b"x")
    assert res._body == b"x"


async def test_rsgi_end_streamed_close_without_chunk(app_factory):
    app = app_factory()
    proto = FakeProto()
    res = RSGIResponse(app=app, proto=proto)
    assert await res.stream("x") is True
    assert proto.streams[0].chunks == [b"x"]
    await res.end()
    assert res._transport is None


async def test_rsgi_stream_bytes_and_int_buffered(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    assert await res.stream(b"x") is True
    assert await res.stream(7) is True
    assert res._chunks == [b"x", b"7"]


async def test_rsgi_terminal_sent_early_return(app_factory):
    app = app_factory()
    res = RSGIResponse(app=app)
    res._sent = True
    assert await res.sendStatus(404) is res
    await res.json({"a": 1})
    await res.export(b"x", "f.bin")
    await res.render("t.html")
    assert res._body == b""
    await res._send_body(b"x", b"text/plain", 200)
    assert res._body == b""
