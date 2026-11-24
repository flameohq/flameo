import time

import pytest

from flameo.cycles import RouterMap


@pytest.mark.performance
def test_warm_route_lookup_smoke():
    routes = RouterMap()
    handler = object()
    routes.add_route("GET", "/health", handler)
    assert routes.match("GET", "/health")[0] is handler

    started = time.perf_counter()
    for _ in range(10_000):
        assert routes.match("GET", "/health")[0] is handler
    elapsed = time.perf_counter() - started

    # Deliberately broad: this guards against an accidental pathological
    # hot-path regression, not small machine-to-machine timing changes.
    assert elapsed < 2.0
