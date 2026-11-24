"""Endpoint response caching + the in-process cache fallback.

Two related concerns live here:

1. ``@app.cache(ttl=...)`` / ``@router.cache(ttl=...)`` response caching.
   Cached routes are keyed on ``METHOD + originalUrl`` (path + raw query
   string), store only completed 2xx non-streamed responses that set no
   cookies, and replay through the regular ``Response._send_body`` funnel
   so session-finalize, content-length handling and per-request
   compression negotiation all keep working on the hit path.

2. ``MemoryCacheStore`` — the Redis-compatible subset handed to every
   handler/middleware that declares a ``cache`` parameter when no
   ``cache_config`` was given (and the internal store for cached
   endpoint entries). All JSON (de)serialization uses orjson.
"""

import base64
import time
import warnings
from collections import OrderedDict

import orjson

_EPCACHE_PREFIX = "flameo:epcache:"
_MEMORY_STORE_MAX = 1024

_CACHE_WARNING = (
    "[flameo] Caching may not work accurately for HTTP methods "
    "other GET and HEAD"
)


def _resolve_cache(host):
    """The cache object every handler/middleware ``cache`` slot receives.

    Prefers the Redis client created by ``init_cache_pool``; falls back
    to the app's in-process store when no ``cache_config`` was provided
    (or before the pool finished initializing).
    """
    c = host.CACHE
    return c if c is not None else host._memory_cache


class MemoryCacheStore:
    """Redis-compatible in-process cache (the no-cache_config fallback).

    Values are stored as given and returned as strings from ``get()``
    (bytes are decoded utf-8). ``ex`` is always seconds. ``nx=True``
    stores only when the key does not exist (or has expired). All JSON
    work goes through orjson.
    """

    __slots__ = ("_data",)

    def __init__(self):
        # key -> [expires_at_monotonic | None, value]
        self._data = OrderedDict()

    # ----- internals -------------------------------------------------

    def _live_slot(self, key):
        """Return the slot for ``key`` if present and unexpired, else
        None (purging it when expired)."""
        slot = self._data.get(key)
        if slot is None:
            return None
        exp = slot[0]
        if exp is not None and exp <= time.monotonic():
            del self._data[key]
            return None
        return slot

    def _evict_if_overflow(self):
        if len(self._data) <= _MEMORY_STORE_MAX:
            return
        now = time.monotonic()
        dead = [k for k, slot in self._data.items()
                if slot[0] is not None and slot[0] <= now]
        for k in dead:
            del self._data[k]
        while len(self._data) > _MEMORY_STORE_MAX:
            self._data.popitem(last=False)

    # ----- redis-compatible API --------------------------------------

    async def set(self, key, value, ex=None, nx=False):
        slot = self._live_slot(key)
        if slot is not None and nx:
            return None
        expires = (time.monotonic() + ex) if ex is not None else None
        self._data[key] = [expires, value]
        self._evict_if_overflow()
        return True

    async def get(self, key):
        slot = self._live_slot(key)
        if slot is None:
            return None
        v = slot[1]
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", "replace")
        if isinstance(v, str):
            return v
        return str(v)

    async def exists(self, key):
        return self._live_slot(key) is not None

    async def set_json(self, key, value, ex=None, nx=False):
        return await self.set(key, orjson.dumps(value), ex=ex, nx=nx)

    async def get_json(self, key):
        v = await self.get(key)
        if v is None:
            return None
        return orjson.loads(v)

    async def incr(self, key, amount=1):
        slot = self._live_slot(key)
        if slot is None:
            raise KeyError(
                f"[flameo] incr/decr: key {key!r} does not exist in cache"
            )
        raw = slot[1]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, float):
            raise ValueError(
                f"[flameo] incr/decr: value at {key!r} is not an integer"
            )
        if isinstance(raw, int):
            current = raw
        elif isinstance(raw, str):
            try:
                current = int(raw)
            except ValueError:
                raise ValueError(
                    f"[flameo] incr/decr: value at {key!r} is not an integer"
                ) from None
        else:
            raise ValueError(
                f"[flameo] incr/decr: value at {key!r} is not an integer"
            )
        new = current + amount
        slot[1] = str(new)
        return new

    async def decr(self, key, amount=1):
        return await self.incr(key, -amount)

    async def rate_limit_hit(self, key, window_ms):
        slot = self._live_slot(key)
        now = time.monotonic()
        if slot is None:
            expires = now + window_ms / 1000
            self._data[key] = [expires, "1"]
            self._evict_if_overflow()
            return 1, max(0, round((expires - time.monotonic()) * 1000))

        current = int(slot[1]) + 1
        slot[1] = str(current)
        return current, max(0, round((slot[0] - time.monotonic()) * 1000))


# ----------------------------------------------------------------------
# Redis client JSON helpers — bound onto OUR client instance only; the
# redis library classes are never modified.
# ----------------------------------------------------------------------

async def _redis_set_json(self, key, value, ex=None, nx=False):
    return await self.set(key, orjson.dumps(value), ex=ex, nx=nx)


async def _redis_get_json(self, key):
    v = await self.get(key)
    return None if v is None else orjson.loads(v)


async def _redis_rate_limit_hit(self, key, window_ms):
    result = await self.eval(
        "local n = redis.call('INCR', KEYS[1]); "
        "if n == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]); end; "
        "return {n, redis.call('PTTL', KEYS[1])}",
        1,
        key,
        window_ms,
    )
    return int(result[0]), max(0, int(result[1]))


def bind_redis_json_helpers(client):
    """Attach ``set_json``/``get_json`` to the specific client instance
    created by ``init_cache_pool`` so every handler that takes a
    ``cache`` parameter gets them without touching the redis class."""
    import types
    client.set_json = types.MethodType(_redis_set_json, client)
    client.get_json = types.MethodType(_redis_get_json, client)
    client.rate_limit_hit = types.MethodType(_redis_rate_limit_hit, client)
    return client


# ----------------------------------------------------------------------
# Endpoint response caching
# ----------------------------------------------------------------------

class EndpointCacheSpec:
    """Per-decorator cache configuration.

    ``resolve`` hands back the pair of clients a cached dispatch needs:
    the one injected into handler/middleware ``cache`` slots and the one
    the response entries are stored in. With Redis configured both are
    the pool-backed client (captured here on first request and kept —
    handlers never re-initialize it); without a cache_config the handler
    slot gets the app's user-facing memory store while response entries
    live in a separate internal store so neither can evict the other.
    """

    __slots__ = ("ttl", "ex_redis", "_client")

    def __init__(self, ttl):
        self.ttl = ttl
        self.ex_redis = max(1, int(ttl))
        self._client = None

    def resolve(self, host):
        c = host.CACHE
        if c is not None:
            # Keep the pool-backed client on the spec ("take the pool
            # and keep it"); re-resolves automatically if the app ever
            # re-creates it (shutdown -> init cycle).
            self._client = c
            return c, c
        return host._memory_cache, host._response_memory_cache

    def key_for(self, req):
        return f"{_EPCACHE_PREFIX}{req.method}:{req.originalUrl}"


def warn_uncacheable_method(stacklevel=3):
    warnings.warn(_CACHE_WARNING, UserWarning, stacklevel=stacklevel)


# ----- entry codec -----------------------------------------------------
# Canonical entry: (status:int, headers:list[tuple[str, str|list]],
#                   content_type:str, body:bytes)
# Redis payload: orjson {"s","h","ct","b":base64}. The memory store keeps
# the same encoded bytes (it is just a different backend for the blob).

def _encode_entry(entry):
    status, headers, ct, body = entry
    return orjson.dumps({
        "s": status,
        "h": [[k, v] for k, v in headers],
        "ct": ct,
        "b": base64.b64encode(body).decode("ascii"),
    })


def _decode_entry(payload):
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("ascii")
        raw = orjson.loads(payload)
        return (
            raw["s"],
            [(k, v) for k, v in raw["h"]],
            raw["ct"],
            base64.b64decode(raw["b"]),
        )
    except Exception:
        return None


# ----- hit / miss machinery (shared by ASGI + RSGI dispatches) ---------

async def _cache_get(client, key):
    try:
        return await client.get(key)
    except Exception:
        # A down/slow backend must never take the endpoint down —
        # degrade to a miss.
        return None


async def _replay_entry(res, entry):
    """Replay a cached response through the regular ``_send_body`` funnel.

    Restores the handler-installed headers first, marks the hit, then
    lets ``_send_body`` re-apply session finalize, content-length and
    (per request) compression negotiation.
    """
    status, headers, ct, body = entry
    ct_bytes = ct.encode("latin-1") if isinstance(ct, str) else ct
    if hasattr(res, "_header_map"):  # ASGI Response
        # Start from the app's base headers (normally empty) so a replayed
        # response carries exactly what the live path would have.
        hmap = dict(getattr(res._app, "_base_header_map", None) or {})
        for k, v in headers:
            kb = k.encode("latin-1")
            if isinstance(v, list):
                hmap[kb] = [item.encode("latin-1") for item in v]
            else:
                hmap[kb] = v.encode("latin-1")
        res._header_map = hmap
    else:  # RSGIResponse (str-pair ``_hdr`` list)
        res._hdr = [(k, v) for k, v in headers]
    res.setHeader("X-Flameo-Cache", "HIT")
    await res._send_body(body, ct_bytes, status)


async def _maybe_store(spec, client, key, capture, res):
    """Store the captured response if it qualifies (called after a clean
    handler completion)."""
    if not capture or not res._sent:
        return
    if res._streamed or res._streaming:
        return
    status = capture.get("status")
    if status is None or not (200 <= status < 300):
        return
    headers = capture.get("headers")
    if headers is None:
        return
    if any(k.lower() == "set-cookie" for k, _v in headers):
        # Never cache responses that mint cookies — a session/CSRF
        # cookie baked for one client must not be replayed to others.
        return
    entry = (status, headers, capture["ct"], capture["body"])
    try:
        payload = _encode_entry(entry)
    except Exception:
        return
    ex = spec.ttl if isinstance(client, MemoryCacheStore) else spec.ex_redis
    try:
        await client.set(key, payload, ex=ex)
    except Exception:
        pass


async def cached_invoke(spec, store, key, res, invoke):
    """Cache lookup + capture around the raw handler invocation.

    ``invoke`` is a zero-arg async callable that runs the handler (with
    its injected slots already materialized). On a hit the handler never
    runs. Route middlewares have already run by the time this is called.
    """
    payload = await _cache_get(store, key)
    if payload is not None:
        entry = _decode_entry(payload)
        if entry is not None:
            await _replay_entry(res, entry)
            return None
    try:
        res.setHeader("X-Flameo-Cache", "MISS")
    except Exception:
        pass
    capture = {}
    res._cache_capture__ = capture
    ok = False
    try:
        result = await invoke()
        ok = True
        return result
    finally:
        res._cache_capture__ = None
        if ok:
            await _maybe_store(spec, store, key, capture, res)
