import json

import pytest


def _build_parity_app(app_factory):
    from flameo import Router

    app = app_factory()

    @app.get("/app/json")
    async def app_json(req, res):
        res.setHeader("X-Source", "app")
        await res.json({"source": "app"}, status=201)

    @app.post("/app/send")
    async def app_send(req, res):
        res.setHeader("X-Source", "app")
        await res.send("app-send", status=202)

    router = Router()

    @router.get("/json")
    async def router_json(req, res):
        res.setHeader("X-Source", "router")
        await res.json({"source": "router"}, status=203)

    @router.post("/send")
    async def router_send(req, res):
        res.setHeader("X-Source", "router")
        await res.send("router-send", status=202)

    app.use_router("/api", router)
    return app


@pytest.mark.asyncio
async def test_app_and_router_responses_match_across_asgi_and_rsgi(
    app_factory, asgi_client, rsgi_client,
):
    app = _build_parity_app(app_factory)
    cases = [
        ("GET", "/app/json", b"", 201, b'{"source":"app"}'),
        ("POST", "/app/send", b"payload", 202, b"app-send"),
        ("GET", "/api/json", b"", 203, b'{"source":"router"}'),
        ("POST", "/api/send", b"payload", 202, b"router-send"),
    ]

    for method, path, body, status, expected_body in cases:
        asgi = await asgi_client(app, method, path, body=body)
        rsgi = await rsgi_client(app, method, path, body=body)

        assert asgi["status"] == status
        assert rsgi["status"] == status
        assert asgi["body"] == expected_body
        assert rsgi["body"] == expected_body
        assert asgi["headers"]["content-type"] == rsgi["headers"]["content-type"]
        assert asgi["headers"]["x-source"] == rsgi["headers"]["x-source"]


@pytest.mark.asyncio
async def test_json_response_parity_between_endpoint_kinds(
    app_factory, asgi_client, rsgi_client,
):
    app = _build_parity_app(app_factory)

    asgi_app = await asgi_client(app, "GET", "/app/json")
    rsgi_app = await rsgi_client(app, "GET", "/app/json")
    asgi_router = await asgi_client(app, "GET", "/api/json")
    rsgi_router = await rsgi_client(app, "GET", "/api/json")

    assert json.loads(asgi_app["body"]) == json.loads(rsgi_app["body"])
    assert json.loads(asgi_router["body"]) == json.loads(rsgi_router["body"])
    assert asgi_app["headers"]["content-type"] == ["application/json"]
    assert rsgi_router["headers"]["content-type"] == ["application/json"]
