import asyncio
import json
import zlib

import pytest

@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_protocol_response_types_cookies_and_status(protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()

    @app.get("/json")
    async def json_route(req, res):
        res.cookie("theme", "dark", httpOnly=True, sameSite="lax")
        res.setHeader("X-Answer", "yes")
        await res.status(201).json({"unicode": "café"})

    @app.get("/text")
    async def text_route(req, res):
        await res.send("plain text")

    json_response = await client(app, path="/json")
    assert json_response["status"] == 201
    assert json.loads(json_response["body"]) == {"unicode": "café"}
    assert json_response["headers"]["x-answer"] == ["yes"]
    set_cookie = json_response["headers"]["set-cookie"][0]
    assert "theme=dark" in set_cookie and "HttpOnly" in set_cookie

    text_response = await client(app, path="/text")
    assert text_response["status"] == 200
    assert text_response["body"] == b"plain text"
    assert text_response["headers"]["content-type"][0].startswith("text/plain")

    @app.get("/redirect")
    async def redirect(req, res):
        await res.redirect("/json", code=307)

    @app.get("/empty")
    async def empty(req, res):
        await res.send("")

    redirected = await client(app, path="/redirect")
    assert redirected["status"] == 307
    assert redirected["headers"]["location"] == ["/json"]
    assert redirected["body"] == b""
    empty_response = await client(app, path="/empty")
    assert empty_response["status"] == 200
    assert empty_response["body"] == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_cors_allowed_disallowed_and_preflight(protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    app.use_cors(origins=["https://allowed.example"], credentials=True,
                 methods=["GET", "POST"], headers=["Content-Type"], max_age=300)

    @app.get("/resource")
    async def resource(req, res):
        await res.send("ok")

    allowed = await client(app, path="/resource", headers={"Origin": "https://allowed.example"})
    assert allowed["headers"]["access-control-allow-origin"] == ["https://allowed.example"]
    assert allowed["headers"]["access-control-allow-credentials"] == ["true"]
    assert "origin" in allowed["headers"]["vary"][0].lower()

    denied = await client(app, path="/resource", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied["headers"]

    preflight = await client(
        app, "OPTIONS", "/resource",
        headers={"Origin": "https://allowed.example", "Access-Control-Request-Headers": "X-Token"},
    )
    assert preflight["status"] == 204
    assert preflight["headers"]["access-control-allow-methods"][0].lower() == "get, post"
    assert preflight["headers"]["access-control-allow-headers"][0].lower() == "content-type"
    assert preflight["headers"]["access-control-max-age"] == ["300"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_signed_session_roundtrip_and_clear(protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    app.use_session(secret_key="a-test-secret-long-enough", max_age=3600)

    @app.get("/increment")
    async def increment(req, res):
        req.session["count"] = req.session.get("count", 0) + 1
        await res.json({"count": req.session["count"]})

    @app.get("/clear")
    async def clear(req, res):
        req.session.clear()
        await res.send("cleared")

    first = await client(app, path="/increment")
    cookie = first["headers"]["set-cookie"][0].split(";", 1)[0]
    second = await client(app, path="/increment", headers={"Cookie": cookie})
    assert json.loads(second["body"]) == {"count": 2}
    cleared = await client(app, path="/clear", headers={"Cookie": cookie})
    assert "Expires=Thu, 01 Jan 1970 00:00:00 GMT" in cleared["headers"]["set-cookie"][0]


@pytest.mark.asyncio
async def test_static_files_etag_and_traversal(app_factory, asgi_client, tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "hello.txt").write_text("static content", encoding="utf-8")
    app = app_factory()
    app.use_static("/assets", str(root))

    fetched = await asgi_client(app, path="/assets/hello.txt")
    assert fetched["status"] == 200
    assert fetched["body"] == b"static content"
    assert fetched["headers"]["content-type"][0].startswith("text/plain")
    assert "etag" in fetched["headers"]

    cached = await asgi_client(
        app, path="/assets/hello.txt", headers={"If-None-Match": fetched["headers"]["etag"][0]}
    )
    assert cached["status"] == 304
    missing = await asgi_client(app, path="/assets/not-here.txt")
    assert missing["status"] == 404
    traversal = await asgi_client(app, path="/assets/../secret.txt")
    assert traversal["status"] == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_form_and_multipart_requests(protocol, app_factory, asgi_client, rsgi_client, tmp_path, monkeypatch):
    import flameo.parser as parser

    client = asgi_client if protocol == "asgi" else rsgi_client
    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(parser, "TEMP_DIR", spill_dir)
    app = app_factory()

    @app.post("/form")
    async def form(req, res):
        await res.json({"body": await req.body})

    @app.post("/upload")
    async def upload(req, res):
        fields = await req.body
        files = await req.files
        uploaded = files["file"]
        await res.json({"title": fields["title"], "filename": uploaded.filename,
                        "size": uploaded.size, "content_type": uploaded.content_type})

    form_response = await client(
        app, "POST", "/form", headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=b"title=hello+world&tag=a&tag=b",
    )
    assert json.loads(form_response["body"]) == {
        "body": {"title": "hello world", "tag": ["a", "b"]}
    }

    boundary = "upload-boundary"
    body = (
        b"--upload-boundary\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
        b"report\r\n--upload-boundary\r\n"
        b"Content-Disposition: form-data; name=\"file\"; filename=\"report.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\ncontents\r\n--upload-boundary--\r\n"
    )
    uploaded_response = await client(
        app, "POST", "/upload",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body_chunks=[body[start:start + 7] for start in range(0, len(body), 7)],
    )
    assert uploaded_response["status"] == 200
    assert json.loads(uploaded_response["body"]) == {
        "title": "report", "filename": "report.txt", "size": 8,
        "content_type": "text/plain",
    }
    assert list(spill_dir.glob("upload_*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_static_files_are_served_by_both_adapters(protocol, app_factory, asgi_client, rsgi_client, tmp_path):
    client = asgi_client if protocol == "asgi" else rsgi_client
    root = tmp_path / "static"
    root.mkdir()
    (root / "page.html").write_text("<h1>ok</h1>", encoding="utf-8")
    app = app_factory()
    app.use_static("/public", str(root))

    result = await client(app, path="/public/page.html")
    assert result["status"] == 200
    assert result["body"] == b"<h1>ok</h1>"
    assert result["headers"]["content-type"][0].startswith("text/html")
    assert "etag" in result["headers"]


@pytest.mark.asyncio
async def test_endpoint_cache_and_router_cache(app_factory, asgi_client):
    from flameo import Router

    app = app_factory()
    calls = {"app": 0, "router": 0}

    @app.cache(ttl=30)
    @app.get("/cached")
    async def cached(req, res):
        calls["app"] += 1
        await res.json({"call": calls["app"]})

    router = Router()

    @router.cache(ttl=30)
    @router.get("/cached")
    async def cached_router(req, res):
        calls["router"] += 1
        await res.json({"call": calls["router"]})

    app.use_router("/api", router)
    app_first = await asgi_client(app, path="/cached")
    app_second = await asgi_client(app, path="/cached")
    router_first = await asgi_client(app, path="/api/cached")
    router_second = await asgi_client(app, path="/api/cached")
    assert json.loads(app_first["body"]) == json.loads(app_second["body"]) == {"call": 1}
    assert app_first["headers"]["x-flameo-cache"] == ["MISS"]
    assert app_second["headers"]["x-flameo-cache"] == ["HIT"]
    assert json.loads(router_first["body"]) == json.loads(router_second["body"]) == {"call": 1}
    assert router_second["headers"]["x-flameo-cache"] == ["HIT"]


@pytest.mark.asyncio
async def test_validation_and_openapi_generation(app_factory, asgi_client):
    from pydantic import BaseModel

    class CreateItem(BaseModel):
        name: str
        quantity: int

    app = app_factory(title="Test inventory", version="2.1", docs_url="/docs")

    @app.post("/items", summary="Create an item", tags=["items"], query={"dry_run": bool})
    async def create_item(data: CreateItem, res):
        await res.json({"name": data.name, "quantity": data.quantity}, status=201)

    valid = await asgi_client(
        app, "POST", "/items", headers={"Content-Type": "application/json"},
        body=b'{"name":"tea","quantity":3}',
    )
    assert valid["status"] == 201
    assert json.loads(valid["body"]) == {"name": "tea", "quantity": 3}

    invalid = await asgi_client(
        app, "POST", "/items", headers={"Content-Type": "application/json"},
        body=b'{"name":"tea","quantity":"many"}',
    )
    assert invalid["status"] == 422
    assert json.loads(invalid["body"])["error"] == "ValidationError"

    spec_result = await asgi_client(app, path="/openapi.json")
    spec = json.loads(spec_result["body"])
    assert spec["openapi"] == "3.1.0"
    assert spec["info"] == {"title": "Test inventory", "version": "2.1"}
    operation = spec["paths"]["/items"]["post"]
    assert operation["summary"] == "Create an item"
    assert operation["parameters"][0]["name"] == "dry_run"
    assert "CreateItem" in spec["components"]["schemas"]


@pytest.mark.asyncio
async def test_gzip_negotiation_and_body_integrity(app_factory, asgi_client):
    app = app_factory()
    app.use_compression(min_size=1)

    @app.get("/compressed")
    async def compressed(req, res):
        await res.json({"message": "compression " * 40})

    response = await asgi_client(app, path="/compressed", headers={"Accept-Encoding": "gzip"})
    assert response["headers"]["content-encoding"] == ["gzip"]
    decoded = zlib.decompress(response["body"], 16 + zlib.MAX_WBITS)
    assert json.loads(decoded)["message"].startswith("compression")
    plain = await asgi_client(app, path="/compressed", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain["headers"]


@pytest.mark.asyncio
async def test_asgi_streaming_chunks_and_sse_headers(app_factory, asgi_client):
    app = app_factory()

    @app.get("/events")
    async def events(req, res):
        await res.stream("data: first\n\n")
        await res.stream("event: update\ndata: second\n\n")
        await res.end()

    response = await asgi_client(app, path="/events")
    assert response["status"] == 200
    assert response["body"] == b"data: first\n\nevent: update\ndata: second\n\n"
    assert response["headers"]["content-type"][0].startswith("text/event-stream")
    body_events = [e for e in response["events"] if e["type"] == "http.response.body"]
    assert len(body_events) == 3
    assert body_events[-1]["body"] == b""


@pytest.mark.asyncio
async def test_lifespan_startup_and_shutdown_hooks(app_factory):
    app = app_factory()
    events = []

    @app.startup
    async def startup():
        events.append("startup")

    @app.shutdown
    async def shutdown():
        events.append("shutdown")

    incoming = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent = []

    async def receive():
        return next(incoming)

    async def send(event):
        sent.append(event)

    await app.lifespan({"type": "lifespan"}, receive, send)
    assert events == ["startup", "shutdown"]
    assert [event["type"] for event in sent] == [
        "lifespan.startup.complete", "lifespan.shutdown.complete"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_cache_slot_is_injected_and_shared_with_app_state(protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()

    @app.get("/counter")
    async def counter(req, res, cache, state):
        value = await cache.incr("count") if await cache.exists("count") else 1
        if value == 1:
            await cache.set("count", "1")
        state.last_count = value
        await res.json({"cache_value": await cache.get("count"), "state": state.last_count})

    first = await client(app, path="/counter")
    second = await client(app, path="/counter")
    assert json.loads(first["body"]) == {"cache_value": "1", "state": 1}
    assert json.loads(second["body"]) == {"cache_value": "2", "state": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_template_rendering_with_custom_filter(protocol, app_factory, asgi_client, rsgi_client, tmp_path):
    client = asgi_client if protocol == "asgi" else rsgi_client
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "hello.html").write_text("<h1>{{ name | shout }}</h1>", encoding="utf-8")
    app = app_factory()

    def shout(value):
        return str(value).upper()

    app.use_template(str(template_dir), filters=[shout])

    @app.get("/hello")
    async def hello(req, res):
        await res.render("hello.html", {"name": "Flameo"})

    response = await client(app, path="/hello")
    assert response["status"] == 200
    assert response["body"] == b"<h1>FLAMEO</h1>"
    assert response["headers"]["content-type"][0].startswith("text/html")


@pytest.mark.asyncio
async def test_file_upload_can_be_moved_to_application_storage(app_factory, asgi_client, tmp_path, monkeypatch):
    import flameo.parser as parser

    monkeypatch_dir = tmp_path / "spills"
    destination = tmp_path / "saved"
    monkeypatch_dir.mkdir()
    destination.mkdir()
    monkeypatch.setattr(parser, "TEMP_DIR", monkeypatch_dir)
    app = app_factory()

    @app.post("/save")
    async def save(req, res):
        file = (await req.files)["file"]
        saved = await file.upload_to(str(destination), name="saved.txt")
        await res.json({"name": saved.name, "size": saved.stat().st_size})

    body = (
        b"--persist\r\nContent-Disposition: form-data; name=\"file\"; filename=\"upload.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\nhello\r\n--persist--\r\n"
    )
    response = await asgi_client(
        app, "POST", "/save", headers={"Content-Type": "multipart/form-data; boundary=persist"}, body=body,
    )
    assert json.loads(response["body"]) == {"name": "saved.txt", "size": 5}
    assert (destination / "saved.txt").read_bytes() == b"hello"
    assert list(monkeypatch_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_response_finish_callbacks_run_after_response(app_factory, asgi_client):
    app = app_factory()
    finished = asyncio.Event()

    @app.get("/finish")
    async def finish(req, res):
        async def cleanup():
            finished.set()
        res.finish(cleanup)
        await res.send("done")

    response = await asgi_client(app, path="/finish")
    assert response["body"] == b"done"
    await asyncio.wait_for(finished.wait(), timeout=0.2)
