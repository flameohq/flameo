import json

import pytest


@pytest.mark.asyncio
async def test_app_routes_methods_and_typed_parameters(app_factory, asgi_client):
    app = app_factory()
    methods = {
        "GET": app.get,
        "POST": app.post,
        "PUT": app.put,
        "PATCH": app.patch,
        "DELETE": app.delete,
        "OPTIONS": app.options,
        "HEAD": app.head,
    }
    for method, register in methods.items():
        def make_handler(verb):
            async def handler(req, res):
                await res.json({"method": verb}, status=201)
            return handler
        handler = make_handler(method)
        register(f"/methods/{method.lower()}", handler)

    @app.get("/items/{item_id:int}/parts/{part_id:int}")
    async def get_item(req, res):
        await res.json({"item": req.params["item_id"], "part": req.params["part_id"]})

    for method in methods:
        result = await asgi_client(app, method, f"/methods/{method.lower()}")
        assert result["status"] == 201
        assert json.loads(result["body"]) == {"method": method}
        assert result["headers"]["content-type"][0].startswith("application/json")

    typed = await asgi_client(app, path="/items/12/parts/4")
    assert typed["status"] == 200
    assert json.loads(typed["body"]) == {"item": 12, "part": 4}
    assert (await asgi_client(app, path="/items/no/parts/4"))["status"] == 404


@pytest.mark.asyncio
async def test_request_metadata_json_and_not_found(app_factory, asgi_client):
    app = app_factory()

    @app.post("/echo")
    async def echo(req, res):
        await res.json({
            "payload": await req.body,
            "query": req.query,
            "header": req.headers.get("x-token"),
            "cookie": req.cookies.get("session"),
            "url": req.originalUrl,
        })

    result = await asgi_client(
        app, "POST", "/echo", query=b"q=hello+world&tag=a&tag=b",
        headers={"Content-Type": "application/json", "X-Token": "abc", "Cookie": "session=signed"},
        body=b'{"name":"Flameo"}',
    )
    assert result["status"] == 200
    assert json.loads(result["body"]) == {
        "payload": {"name": "Flameo"},
        "query": {"q": "hello world", "tag": ["a", "b"]},
        "header": "abc",
        "cookie": "signed",
        "url": "/echo?q=hello+world&tag=a&tag=b",
    }

    missing = await asgi_client(app, "DELETE", "/missing")
    assert missing["status"] == 404
    assert b"Cannot DELETE /missing" in missing["body"]


@pytest.mark.asyncio
async def test_router_prefix_middleware_and_shared_state(app_factory, asgi_client):
    from flameo import Router

    app = app_factory()
    app.state.events = []

    async def app_middleware(req, res, state):
        state.events.append("app")

    async def router_middleware(req, res, state):
        state.events.append("router")

    app.use(app_middleware)
    router = Router(title="inventory")
    router.use(router_middleware)

    @router.get("/products/{product_id:int}")
    async def product(req, res, app):
        await res.json({"id": req.params["product_id"], "base": req.baseUrl,
                        "events": app.state.events})

    app.use_router("/api/v1", router)
    result = await asgi_client(app, path="/api/v1/products/27")
    assert result["status"] == 200
    assert json.loads(result["body"]) == {
        "id": 27, "base": "/api/v1", "events": ["app", "router"]
    }


@pytest.mark.asyncio
async def test_router_mount_validation_and_custom_not_found(app_factory, asgi_client):
    from flameo import Router

    app = app_factory()
    router = Router()

    @router.get("/")
    async def home(req, res):
        await res.send("router-root")

    app.use_router("/docs/", router)
    assert (await asgi_client(app, path="/docs"))["body"] == b"router-root"
    assert (await asgi_client(app, path="/docs/"))["body"] == b"router-root"
    with pytest.raises(ValueError, match="already mounted"):
        app.use_router("/docs", router)

    @app.not_found
    async def custom_404(req, res):
        await res.json({"path": req.path}, status=404)

    missing = await asgi_client(app, path="/other")
    assert missing["status"] == 404
    assert json.loads(missing["body"]) == {"path": "/other"}


@pytest.mark.asyncio
async def test_asgi_preserves_case_sensitive_header_values(app_factory, asgi_client):
    app = app_factory()

    @app.get("/header")
    async def header(req, res):
        res.setHeader("X-Case", "MiXeD-Value")
        await res.send("ok")

    response = await asgi_client(app, path="/header")
    assert response["headers"]["x-case"] == ["MiXeD-Value"]


@pytest.mark.asyncio
async def test_concurrent_requests_are_isolated(app_factory, asgi_client):
    import asyncio

    app = app_factory()

    @app.get("/value/:value")
    async def value(req, res):
        await asyncio.sleep(0)
        await res.json({"value": req.params["value"]})

    responses = await asyncio.gather(*(
        asgi_client(app, path=f"/value/{value}") for value in range(12)
    ))
    assert [json.loads(response["body"]) for response in responses] == [
        {"value": str(value)} for value in range(12)
    ]


@pytest.mark.asyncio
async def test_application_middleware_short_circuits_and_route_middleware_runs(app_factory, asgi_client):
    app = app_factory()
    calls = []

    async def auth(req, res):
        if req.headers.get("x-auth") != "yes":
            await res.send("unauthorized", status=401)

    async def route_middleware(req, res):
        calls.append("middleware")

    app.use(auth)

    @app.get("/private", middlewares=[route_middleware])
    async def private(req, res):
        calls.append("handler")
        await res.send("private")

    rejected = await asgi_client(app, path="/private")
    assert rejected["status"] == 401
    assert calls == []
    allowed = await asgi_client(app, path="/private", headers={"X-Auth": "yes"})
    assert allowed["status"] == 200
    assert calls == ["middleware", "handler"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_source", ["endpoint", "middleware"])
async def test_asgi_unhandled_exception_returns_safe_500(app_factory, asgi_client, failure_source):
    app = app_factory()

    if failure_source == "middleware":
        async def failure(req, res):
            raise RuntimeError("private implementation detail")
        app.use(failure)
    else:
        @app.get("/failure")
        async def failure(req, res):
            raise RuntimeError("private implementation detail")

    response = await asgi_client(app, path="/failure")
    assert response["status"] == 500
    assert response["body"] == b"Internal Server Error"
    assert b"private implementation detail" not in response["body"]


@pytest.mark.asyncio
async def test_asgi_exception_after_stream_start_closes_without_second_start(app_factory, asgi_client):
    app = app_factory()

    @app.get("/stream-failure")
    async def stream_failure(req, res):
        await res.stream("data: partial\n\n")
        raise RuntimeError("stream failed")

    response = await asgi_client(app, path="/stream-failure")
    starts = [event for event in response["events"] if event["type"] == "http.response.start"]
    bodies = [event for event in response["events"] if event["type"] == "http.response.body"]
    assert len(starts) == 1
    assert starts[0]["status"] == 200
    assert bodies[-1]["body"] == b""
