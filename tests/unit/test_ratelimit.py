from types import SimpleNamespace

import pytest

from flameo.endpoint_cache import MemoryCacheStore, bind_redis_json_helpers
from flameo.ratelimit import RateLimitSpec


@pytest.mark.parametrize("count", [0, -1, 1.5, True, "1"])
def test_rate_limit_rejects_invalid_count(count):
    with pytest.raises(ValueError, match="count"):
        RateLimitSpec(count, 1)


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True, "1"])
def test_rate_limit_rejects_invalid_window(seconds):
    with pytest.raises(ValueError, match="seconds"):
        RateLimitSpec(1, seconds)


def test_rate_limit_rejects_unrepresentable_window():
    with pytest.raises(ValueError, match="seconds"):
        RateLimitSpec(1, 10**1000)


def test_rate_limit_rejects_non_callable_key_function():
    with pytest.raises(TypeError, match="key_function"):
        RateLimitSpec(1, 1, "user")


@pytest.mark.asyncio
async def test_memory_rate_limit_counter_is_fixed_window(monkeypatch):
    from flameo import endpoint_cache

    now = [10.0]
    monkeypatch.setattr(endpoint_cache.time, "monotonic", lambda: now[0])
    cache = MemoryCacheStore()

    assert await cache.rate_limit_hit("key", 5000) == (1, 5000)
    now[0] += 1
    assert await cache.rate_limit_hit("key", 5000) == (2, 4000)
    now[0] += 4
    assert await cache.rate_limit_hit("key", 5000) == (1, 5000)


def test_redis_rate_limit_helper_is_bound_to_client_instance():
    class FakeRedis:
        async def eval(self, script, numkeys, key, window_ms):
            self.args = (script, numkeys, key, window_ms)
            return [3, 1200]

    client = bind_redis_json_helpers(FakeRedis())
    assert not hasattr(FakeRedis, "rate_limit_hit")

    import asyncio

    assert asyncio.run(client.rate_limit_hit("k", 5000)) == (3, 1200)
    assert client.args[1:] == (1, "k", 5000)


@pytest.mark.asyncio
async def test_rate_limit_uses_redis_when_configured():
    from flameo import Flameo

    class FakeRedis:
        async def rate_limit_hit(self, key, window_ms):
            self.key = key
            return 1, window_ms

    app = Flameo()
    app.CACHE = FakeRedis()
    app._memory_rate_limit = MemoryCacheStore()
    req = SimpleNamespace(headers={}, _base_url="")
    res = SimpleNamespace(setHeader=lambda *args: None, json=lambda *args, **kwargs: None)
    spec = RateLimitSpec(1, 2)

    assert await spec.check(req, res, app, "GET", "/x") is True
    assert app.CACHE.key.startswith("flameo:ratelimit:GET:/x:")
    assert app._memory_rate_limit._data == {}


@pytest.mark.asyncio
async def test_rate_limit_supports_async_key_function():
    app = SimpleNamespace(CACHE=None, _memory_rate_limit=MemoryCacheStore())
    req = SimpleNamespace(headers={}, _base_url="")
    res = SimpleNamespace(setHeader=lambda *args: None, json=lambda *args, **kwargs: None)

    async def key_function(request):
        return "async-user"

    spec = RateLimitSpec(1, 2, key_function)
    assert await spec.check(req, res, app, "GET", "/x") is True
    assert len(app._memory_rate_limit._data) == 1


def test_app_and_router_accept_both_key_function_names(app_factory):
    from flameo import Router

    app = app_factory()
    router = Router()
    assert app.limit(1, 1, key_func=lambda req: req.ip)
    assert router.limit(1, 1, key_function=lambda req: req.ip)
    with pytest.raises(ValueError, match="only one"):
        app.limit(1, 1, lambda req: req.ip, key_func=lambda req: req.ip)


def test_rate_limit_decorator_validates_and_retrofits_route(app_factory):
    app = app_factory()

    @app.get("/retrofit")
    async def handler(req, res):
        await res.send("ok")

    app.limit(1, 1)(handler)
    dispatch, _, _ = app.router.match("GET", "/retrofit")
    assert dispatch is not handler


def test_router_limit_must_be_applied_before_mount(app_factory):
    from flameo import Router

    app = app_factory()
    router = Router()

    @router.get("/x")
    async def handler(req, res):
        await res.send("ok")

    app.use_router("/api", router)
    with pytest.raises(ValueError, match="before app.use_router"):
        router.limit(1, 1)(handler)


def test_limit_decorator_warns_for_a_different_owner(app_factory):
    first = app_factory()
    second = app_factory()

    @first.get("/x")
    async def handler(req, res):
        await res.send("ok")

    with pytest.warns(UserWarning, match="different app/router"):
        second.limit(1, 1)(handler)


def test_limit_decorator_rejects_non_function_handler(app_factory):
    with pytest.raises(TypeError, match="expects a function handler"):
        app_factory().limit(1, 1)(len)
