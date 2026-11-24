import json

import pytest


@pytest.mark.asyncio
async def test_rsgi_app_and_router_responses(app_factory, rsgi_client):
    from flameo import Router

    app = app_factory()

    @app.get("/hello")
    async def hello(req, res):
        res.setHeader("X-Framework", "Flameo")
        await res.json({"message": "hello", "query": req.query.get("name")})

    router = Router()

    @router.get("/users/{user_id:int}")
    async def user(req, res):
        await res.json({"id": req.params["user_id"]})

    app.use_router("/api", router)
    response = await rsgi_client(app, path="/hello", query=b"name=RSGI")
    assert response["status"] == 200
    assert json.loads(response["body"]) == {"message": "hello", "query": "RSGI"}
    assert response["headers"]["x-framework"] == ["Flameo"]

    routed = await rsgi_client(app, path="/api/users/8")
    assert routed["status"] == 200
    assert json.loads(routed["body"]) == {"id": 8}


@pytest.mark.asyncio
async def test_rsgi_request_body_cookies_and_http_methods(app_factory, rsgi_client):
    app = app_factory()

    @app.post("/echo")
    async def echo(req, res):
        await res.json({"body": await req.body, "cookie": req.cookies.get("sid")})

    for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
        def make_handler(verb):
            async def handler(req, res):
                await res.send(verb, status=202)
            return handler
        handler = make_handler(method)
        app.route(method, f"/actual/{method.lower()}", handler)

    result = await rsgi_client(
        app, "POST", "/echo", headers={"Content-Type": "application/json", "Cookie": "sid=abc"},
        body=b'{"ok":true}',
    )
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"body": {"ok": True}, "cookie": "abc"}

    for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
        response = await rsgi_client(app, method, f"/actual/{method.lower()}")
        assert response["status"] == 202
        assert response["body"] == method.encode()


@pytest.mark.asyncio
async def test_rsgi_preserves_header_value_case(app_factory, rsgi_client):
    app = app_factory()

    @app.get("/header")
    async def header(req, res):
        res.setHeader("X-Case", "MiXeD-Value")
        await res.send("ok")

    response = await rsgi_client(app, path="/header")
    assert response["headers"]["x-case"] == ["MiXeD-Value"]


@pytest.mark.asyncio
async def test_rsgi_streaming_sse_uses_protocol_stream(app_factory, rsgi_client):
    app = app_factory()

    @app.get("/events")
    async def events(req, res):
        await res.stream("data: one\n\n")
        await res.stream("event: update\ndata: two\n\n")
        await res.end()

    response = await rsgi_client(app, path="/events")
    assert response["status"] is None
    assert response["stream"] is not None
    assert response["stream"].status == 200
    assert response["stream"].headers[0][0] == "content-type"
    assert response["stream"].headers[0][1].startswith("text/event-stream")
    assert response["stream"].chunks == [b"data: one\n\n", b"event: update\ndata: two\n\n"]


@pytest.mark.asyncio
async def test_rsgi_404_and_middleware_order(app_factory, rsgi_client):
    app = app_factory()
    calls = []

    async def first(req, res):
        calls.append("first")

    async def second(req, res):
        calls.append("second")

    app.use(first)
    app.use(second)
    response = await rsgi_client(app, path="/missing")
    assert response["status"] == 404
    assert b"Cannot GET /missing" in response["body"]
    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_rsgi_endpoint_exception_is_a_500_without_exception_details(app_factory, rsgi_client):
    app = app_factory()

    @app.get("/failure")
    async def failure(req, res):
        raise RuntimeError("private implementation detail")

    response = await rsgi_client(app, path="/failure")
    assert response["status"] == 500
    assert response["body"] == b"Internal Server Error"
    assert b"private implementation detail" not in response["body"]
