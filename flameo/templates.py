import functools
import inspect
import os
import re
from collections import OrderedDict

# Jinja2 looks a filter up by ``nodes.Name``, so the name has to be a plain
# identifier. Only the ``\w`` subset is accepted -- that rules out spaces,
# dashes and leading digits, and keeps a name from ever breaking out of
# the ``{{ value | name }}`` slot.
_FILTER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Upper bound for the unwrap loop below; protects against a pathological
# ``__wrapped__`` cycle.
_UNWRAP_LIMIT = 16
_CALL_ENV_CACHE_MAX = 128
_CALL_ENV_CACHE = OrderedDict()
_STRING_ENV_CACHE_MAX = 64
_STRING_ENV_CACHE = OrderedDict()


def _unwrap(fn):
    """Peel ``functools.partial`` / ``__wrapped__`` layers off ``fn``."""
    for _ in range(_UNWRAP_LIMIT):
        if isinstance(fn, functools.partial):
            fn = fn.func
            continue
        wrapped = getattr(fn, "__wrapped__", None)
        if wrapped is not None and wrapped is not fn:
            fn = wrapped
            continue
        break
    return fn


def is_async_callable(fn) -> bool:
    """True when calling ``fn`` yields a coroutine.

    Handles plain ``async def`` functions, ``functools.partial`` wrappers,
    decorated functions and callable objects with an ``async def __call__``.
    """
    target = _unwrap(fn)
    if inspect.iscoroutinefunction(target):
        return True
    call = getattr(target, "__call__", None)
    if call is None:
        return False
    return inspect.iscoroutinefunction(_unwrap(call))


def _derive_name(fn) -> str:
    """Derive the template filter name from a function's ``__name__``."""
    if not callable(fn):
        raise TypeError(
            "[flameo] template 'filters' must be callable; got "
            f"{type(fn).__name__}."
        )
    name = getattr(fn, "__name__", "") or ""
    if not name or name == "<lambda>":
        raise ValueError(
            "[flameo] Cannot derive a template filter name from this "
            "function. Pass a dict ({\"name\": function}) to name it "
            "explicitly."
        )
    return name


def resolve_filters(filters) -> list:
    """Normalise the ``filters`` argument into ``(name, function)`` pairs.

    Accepts a list/tuple of callables (named after ``fn.__name__``) or a
    dict mapping an explicit name to a callable.
    """
    if filters is None:
        return []

    if isinstance(filters, dict):
        pairs = list(filters.items())
    elif isinstance(filters, (list, tuple)):
        pairs = [(_derive_name(fn), fn) for fn in filters]
    elif callable(filters):
        raise TypeError(
            "[flameo] template 'filters' expects a list of functions; got a "
            "single function. Pass [your_function] instead."
        )
    else:
        raise TypeError(
            "[flameo] template 'filters' expects a list of functions or a "
            "dict of {name: function}; got "
            f"{type(filters).__name__}."
        )

    resolved = []
    for name, fn in pairs:
        if not callable(fn):
            raise TypeError(
                "[flameo] template 'filters' must be callable; got "
                f"{type(fn).__name__} for filter {name!r}."
            )
        if not isinstance(name, str) or not _FILTER_NAME_RE.match(name):
            raise ValueError(
                f"[flameo] Invalid template filter name {name!r}. Names must "
                "be plain identifiers (letters, digits and underscores, not "
                "starting with a digit)."
            )
        resolved.append((name, fn))
    return resolved


# The template folder most recently registered through
# ``app.use_template()``. ``utils.template_to_string()`` reads this so its
# ``template_name`` argument stays relative to the app's template root.
_last_template_directory = None
_last_template_filter_map: dict = {}


def get_last_template_directory():
    """Return the template folder registered by ``use_template()``, if any."""
    return _last_template_directory


def get_last_template_filters():
    """Return app-wide filters registered by ``use_template()``."""
    return dict(_last_template_filter_map)


def build_environment(directory: str, filters=None):
    """Build the Jinja2 ``Environment`` used by ``use_template()``.
    """
    global _last_template_directory
    _last_template_directory = directory
    pairs = resolve_filters(filters)
    _last_template_filter_map.clear()
    _last_template_filter_map.update(pairs)

    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as e:
        raise ImportError(
            "[flameo] use_template() requires jinja2, but it is not installed.\n"
            "Install it with:\n\n"
            "    pip install jinja2\n"
        ) from e

    enable_async = any(is_async_callable(fn) for _, fn in pairs)
    env = Environment(
        loader=FileSystemLoader(directory),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=enable_async,
    )
    for name, fn in pairs:
        env.filters[name] = fn
    return env


async def render_template(env, template_name: str, context: dict = None) -> str:
    """Render ``template_name`` through ``env`` and return the string.
    """
    template = env.get_template(template_name)
    variables = context if context is not None else {}
    if getattr(env, "is_async", False):
        return await template.render_async(variables)
    return template.render(variables)


def build_route_environment(base_env, pairs):
    """Build an environment carrying the app filters plus route filters.
    """
    if not pairs:
        return None

    env = base_env.overlay()
    # ``overlay()`` copies the parent's filter dict by reference -- take
    # our own copy so registering a route filter never leaks back into
    # the app-wide environment.
    env.filters = dict(base_env.filters)
    for name, fn in pairs:
        env.filters[name] = fn

    # An async route filter forces async mode. A template compiled in
    # sync mode cannot be awaited, so the cache is dropped either way and
    # every template is recompiled against this environment's filters.
    if any(is_async_callable(fn) for _, fn in pairs):
        env.is_async = True
    env.cache.clear()
    return env


def build_call_environment(base_env, filters):
    """Return a cached overlay for filters supplied to one render call.

    Per-call filters are often module-level functions reused on every
    request. Reusing their overlay preserves Jinja's compiled-template
    cache instead of rebuilding an environment and recompiling each time.
    The bounded cache retains strong references to the base environment and
    filter callables, preventing object-id reuse from returning a stale env.
    """
    pairs = resolve_filters(filters)
    if not pairs:
        return base_env

    signature = tuple((name, id(fn)) for name, fn in pairs)
    key = (id(base_env), signature)
    cached = _CALL_ENV_CACHE.get(key)
    if cached is not None:
        cached_base, cached_pairs, env = cached
        if cached_base is base_env and len(cached_pairs) == len(pairs) and all(
            old_name == name and old_fn is fn
            for (old_name, old_fn), (name, fn) in zip(cached_pairs, pairs)
        ):
            _CALL_ENV_CACHE.move_to_end(key)
            return env

    env = build_route_environment(base_env, pairs)
    _CALL_ENV_CACHE[key] = (base_env, tuple(pairs), env)
    _CALL_ENV_CACHE.move_to_end(key)
    if len(_CALL_ENV_CACHE) > _CALL_ENV_CACHE_MAX:
        _CALL_ENV_CACHE.popitem(last=False)
    return env


def build_template_string_environment(directory, filters=None):
    """Return a cached environment for repeated ``template_to_string`` calls."""
    pairs = resolve_filters(filters)
    explicit = dict(pairs)
    base = dict(_last_template_filter_map)
    merged = {**base, **explicit}
    pairs = list(merged.items())
    directories = directory if isinstance(directory, (list, tuple)) else (directory,)
    directory_key = tuple(os.path.abspath(os.fspath(item)) for item in directories)
    signature = tuple((name, id(fn)) for name, fn in pairs)
    key = (directory_key, signature)
    cached = _STRING_ENV_CACHE.get(key)
    if cached is not None:
        cached_pairs, env = cached
        if len(cached_pairs) == len(pairs) and all(
            old_name == name and old_fn is fn
            for (old_name, old_fn), (name, fn) in zip(cached_pairs, pairs)
        ):
            _STRING_ENV_CACHE.move_to_end(key)
            return env

    env = build_environment(directory, filters=dict(pairs))
    _last_template_filter_map.clear()
    _last_template_filter_map.update(base)
    _STRING_ENV_CACHE[key] = (tuple(pairs), env)
    _STRING_ENV_CACHE.move_to_end(key)
    if len(_STRING_ENV_CACHE) > _STRING_ENV_CACHE_MAX:
        _STRING_ENV_CACHE.popitem(last=False)
    return env


class RouteTemplateEnv:
    """Lazily-built, route-scoped template environment.

    """

    __slots__ = ("_filters", "_pairs", "_envs")

    def __init__(self, filters) -> None:
        # Validate eagerly so a bad filter is reported at registration
        # time rather than on the first request.
        self._filters = filters
        self._pairs = resolve_filters(filters)
        self._envs: dict = {}

    def resolve(self, app):
        """Return the route environment for ``app``, building it if needed."""
        base = getattr(app, "_template_env", None) if app is not None else None
        if base is None:
            raise RuntimeError(
                "Initialize 'app.use_template()' before rendering a route "
                "that registers its own filters"
            )
        # The base environment doubles as the cache key -- it keeps the
        # entry alive, so its id cannot be reused by another object.
        env = self._envs.get(base)
        if env is None:
            env = build_route_environment(base, self._pairs)
            self._envs[base] = env
        return env
