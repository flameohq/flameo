import functools
import os
import sys
from collections import OrderedDict

import pytest

from flameo import templates


@pytest.fixture
def template_state(monkeypatch):
    monkeypatch.setattr(templates, "_last_template_directory", None)
    monkeypatch.setattr(templates, "_CALL_ENV_CACHE", OrderedDict())
    monkeypatch.setattr(templates, "_STRING_ENV_CACHE", OrderedDict())
    return templates


def make_templates(tmp_path, name="templates"):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def shout(value):
    return str(value).upper()


def exclaim(value):
    return str(value) + "!"


async def async_shout(value):
    return str(value).upper()


class NoNameCallable:
    def __call__(self, value):
        return value


def test_unwrap_peels_partial_and_wrapped_layers():
    def base(value):
        return value

    def wrapper(*args, **kwargs):
        return base(*args, **kwargs)

    wrapper.__wrapped__ = base

    assert templates._unwrap(functools.partial(wrapper)) is base
    assert templates._unwrap(wrapper) is base
    assert templates._unwrap(base) is base
    assert templates._unwrap(functools.partial(functools.partial(base))) is base


def test_unwrap_stops_after_the_limit_on_a_cyclic_wrapped_chain():
    def first():
        return 1

    def second():
        return 2

    first.__wrapped__ = second
    second.__wrapped__ = first

    unwrapped = templates._unwrap(first)
    assert unwrapped in (first, second)


def test_is_async_callable_detects_async_callables():
    def sync_fn():
        return 1

    def wrapper(*args, **kwargs):
        return async_shout()

    wrapper.__wrapped__ = async_shout

    class AsyncCallable:
        async def __call__(self):
            return 1

    class SyncCallable:
        def __call__(self):
            return 1

    assert templates.is_async_callable(async_shout) is True
    assert templates.is_async_callable(functools.partial(async_shout)) is True
    assert templates.is_async_callable(wrapper) is True
    assert templates.is_async_callable(AsyncCallable()) is True
    assert templates.is_async_callable(sync_fn) is False
    assert templates.is_async_callable(SyncCallable()) is False
    assert templates.is_async_callable(object()) is False
    assert templates.is_async_callable(42) is False


def test_resolve_filters_accepts_none_list_tuple_and_dict():
    assert templates.resolve_filters(None) == []
    assert templates.resolve_filters([shout]) == [("shout", shout)]
    assert templates.resolve_filters((shout,)) == [("shout", shout)]
    assert templates.resolve_filters({"loud": shout}) == [("loud", shout)]
    assert templates.resolve_filters({}) == []


@pytest.mark.parametrize(
    "filters, error, message",
    [
        (shout, TypeError, "expects a list of functions"),
        ("not-callable", TypeError, "expects a list of functions or a dict"),
        (42, TypeError, "expects a list of functions or a dict"),
        ([42], TypeError, "must be callable"),
        ([lambda value: value], ValueError, "Cannot derive a template filter name"),
        ([NoNameCallable()], ValueError, "Cannot derive a template filter name"),
        ({"broken": 42}, TypeError, "must be callable"),
        ({"bad name": shout}, ValueError, "Invalid template filter name"),
        ({"9bad": shout}, ValueError, "Invalid template filter name"),
        ({"": shout}, ValueError, "Invalid template filter name"),
        ({"with-dash": shout}, ValueError, "Invalid template filter name"),
    ],
)
def test_resolve_filters_rejects_invalid_inputs(filters, error, message):
    with pytest.raises(error, match=message):
        templates.resolve_filters(filters)


def test_build_environment_registers_filters_and_tracks_the_directory(tmp_path, template_state):
    directory = make_templates(tmp_path)
    env = templates.build_environment(str(directory), filters=[shout])

    assert env.filters["shout"] is shout
    assert env.is_async is False
    assert templates.get_last_template_directory() == str(directory)
    assert env.loader.searchpath == [str(directory)]


def test_build_environment_without_filters_creates_an_empty_filter_set(tmp_path, template_state):
    directory = make_templates(tmp_path)
    env = templates.build_environment(str(directory))

    assert env.filters.get("shout") is None
    assert env.is_async is False
    assert templates.get_last_template_directory() == str(directory)


def test_get_last_template_directory_is_none_until_a_directory_is_registered(template_state):
    assert templates.get_last_template_directory() is None


def test_build_environment_explains_a_missing_jinja2_install(tmp_path, template_state, monkeypatch):
    monkeypatch.setitem(sys.modules, "jinja2", None)

    with pytest.raises(ImportError, match="requires jinja2"):
        templates.build_environment(str(tmp_path))


@pytest.mark.asyncio
async def test_render_template_uses_the_sync_and_async_renderers(tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "greet.html").write_text("Hello {{ name | shout }}", encoding="utf-8")
    (directory / "greet_async.html").write_text("Hello {{ name | async_shout }}", encoding="utf-8")

    sync_env = templates.build_environment(str(directory), filters=[shout])
    assert await templates.render_template(sync_env, "greet.html", {"name": "Ada"}) == "Hello ADA"
    assert await templates.render_template(sync_env, "greet.html") == "Hello "

    async_env = templates.build_environment(str(directory), filters=[async_shout])
    assert async_env.is_async is True
    assert await templates.render_template(async_env, "greet_async.html", {"name": "Ada"}) == "Hello ADA"


def test_build_route_environment_overlays_filters_without_leaking_them(tmp_path, template_state):
    directory = make_templates(tmp_path)
    base = templates.build_environment(str(directory), filters=[shout])

    overlay = templates.build_route_environment(base, [("bang", exclaim)])

    assert overlay is not None
    assert overlay is not base
    assert overlay.filters["bang"] is exclaim
    assert overlay.filters["shout"] is shout
    assert "bang" not in base.filters
    assert templates.build_route_environment(base, []) is None


def test_build_route_environment_switches_to_async_for_async_filters(tmp_path, template_state):
    base = templates.build_environment(str(tmp_path))
    overlay = templates.build_route_environment(base, [("bang", async_shout)])

    assert overlay.is_async is True
    assert base.is_async is False


def test_build_call_environment_returns_the_base_environment_without_filters(tmp_path, template_state):
    base = templates.build_environment(str(tmp_path))

    assert templates.build_call_environment(base, None) is base
    assert templates.build_call_environment(base, []) is base


def test_build_call_environment_reuses_the_overlay_for_the_same_filters(tmp_path, template_state):
    base = templates.build_environment(str(tmp_path))

    first = templates.build_call_environment(base, [shout])
    second = templates.build_call_environment(base, [shout])

    assert first is second
    assert first.filters["shout"] is shout
    assert len(templates._CALL_ENV_CACHE) == 1

    named = templates.build_call_environment(base, {"loud": shout})
    assert named is not first
    assert named.filters["loud"] is shout
    assert len(templates._CALL_ENV_CACHE) == 2


def test_build_call_environment_rebuilds_when_the_cached_entry_does_not_match(tmp_path, template_state):
    base = templates.build_environment(str(tmp_path))
    pairs = templates.resolve_filters([shout])
    signature = tuple((name, id(fn)) for name, fn in pairs)
    key = (id(base), signature)
    stale = object()
    templates._CALL_ENV_CACHE[key] = (base, (("other_name", shout),), stale)

    rebuilt = templates.build_call_environment(base, [shout])

    assert rebuilt is not stale
    assert rebuilt.filters["shout"] is shout


def test_build_call_environment_evicts_the_oldest_entry_when_over_the_limit(tmp_path, monkeypatch, template_state):
    monkeypatch.setattr(templates, "_CALL_ENV_CACHE_MAX", 2)
    base = templates.build_environment(str(tmp_path))

    def first_filter(value):
        return value

    def second_filter(value):
        return value

    def third_filter(value):
        return value

    templates.build_call_environment(base, [first_filter])
    templates.build_call_environment(base, [second_filter])
    templates.build_call_environment(base, [third_filter])

    assert len(templates._CALL_ENV_CACHE) == 2
    cached_names = {
        name
        for _, pairs, _ in templates._CALL_ENV_CACHE.values()
        for name, _ in pairs
    }
    assert "first_filter" not in cached_names
    assert {"second_filter", "third_filter"} <= cached_names


def test_build_template_string_environment_caches_by_directory_and_filters(tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "a.txt").write_text("A", encoding="utf-8")

    first = templates.build_template_string_environment(str(directory), None)
    second = templates.build_template_string_environment(str(directory), None)

    assert first is second
    assert len(templates._STRING_ENV_CACHE) == 1

    with_filters = templates.build_template_string_environment(str(directory), [shout])
    assert with_filters is not first
    assert with_filters.filters["shout"] is shout
    assert len(templates._STRING_ENV_CACHE) == 2


def test_build_template_string_environment_accepts_multiple_directories(tmp_path, template_state):
    first_dir = make_templates(tmp_path, "first")
    second_dir = make_templates(tmp_path, "second")
    (second_dir / "b.txt").write_text("B", encoding="utf-8")

    env = templates.build_template_string_environment([str(first_dir), str(second_dir)], None)

    assert env.get_template("b.txt").render() == "B"


def test_build_template_string_environment_rebuilds_when_the_cached_entry_does_not_match(tmp_path, template_state):
    directory = make_templates(tmp_path)
    pairs = templates.resolve_filters([shout])
    signature = tuple((name, id(fn)) for name, fn in pairs)
    key = ((os.path.abspath(str(directory)),), signature)
    stale = object()
    templates._STRING_ENV_CACHE[key] = ((("other_name", shout),), stale)

    rebuilt = templates.build_template_string_environment(str(directory), [shout])

    assert rebuilt is not stale
    assert rebuilt.filters["shout"] is shout


def test_build_template_string_environment_evicts_when_over_the_limit(tmp_path, monkeypatch, template_state):
    monkeypatch.setattr(templates, "_STRING_ENV_CACHE_MAX", 1)
    first_dir = make_templates(tmp_path, "first")
    second_dir = make_templates(tmp_path, "second")

    templates.build_template_string_environment(str(first_dir), None)
    templates.build_template_string_environment(str(second_dir), None)

    assert len(templates._STRING_ENV_CACHE) == 1
    remaining = list(templates._STRING_ENV_CACHE.keys())
    assert remaining[0][0] == (os.path.abspath(str(second_dir)),)


def test_route_template_env_requires_an_app_template_environment(app_factory, template_state):
    app = app_factory()
    route_env = templates.RouteTemplateEnv([shout])

    with pytest.raises(RuntimeError, match="use_template"):
        route_env.resolve(app)

    with pytest.raises(RuntimeError, match="use_template"):
        route_env.resolve(None)


def test_route_template_env_caches_the_resolved_environment(app_factory, tmp_path, template_state):
    app = app_factory()
    app.use_template(str(tmp_path))
    route_env = templates.RouteTemplateEnv([shout])

    first = route_env.resolve(app)
    second = route_env.resolve(app)

    assert first is second
    assert first.filters["shout"] is shout
    assert templates.RouteTemplateEnv(None)._pairs == []


def test_template_to_string_renders_from_the_registered_directory(app_factory, tmp_path, template_state):
    from flameo import template_to_string

    directory = make_templates(tmp_path)
    (directory / "mail.html").write_text("Hello {{ name }}", encoding="utf-8")
    (directory / "mail_filtered.html").write_text("Hello {{ name | shout }}", encoding="utf-8")

    app = app_factory()
    app.use_template(str(directory))

    assert template_to_string("mail.html", {"name": "Ada"}) == "Hello Ada"
    assert template_to_string("mail_filtered.html", {"name": "Ada"}, filters=[shout]) == "Hello ADA"
    assert template_to_string("mail_filtered.html", {"name": "Ada"}, filters={"shout": shout}) == "Hello ADA"


def test_template_to_string_renders_async_filters(app_factory, tmp_path, template_state):
    from flameo import template_to_string

    directory = make_templates(tmp_path)
    (directory / "mail.txt").write_text("Hello {{ name | async_shout }}", encoding="utf-8")

    app = app_factory()
    app.use_template(str(directory), filters=[async_shout])

    assert template_to_string("mail.txt", {"name": "Ada"}, filters=[async_shout]) == "Hello ADA"


def test_template_to_string_requires_a_registered_template_directory(template_state):
    from flameo import template_to_string

    with pytest.raises(RuntimeError, match="no template folder registered yet"):
        template_to_string("mail.html")


@pytest.mark.asyncio
async def test_route_filters_are_scoped_to_the_route_that_declares_them(app_factory, asgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "app.html").write_text("{{ name | shout }}", encoding="utf-8")
    (directory / "route.html").write_text("{{ name | shout }}|{{ name | exclaim }}", encoding="utf-8")

    app = app_factory()
    app.use_template(str(directory), filters=[shout])

    @app.get("/route", filters=[exclaim])
    async def route(req, res):
        await res.render("route.html", {"name": "flameo"})

    @app.get("/plain")
    async def plain(req, res):
        await res.render("app.html", {"name": "flameo"})

    routed = await asgi_client(app, path="/route")
    assert routed["status"] == 200
    assert routed["body"] == b"FLAMEO|flameo!"
    assert routed["headers"]["content-type"][0].startswith("text/html")

    plain = await asgi_client(app, path="/plain")
    assert plain["status"] == 200
    assert plain["body"] == b"FLAMEO"
    assert "exclaim" not in app._template_env.filters


@pytest.mark.asyncio
async def test_route_with_async_filters_renders_through_the_async_renderer(app_factory, asgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "route.html").write_text("{{ name | async_shout }}", encoding="utf-8")

    app = app_factory()
    app.use_template(str(directory))

    @app.get("/route", filters=[async_shout])
    async def route(req, res):
        await res.render("route.html", {"name": "flameo"})

    routed = await asgi_client(app, path="/route")
    assert routed["status"] == 200
    assert routed["body"] == b"FLAMEO"


@pytest.mark.asyncio
async def test_route_filters_work_over_rsgi(app_factory, rsgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "route.html").write_text("{{ name | exclaim }}", encoding="utf-8")

    app = app_factory()
    app.use_template(str(directory))

    @app.get("/route", filters=[exclaim])
    async def route(req, res):
        await res.render("route.html", {"name": "flameo"})

    routed = await rsgi_client(app, path="/route")
    assert routed["status"] == 200
    assert routed["body"] == b"flameo!"


@pytest.mark.asyncio
async def test_per_call_filters_apply_to_a_single_render_only(app_factory, asgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "call.html").write_text("{{ name | wrap }}", encoding="utf-8")

    def wrap(value):
        return "[" + str(value) + "]"

    app = app_factory()
    app.use_template(str(directory))

    @app.get("/call")
    async def call(req, res):
        await res.render("call.html", {"name": "x"}, filters={"wrap": wrap})

    response = await asgi_client(app, path="/call")
    assert response["status"] == 200
    assert response["body"] == b"[x]"
    assert "wrap" not in app._template_env.filters


@pytest.mark.asyncio
async def test_per_call_filter_overlay_is_cached_between_requests(app_factory, asgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "call.html").write_text("{{ name | wrap }}", encoding="utf-8")

    def wrap(value):
        return "[" + str(value) + "]"

    app = app_factory()
    app.use_template(str(directory))

    @app.get("/call")
    async def call(req, res):
        await res.render("call.html", {"name": "x"}, filters=[wrap])

    first = await asgi_client(app, path="/call")
    second = await asgi_client(app, path="/call")

    assert first["body"] == second["body"] == b"[x]"
    assert len(templates._CALL_ENV_CACHE) == 1


@pytest.mark.asyncio
async def test_per_call_async_filters_render_through_the_async_renderer(app_factory, asgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "call.html").write_text("{{ name | async_shout }}", encoding="utf-8")

    app = app_factory()
    app.use_template(str(directory))

    @app.get("/call")
    async def call(req, res):
        await res.render("call.html", {"name": "x"}, filters=[async_shout])

    response = await asgi_client(app, path="/call")
    assert response["status"] == 200
    assert response["body"] == b"X"


@pytest.mark.asyncio
async def test_per_call_filters_sit_on_top_of_the_route_filters(app_factory, asgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "call.html").write_text("{{ name | shout }}|{{ name | exclaim }}|{{ name | wrap }}", encoding="utf-8")

    def wrap(value):
        return "[" + str(value) + "]"

    app = app_factory()
    app.use_template(str(directory), filters=[shout])

    @app.get("/call", filters=[exclaim])
    async def call(req, res):
        await res.render("call.html", {"name": "x"}, filters={"wrap": wrap})

    response = await asgi_client(app, path="/call")
    assert response["status"] == 200
    assert response["body"] == b"X|x!|[x]"


@pytest.mark.asyncio
async def test_html_templates_are_autoescaped(app_factory, asgi_client, tmp_path, template_state):
    directory = make_templates(tmp_path)
    (directory / "safe.html").write_text("{{ value }}", encoding="utf-8")
    (directory / "safe.txt").write_text("{{ value }}", encoding="utf-8")

    app = app_factory()
    app.use_template(str(directory))

    @app.get("/html")
    async def html(req, res):
        await res.render("safe.html", {"value": "<b>"})

    @app.get("/txt")
    async def txt(req, res):
        await res.render("safe.txt", {"value": "<b>"})

    html_response = await asgi_client(app, path="/html")
    assert html_response["body"] == b"&lt;b&gt;"
    txt_response = await asgi_client(app, path="/txt")
    assert txt_response["body"] == b"<b>"
