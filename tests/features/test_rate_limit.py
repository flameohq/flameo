import json

import pytest
from pydantic import BaseModel


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_app_rate_limit_short_circuits_route_middleware_and_handler(
        protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    calls = []

    async def global_middleware(req, res):
        calls.append("global")

    async def route_middleware(req, res):
        calls.append("route")

    app.use(global_middleware)

    @app.limit(2, 60)
    @app.get("/limited", middlewares=[route_middleware])
    async def limited(req, res):
        calls.append("handler")
        await res.json({"ok": True})

    first = await client(app, path="/limited")
    second = await client(app, path="/limited")
    rejected = await client(app, path="/limited")

    assert first["status"] == second["status"] == 200
    assert rejected["status"] == 429
    assert json.loads(rejected["body"]) == {"error": "Too Many Requests"}
    assert int(rejected["headers"]["retry-after"][0]) >= 1
    assert calls == ["global", "route", "handler"] * 2 + ["global"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_rate_limit_key_function_separates_request_identities(
        protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()

    @app.limit(1, 60, key_func=lambda req: req.headers.get("x-user"))
    @app.get("/by-user")
    async def by_user(req, res):
        await res.send("ok")

    first_a = await client(app, path="/by-user", headers={"x-user": "a"})
    first_b = await client(app, path="/by-user", headers={"x-user": "b"})
    second_a = await client(app, path="/by-user", headers={"x-user": "a"})

    assert first_a["status"] == first_b["status"] == 200
    assert second_a["status"] == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_router_limit_uses_mounted_endpoint_path(
        protocol, app_factory, asgi_client, rsgi_client):
    from flameo import Router

    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    router = Router()

    @router.limit(1, 60)
    @router.get("/users/:user_id")
    async def user(req, res):
        await res.json({"id": req.params["user_id"]})

    app.use_router("/api", router)
    first = await client(app, path="/api/users/1")
    second = await client(app, path="/api/users/2")

    assert first["status"] == 200
    assert second["status"] == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_rate_limit_counts_cached_requests_in_both_decorator_orders(
        protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    calls = []

    @app.limit(1, 60)
    @app.cache(ttl=60)
    @app.get("/cache-first")
    async def cache_first(req, res):
        calls.append("cache-first")
        await res.send("ok")

    @app.cache(ttl=60)
    @app.get("/limit-first")
    @app.limit(1, 60)
    async def limit_first(req, res):
        calls.append("limit-first")
        await res.send("ok")

    for path in ("/cache-first", "/limit-first"):
        assert (await client(app, path=path))["status"] == 200
        assert (await client(app, path=path))["status"] == 429
    assert calls == ["cache-first", "limit-first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_rate_limit_window_expires(protocol, app_factory, asgi_client, rsgi_client,
                                         monkeypatch):
    from flameo import endpoint_cache

    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    now = [100.0]
    monkeypatch.setattr(endpoint_cache.time, "monotonic", lambda: now[0])

    @app.limit(1, 5)
    @app.get("/window")
    async def window(req, res):
        await res.send("ok")

    assert (await client(app, path="/window"))["status"] == 200
    assert (await client(app, path="/window"))["status"] == 429
    now[0] += 5
    assert (await client(app, path="/window"))["status"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_rate_limit_runs_before_pydantic_validation(
        protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()

    class Payload(BaseModel):
        value: int

    @app.limit(1, 60)
    @app.post("/validated")
    async def validated(body: Payload, res):
        await res.json(body.model_dump())

    first = await client(app, method="POST", path="/validated", body=b"{}")
    second = await client(app, method="POST", path="/validated", body=b"{}")

    assert first["status"] == 422
    assert second["status"] == 429
    assert json.loads(second["body"]) == {"error": "Too Many Requests"}
