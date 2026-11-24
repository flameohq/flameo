import asyncio, orjson, re, sys, os, mimetypes, uuid, inspect, functools
import heapq
from datetime import date, datetime, timezone
import email.utils
from typing import Any, Dict, Tuple, List, Callable, Optional
from collections import defaultdict
from urllib.parse import parse_qs, quote as _url_quote
from urllib.parse import unquote as _url_unquote
from http import HTTPStatus as _HTTPStatus
from .models import DotDict
from .parser import RequestParser, parse_multipart_body_streaming
from .templates import build_call_environment, render_template

MIN_BODY_TO_THREAD = 128*1024

DEFAULT_MAX_BODY = 10*1024*1024

_JSON_CT = b"application/json"
_TEXT_CT = b"text/plain; charset=utf-8"


_NP_TUPLE = None


_VALID_JSONP_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$")
try:
    import numpy as _np
    _NP_TUPLE = (_np.ndarray, _np.integer, _np.floating, _np.bool_)
except ImportError:  # pragma: no cover - numpy is a test-only dependency
    _np = None


def _orjson_default(obj):
    """orjson ``default=`` callback.

    Falls back to ``vars(obj)`` for plain user classes (e.g. a
    ``MerchantHistory`` produced by ``bind_row_to_model``) so handlers
    can ``await res.json(await conn.fetch(..., model=MerchantHistory))``
    without writing a custom encoder. Skips ``__dict__`` access for
    objects that already expose a dict-like mapping (orjson handles
    those natively).
    """
    # If orjson can't handle it natively and it isn't one of the
    # numpy abstract types, attempt ``vars(obj)`` — covers any plain
    # class that stores state on ``self``.
    try:
        d = obj.__dict__
    except AttributeError:
        # Last-resort: stringify so the response still serializes
        # instead of raising ``TypeError``.
        return str(obj)
    return d


def _orjson_dumps_fast(data):
    tup = _NP_TUPLE
    if tup is not None and isinstance(data, tup):
        return orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY)
    return orjson.dumps(data, default=_orjson_default)

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Sentinel for lazy-cache lookups (distinguishes "not present" from a
# cached value of None, e.g. an empty query dict).
_MISSING = object()

# Sentinel for Request._files so we don't allocate a dict per request
# for handlers that never read multipart files (the /hello case).
_FILES_SENTINEL: dict = {}

# Cache for ``_resolve_safe_path`` successful results. Keyed by
# ``(caller_file, normalized_path)`` — both stable across requests, so
# the same handler serving the same file repeatedly can skip the
# ``inspect.stack()`` walk + ``commonpath`` containment check on every
# call. We only cache the **success** path (``err is None``) so the
# 404 / same-dir / escape verdicts stay live and a file created after
# a miss is still served correctly. Bounded to keep memory in check.
from collections import OrderedDict
_PATH_CHECK_CACHE: "OrderedDict[Tuple[str, str], Tuple[str, str]]" = OrderedDict()
_PATH_CHECK_CACHE_MAX = 1024


def _resolve_safe_path(file_path: str):
    """Resolve a file path against the caller's directory with containment checks.
    """
    decoded_path = _url_unquote(file_path or "")
    normalized_path = os.path.normpath(decoded_path)

    # Identify the caller. We must skip our own internal frames (this
    # function itself, the closure that wraps it, and the lazy
    # ``__getattr__`` that materializes the closure on first access) so
    # that ``caller_dir`` reflects the user-defined handler that
    # actually wrote ``res.sendFile(...)``. If introspection fails
    # (e.g. running under a debugger with stripped frames) fall back to
    # CWD, which is still bounded below by the containment check.
    try:
        stack = inspect.stack()
        caller_file = os.path.abspath(".")
        for frame_info in stack[1:]:
            f = frame_info.filename
            if not f:
                continue
            abs_f = os.path.abspath(f)
            # Skip the framework's own files (cycles.py holds every
            # frame between us and the user's handler).
            if abs_f.endswith("cycles.py") or abs_f.endswith("flameo.py"):
                continue
            caller_file = abs_f
            break
    except Exception:
        caller_file = os.path.abspath(".")
    caller_dir = os.path.dirname(os.path.realpath(caller_file))

    # Cache lookup is keyed on the *resolved* caller_file (post abspath)
    # so two callers in the same directory share the same cache row.
    cache_key = (caller_file, normalized_path)
    cache = _PATH_CHECK_CACHE
    if cache_key in cache:
        cached_path, caller_dir = cache[cache_key]
        # LRU touch — move to the back so it's the most-recently-used.
        cache.move_to_end(cache_key)
        # Re-resolve: a symlink swapped in after the first call must not
        # reuse a stale containment verdict.
        resolved_path = os.path.realpath(cached_path)
        try:
            if os.path.commonpath([caller_dir, resolved_path]) != caller_dir:
                return resolved_path, caller_dir, "escape"
        except ValueError:
            return resolved_path, caller_dir, "escape"
        if not os.path.isfile(resolved_path):
            return resolved_path, caller_dir, "not_found"
        return resolved_path, caller_dir, None

    # Join first, then resolve ``..`` segments and symlinks, so an escape
    # via traversal *or* a symlink is caught by the containment test below.
    resolved_path = os.path.realpath(os.path.join(caller_dir, normalized_path))

    # Containment: ensure the resolved path is *under* caller_dir.
    # ``commonpath`` raises if the two paths live on different drives
    # on Windows, which we treat as "escape" too.
    try:
        common = os.path.commonpath([caller_dir, resolved_path])
    except ValueError:
        return resolved_path, caller_dir, "escape"
    if common != caller_dir:
        return resolved_path, caller_dir, "escape"

    # Same-directory block: we deliberately do NOT serve files that
    # sit next to the route file. The caller must use a subdirectory.
    file_dir = os.path.dirname(resolved_path)
    if file_dir == caller_dir:
        return resolved_path, caller_dir, "same_dir"

    if not os.path.isfile(resolved_path):
        return resolved_path, caller_dir, "not_found"

    # Cache only the success path. Insertion-order tracks recency; we
    # evict the oldest entry once the cache exceeds the cap.
    cache[cache_key] = (resolved_path, caller_dir)
    if len(cache) > _PATH_CHECK_CACHE_MAX:
        cache.popitem(last=False)
    return resolved_path, caller_dir, None


def _make_send_file(app, res):
    """Build the per-Response ``sendFile`` closure lazily.

    Streams the file in chunks via ASGI ``http.response.body`` events
    rather than buffering the whole file in memory. The closure is only
    constructed when a handler actually calls ``res.sendFile(...)`` —
    see Response.__getattr__ — so the per-request cost (closure cells +
    ``inspect.stack`` walk) is paid only on routes that need it.
    """
    async def sendFile(file_path, chunk_size=256 * 1024):
        resolved, file_size, err = await _safe_resolve_for_file(
            res, file_path, caller_label="sendFile",
        )
        if err is not None:
            return

        
        mime_type, _ = mimetypes.guess_type(resolved)
        if not mime_type:
            # Unknown extension: still render inline as text. ``sendFile``
            # is "render on browser" semantics, so we *don't* fall back to
            # ``application/octet-stream`` (which Chrome / Brave /
            # Firefox treat as "always download" even with
            # ``Content-Disposition: inline``). ``text/plain; charset=utf-8``
            # is the safe default — Chrome renders it as a preview pane,
            # and the explicit charset keeps non-ASCII bytes intact.
            mime_type = "text/plain; charset=utf-8"

        basename = os.path.basename(resolved) or "file"
        ascii_fallback = basename.encode("ascii", "replace").decode("ascii")
        encoded_star = _url_quote(basename, safe="")
        inline_disposition = (
            f'inline; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{encoded_star}"
        ).encode("latin-1")

        await res._send_file_streaming(
            resolved_path=resolved,
            content_type=mime_type.encode(),
            status=200,
            chunk_size=chunk_size,
            file_size=file_size,
            extra_headers=(
                (b"content-disposition", inline_disposition),
                (b"x-content-type-options", b"nosniff"),
            ),
        )

    return sendFile


def _make_download(app, res, default_filename: Optional[str] = None):
    """Build the per-Response ``download`` closure lazily.
    """
    async def download(file_path, filename=None, chunk_size=256 * 1024):
        resolved, file_size, err = await _safe_resolve_for_file(
            res, file_path, caller_label="download",
        )
        if err is not None:
            return

        if not filename:
            filename = os.path.basename(resolved) or default_filename or "download"

        ascii_fallback = filename.encode("ascii", "replace").decode("ascii")
        encoded_star = _url_quote(filename, safe="")
        disposition = (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{encoded_star}"
        )

        await res._send_file_streaming(
            resolved_path=resolved,
            content_type=b"application/octet-stream",
            status=200,
            chunk_size=chunk_size,
            file_size=file_size,
            extra_headers=((b"content-disposition", disposition.encode("latin-1")),),
        )

    return download


async def _safe_resolve_for_file(res, file_path: str, *, caller_label: str):
    """Shared path-safety + size-probe for ``sendFile`` and ``download``.

    Returns ``(resolved_path, file_size, err_token)``:
      * ``err_token`` is one of ``"same_dir"``, ``"escape"``, ``"not_found"``.
        When set, the helper has already written the corresponding 403/404
        response on ``res`` and the caller should just ``return``.
      * On success ``err_token`` is ``None`` and ``file_size`` may be
        ``None`` if ``os.path.getsize`` failed (the streaming code treats
        that as "unknown" and skips Content-Length).

    Both ASGI ``Response.sendFile``/``download`` and the RSGI equivalents
    go through this same skeleton, so a path-safety or error-message
    fix is guaranteed to apply to all four entry points at once.
    """
    resolved_path, _caller_dir, err = _resolve_safe_path(file_path)
    if err == "same_dir":
        await res.send(
            f"Access denied: {caller_label}() does not serve files from the caller's directory.",
            status=403,
        )
        return resolved_path, None, err
    if err == "escape":
        await res.send("Access denied: path escapes the caller's directory.", status=403)
        return resolved_path, None, err
    if err == "not_found":
        await res.send(f"File not found: {file_path}", status=404)
        return resolved_path, None, err

    try:
        file_size = os.path.getsize(resolved_path)
    except OSError:
        file_size = None
    return resolved_path, file_size, None


def _read_whole_file(resolved_path: str) -> bytes:
    """Blocking read of an entire file into a bytes object.

    Used by ``_send_file_streaming`` for tiny files (size < 64 KiB) where
    the per-chunk executor hop + chunked-transfer framing overhead exceeds
    the cost of one ``read()`` + one ``http.response.body`` event. Lives
    outside the coroutine so ``run_in_executor`` schedules it on a worker
    thread rather than holding the event loop for a blocking syscall.

    Reads in binary mode so CRLF translation isn't silently applied on
    Windows (text mode would corrupt binary assets like images).
    """
    with open(resolved_path, "rb") as f:
        return f.read()


def _make_rsgi_send_file(res):
    """Lazy builder for ``RSGIResponse.sendFile``.

    RSGI returns one complete ``response_bytes`` payload rather than a
    streaming channel, so the file is read once on an executor thread and
    placed directly in that final response buffer.
    """
    async def sendFile(file_path, chunk_size=256 * 1024):
        resolved, file_size, err = await _safe_resolve_for_file(
            res, file_path, caller_label="sendFile",
        )
        if err is not None:
            return

        mime_type, _ = mimetypes.guess_type(resolved)
        if not mime_type:
            # Unknown extension: still render inline as text. ``sendFile``
            # is "render on browser" semantics, so we *don't* fall back to
            # ``application/octet-stream`` (which Chrome / Brave /
            # Firefox treat as "always download" even with
            # ``Content-Disposition: inline``). ``text/plain; charset=utf-8``
            # is the safe default — Chrome renders it as a preview pane,
            # and the explicit charset keeps non-ASCII bytes intact.
            mime_type = "text/plain; charset=utf-8"

        basename = os.path.basename(resolved) or "file"
        ascii_fallback = basename.encode("ascii", "replace").decode("ascii")
        encoded_star = _url_quote(basename, safe="")
        inline_disposition = (
            f'inline; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{encoded_star}"
        )

        loop = asyncio.get_running_loop()
        # RSGI returns a single response_bytes payload, so use one executor
        # hop to read the final body instead of one hop per chunk followed by
        # a full-body join (which temporarily duplicates the payload).
        body = await loop.run_in_executor(None, _read_whole_file, resolved)
        total = len(body)

        res._body = body
        res._status = 200
        # Replace headers wholesale — the user may have called
        # setHeader() before sendFile(), but sendFile owns the
        # content-type/content-length pair plus the inline-disposition
        # + nosniff hints that keep Chrome/Brave from auto-downloading
        # text/html, text/plain, application/json, image/svg+xml, etc.
        res._hdr = [
            ("content-type", mime_type),
            ("content-length", str(total)),
            ("content-disposition", inline_disposition),
            ("x-content-type-options", "nosniff"),
        ]
        res._sent = True

    return sendFile


def _make_rsgi_download(res, default_filename: Optional[str] = None):
    """Lazy builder for ``RSGIResponse.download``.

    Reads the file once on an executor thread and emits the complete body
    with a ``Content-Disposition`` header (RFC 5987 ``filename*`` for
    non-ASCII).
    """
    async def download(file_path, filename=None, chunk_size=256 * 1024):
        resolved, file_size, err = await _safe_resolve_for_file(
            res, file_path, caller_label="download",
        )
        if err is not None:
            return

        if not filename:
            filename = os.path.basename(resolved) or default_filename or "download"

        ascii_fallback = filename.encode("ascii", "replace").decode("ascii")
        encoded_star = _url_quote(filename, safe="")
        disposition = (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{encoded_star}"
        )

        loop = asyncio.get_running_loop()
        # RSGI buffers the response body; avoid repeated executor handoffs
        # and the extra join copy for the final payload.
        body = await loop.run_in_executor(None, _read_whole_file, resolved)
        total = len(body)

        res._body = body
        res._status = 200
        res._hdr = [
            ("content-type", "application/octet-stream"),
            ("content-length", str(total)),
            ("content-disposition", disposition),
        ]
        res._sent = True

    return download
SLUG_RE = r"[-a-zA-Z0-9_]+"
PATH_RE = r".+"


def _to_int(value: str) -> int:
    return int(value)


def _to_float(value: str) -> float:
    return float(value)


def _to_uuid(value: str):
    return uuid.UUID(value)


def _to_date(value: str):
    return date.fromisoformat(value)


def _to_datetime(value: str):
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")


def _to_bool(value: str) -> bool:
    if value.lower() in ("true", "1", "yes", "on"):
        return True
    if value.lower() in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _to_slug(value: str) -> str:
    return value


def _to_path(value: str) -> str:
    return value


DEFAULT_CONVERTERS: Dict[str, Tuple[str, Callable[[str], Any]]] = {
    "str":    (r"[^/]+",                     str),
    "int":    (r"-?\d+",                     _to_int),
    "float":  (r"-?\d+(?:\.\d+)?",           _to_float),
    "uuid":   (UUID_RE,                      _to_uuid),
    "date":   (r"\d{4}-\d{2}-\d{2}",         _to_date),
    "datetime": (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", _to_datetime),
    "bool":   (r"(?:[Tt][Rr][Uu][Ee]|[Ff][Aa][Ll][Ss][Ee]|1|0|[Yy][Ee][Ss]|[Nn][Oo]|[Oo][Nn]|[Oo][Ff][Ff])", _to_bool),
    "slug":   (SLUG_RE,                      _to_slug),
    "path":   (PATH_RE,                      _to_path),
}


class ConverterRegistry:
    def __init__(self) -> None:
        self._converters: Dict[str, Tuple[str, Callable[[str], Any]]] = dict(DEFAULT_CONVERTERS)

    def register(self, name: str, regex: str, caster: Callable[[str], Any]) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("Converter name must be a non-empty string")
        if not name.isidentifier():
            raise ValueError(f"Converter name '{name}' must be a valid Python identifier")
        if name in DEFAULT_CONVERTERS:
            raise ValueError(f"Cannot override built-in converter '{name}'. Use a different name.")
        if not isinstance(regex, str) or not regex:
            raise ValueError("Converter regex must be a non-empty string")
        if not callable(caster):
            raise TypeError("Converter caster must be callable")
        self._converters[name] = (regex, caster)

    def get(self, name: str) -> Tuple[str, Callable[[str], Any]]:
        try:
            return self._converters[name]
        except KeyError:
            raise ValueError(
                f"Unknown path converter ':{name}'. "
                f"Available converters: {sorted(self._converters.keys())}"
            )

    def available(self) -> List[str]:
        return sorted(self._converters.keys())


_CONVERTER_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

if sys.platform == "win32":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
else:
    import uvloop
    loop = uvloop.new_event_loop()
    asyncio.set_event_loop(loop)

def _decode_header_name(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return v.decode("latin-1")
    return str(v)


def _decode_header_value(v: Any) -> str:
    # value may be bytes, str, list of bytes/str, or a single bare scalar
    if isinstance(v, (list, tuple)):
        v = v[0] if v else b""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("latin-1")
    return str(v)


def _parse_cookie_header(raw: str) -> Dict[str, str]:
    """Parse a ``Cookie`` header into a flat dict.

    """
    if not raw:
        return {}
    out: Dict[str, str] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            out[name] = value
    return out


def _strip_port(addr: str) -> str:
    """Strip ``:port`` from an ``"host:port"`` string, leaving IPv6 intact.

    Examples:
      ``"127.0.0.1:60050"`` → ``"127.0.0.1"``
      ``"[::1]:5000"``      → ``"[::1]"``
      ``"::1"``             → ``"::1"``
    """
    if not addr:
        return ""
    # bracketed IPv6, optionally followed by :port
    if addr.startswith("["):
        end = addr.find("]")
        if end != -1:
            return addr[: end + 1]
        return addr
    # plain host:port — only the LAST colon is the port separator for
    # IPv4 and unbracketed hostnames (no port on plain IPv6).
    if addr.count(":") == 1:
        return addr.split(":", 1)[0]
    return addr


def _client_address(client: Any) -> str:
    if isinstance(client, (list, tuple)) and client:
        first = client[0]
        return _strip_port(first) if isinstance(first, str) else str(first)
    if isinstance(client, dict):
        return _strip_port(client.get("host", client.get("ip", "")))
    return _strip_port(str(client)) if client else ""


def _last_forwarded(value: str) -> str:
    parts = [part.strip() for part in (value or "").split(",") if part.strip()]
    return parts[-1] if parts else ""


def _normalize_headers(raw: Any) -> Dict[str, str]:
    """Coerce any of the known header shapes to ``{lower(str): str}``.

    Tolerated:
      * ASGI 3.0 iterable of ``(bytes, bytes)`` 2-tuples
      * Some ASGI/proxy variants: 3-tuples ``(bytes, bytes, bytes)``
      * Pre-decoded iterable of ``(str, str)`` pairs
      * RSGI dicts: ``{bytes: bytes}`` or ``{bytes: [bytes, ...]}``
      * Pre-decoded dicts: ``{str: str}`` or ``{str: list[str]}``
      * Header-name-only iterables (some PyO3 bindings): values fetched
        via ``.get``/``[]`` if available, otherwise skipped silently.

    Never raises: unknown entries are skipped so a malformed header line
    can never 500 a request just because of a single bad pair.
    """
    if raw is None:
        return {}

    out: Dict[str, str] = {}

    # ------- Plain dict --------------------------------------------------
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[_decode_header_name(k).lower()] = _decode_header_value(v)
        return out

    # ------- Pair-shaped iterable (ASGI / typed lists) ------------------
    # Try to introspect the first entry cheaply.
    try:
        first = next(iter(raw))
    except StopIteration:
        return {}
    except TypeError:
        first = None

    if first is None:
        return {}

    # Single scalar (bytes or str) per entry → header-name-only iterable.
    if isinstance(first, (bytes, bytearray, str)):
        # Try to fetch each value via .get / [] on the same object.
        def _fetch(name: Any) -> Any:
            getter = getattr(raw, "get", None)
            if getter is not None:
                try:
                    return getter(name)
                except Exception:
                    pass
            try:
                return raw[name]
            except Exception:
                # Last resort: iterate and find a matching name.
                for k, v in raw.items() if hasattr(raw, "items") else []:
                    if k == name:
                        return v
            return None

        for name in raw:
            out[_decode_header_name(name).lower()] = _decode_header_value(_fetch(name))
        return out

    # Pair-shaped entry (tuple/list).
    for entry in raw:
        if isinstance(entry, (list, tuple)):
            if len(entry) < 2:
                continue
            k, v = entry[0], entry[1]
            out[_decode_header_name(k).lower()] = _decode_header_value(v)
        elif isinstance(entry, dict):
            # Single-key dicts occasionally show up; flatten them.
            for k, v in entry.items():
                out[_decode_header_name(k).lower()] = _decode_header_value(v)
        else:
            # Skip silently — we already accepted the shape, so a stray bad
            # entry should never 500 the request.
            continue

    return out


_MIME_OVERRIDES = {
    "html": "text/html", "htm": "text/html", "txt": "text/plain",
    "text": "text/plain", "json": "application/json", "xml": "application/xml",
    "js": "text/javascript", "css": "text/css", "form": "application/x-www-form-urlencoded",
    "urlencoded": "application/x-www-form-urlencoded", "multipart": "multipart/form-data",
    "bin": "application/octet-stream",
}


@functools.lru_cache(maxsize=128)
def _mime_for(token: str) -> str:
    token = str(token).strip().lower()
    if "/" in token:
        return token
    if token.startswith("."):
        token = token[1:]
    return _MIME_OVERRIDES.get(token) or mimetypes.guess_type("x." + token)[0] or token


def _parse_accept(header: str):
    parsed = []
    for index, part in enumerate(header.split(",")):
        sections = part.strip().split(";")
        media = sections[0].strip().lower()
        if not media:
            continue
        quality = 1.0
        for section in sections[1:]:
            key, _, value = section.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality > 0:
            parsed.append((media, quality, index))
    parsed.sort(key=lambda item: (-item[1], item[2]))
    return [(media, quality) for media, quality, _ in parsed]


def _media_matches(accepted: str, offered: str) -> bool:
    accepted_type, _, accepted_sub = accepted.partition("/")
    offered_type, _, offered_sub = offered.partition("/")
    if not offered_sub:
        return False
    if accepted_type == "*" and accepted_sub == "*":
        return True
    return accepted_type == offered_type and accepted_sub in ("*", offered_sub)


def _response_header(res, name: str) -> str:
    if res is None:
        return ""
    key = name.lower()
    header_map = getattr(res, "_header_map", None)
    if header_map:
        value = header_map.get(key.encode())
        if isinstance(value, (list, tuple)):
            value = value[0] if value else b""
        if isinstance(value, (bytes, bytearray)):
            return value.decode("latin-1")
        if value is not None:
            return str(value)
    headers = getattr(res, "_hdr", None)
    if headers:
        for header_name, header_value in headers:
            if str(header_name).lower() == key:
                return str(header_value)
    return ""


def _is_fresh(req_headers, res) -> bool:
    if_none_match = req_headers.get("if-none-match")
    if_modified_since = req_headers.get("if-modified-since")
    if not if_none_match and not if_modified_since:
        return False
    cache_control = req_headers.get("cache-control", "")
    if cache_control and re.search(r"(?:^|,)\s*no-cache\s*(?:,|$)", cache_control, re.I):
        return False

    if if_none_match and if_none_match.strip() != "*":
        etag = _response_header(res, "etag")
        if not etag:
            return False

        def _strip_weak(tag: str) -> str:
            tag = tag.strip()
            return tag[2:] if tag.startswith("W/") else tag

        expected = _strip_weak(etag)
        if not any(_strip_weak(tag) == expected for tag in if_none_match.split(",")):
            return False

    if if_modified_since:
        last_modified = _response_header(res, "last-modified")
        if not last_modified:
            return False
        try:
            modified_at = email.utils.parsedate_to_datetime(last_modified)
            modified_since = email.utils.parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError, OverflowError):
            return False
        if modified_at is None or modified_since is None:
            return False
        if modified_at.tzinfo is None:
            modified_at = modified_at.replace(tzinfo=timezone.utc)
        if modified_since.tzinfo is None:
            modified_since = modified_since.replace(tzinfo=timezone.utc)
        if modified_at > modified_since:
            return False

    return True


class _RangeSet(list):
    __slots__ = ("type",)


def _parse_range_header(header: str, size: int):
    if not header:
        return None
    unit, _, specs = header.strip().partition("=")
    if unit.strip().lower() != "bytes":
        return -2
    try:
        size = int(size)
    except (TypeError, ValueError):
        return -2
    if size <= 0:
        return -1

    ranges = _RangeSet()
    ranges.type = "bytes"
    malformed = False
    for spec in specs.split(","):
        spec = spec.strip()
        if not spec or "-" not in spec:
            malformed = True
            continue
        start_text, _, end_text = spec.partition("-")
        try:
            if start_text == "":
                if end_text == "":
                    malformed = True
                    continue
                suffix = int(end_text)
                if suffix <= 0:
                    continue
                start, end = max(size - suffix, 0), size - 1
            else:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
                if start > end or start >= size:
                    continue
                end = min(end, size - 1)
        except ValueError:
            malformed = True
            continue
        ranges.append({"start": start, "end": end})

    if not ranges:
        return -1 if not malformed else -2
    return ranges


# Filename-like patterns the browser uses to encode a `<input type=file>`
# field's path in urlencoded / text/plain submissions. We rely on this
# to lift such fields onto `req.files` so a single handler can serve
# multipart + urlencoded without branching. `C:\fakepath\avatar.png`
# is what Chrome/Edge ship; Firefox uses `/home/user/avatar.png`;
# Safari uses just the basename. We accept all three.
_FILE_PATH_HINT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/|\\\\[A-Za-z])"     # drive letter, /, or UNC start
    r"|"
    r"\.[A-Za-z0-9]{2,5}$"                  # bare basename with extension
)


def _looks_like_file_path(value):
    """Best-effort heuristic: is this string a likely file path the
    browser shipped for a `<input type=file>` field?
    """
    if not isinstance(value, str) or not value:
        return False
    return bool(_FILE_PATH_HINT_RE.search(value))


def _extract_files_from_form_fields(fields):
    """Split a flat urlencoded-style field dict into
    ``(text_fields, file_stubs)``.

    A field whose value looks like a file path (see
    ``_looks_like_file_path``) is moved to ``file_stubs`` as a
    ``File`` with empty ``content`` and the basename in
    ``filename``. The original ``fields`` dict is mutated to remove
    those keys.

    This lets a single handler deal with multipart (real bytes) and
    urlencoded (just paths) uniformly: ``files['photo'].filename``
    works in both cases, and ``files['photo'].content is None``
    distinguishes "user attached but did not upload bytes" from
    "user never opened the file picker".
    """
    from .flameo import File
    text_fields = {}
    file_stubs = {}
    for name, value in fields.items():
        if _looks_like_file_path(value):
            filename = re.split(r"[\\/]", value)[-1] or value
            file_stubs[name] = File(
                file_content=b"",
                filename=filename,
                content_type=None,
                field_name=name,
            )
        else:
            text_fields[name] = value
    return text_fields, file_stubs

_PARAM_SEGMENT_RE = re.compile(
    r"^(?P<opener>:|\{|<)(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<conv>[A-Za-z_][A-Za-z0-9_]*))?(?P<closer>[}>]?)$"
)
_BARE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_param_segment(segment: str, *, path: str, label: str, allow_bare: bool = False):
    """Parse ``:name``, ``{name}`` or ``{name:type}`` into ``(name, converter)``.

    ``allow_bare`` additionally accepts a plain ``name`` (wildcard segments
    without a delimiter), defaulting to the ``str`` converter. Angle brackets
    and a type suffix on ``:name`` are rejected with a message pointing at
    the supported forms.
    """
    if allow_bare and _BARE_NAME_RE.match(segment):
        return segment, None

    m = _PARAM_SEGMENT_RE.match(segment)
    if not m:
        raise ValueError(
            f"Invalid {label} segment '{segment}' in path '{path}'; "
            "expected ':name' or '{name}' / '{name:type}'."
        )
    opener, closer = m.group("opener"), m.group("closer")
    if opener == "<":
        raise ValueError(
            f"Unsupported {label} segment '{segment}' in path '{path}': "
            "angle brackets are not supported; use '{name}' or "
            "'{name:type}' instead."
        )
    if opener == ":":
        if m.group("conv") is not None or closer:
            raise ValueError(
                f"Unsupported {label} segment '{segment}' in path '{path}': "
                "':name' accepts no type; use '{name:type}' instead."
            )
    elif closer != "}":
        raise ValueError(
            f"Invalid {label} segment '{segment}' in path '{path}'; "
            "expected '{name}' or '{name:type}'."
        )
    return m.group("name"), m.group("conv")


class RouterMap:
    __slots__ = (
        "static_routes", "dynamic_routes", "_dynamic_by_first_segment",
        "_dynamic_wildcards", "_cache", "converters",
    )

    def __init__(self, converters: Optional[ConverterRegistry] = None) -> None:
        self.static_routes: Dict[str, Dict[str, Tuple[Any, List[Any]]]] = defaultdict(dict)
        self.dynamic_routes: Dict[
            str, List[Tuple[re.Pattern, List[Tuple[str, Callable[[str], Any]]], Any, List[Any]]]
        ] = defaultdict(list)
        # Dynamic routes with a literal first path segment can be selected
        # from a small candidate list rather than scanning every regex for
        # the method. Wildcard-first routes remain in a fallback list; both
        # lists store registration indexes so precedence stays unchanged.
        self._dynamic_by_first_segment: Dict[str, Dict[str, List[int]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._dynamic_wildcards: Dict[str, List[int]] = defaultdict(list)
        # OrderedDict so ``match`` can mark hits as most-recently-used,
        # keeping hot routes / 404 paths alive under bursty load.
        # Plain dict would evict FIFO, which lets cold 404s knock out
        # the popular static route that already cost a regex to find.
        self._cache: "OrderedDict[Tuple[str, str], Tuple[Any, DotDict, List[Any]]]" = OrderedDict()
        self.converters: ConverterRegistry = converters or ConverterRegistry()

    def register_converter(self, name: str, regex: str, caster: Callable[[str], Any]) -> None:
        self.converters.register(name, regex, caster)
        self._cache.clear()

    def _compile_path(self, path: str):
        """Parse `path` and return (regex_pattern, [(name, caster), ...]).

        Supports:
          :name           -> str (default)
          {name}          -> str (default)
          {name:type}     -> typed
          *name           -> wildcard, str (single segment)
          *{name:type}    -> wildcard, typed (captures the rest of the URL)

        ``:name:type`` and any ``<...>`` form are rejected with ValueError.
        """
        params: List[Tuple[str, Callable[[str], Any]]] = []
        segments = path.strip("/").split("/")

        for idx, segment in enumerate(segments):
            wildcard = segment.startswith("*")

            if wildcard:
                name, conv_name = _parse_param_segment(
                    segment[1:], path=path, label="wildcard", allow_bare=True
                )
                regex_pat, caster = self.converters.get(conv_name or "str")
                params.append((name, caster))
                prefix = "/".join(re.escape(s) for s in segments[:idx])
                prefix = ("/" + prefix) if prefix else ""
                pattern = f"^{prefix}/(?P<{name}>{regex_pat})$"
                return re.compile(pattern), params

            if not segment:
                continue

            if segment[0] in (":", "{", "<"):
                name, conv_name = _parse_param_segment(
                    segment, path=path, label="param"
                )
                regex_pat, caster = self.converters.get(conv_name or "str")
                params.append((name, caster))
                segments[idx] = f"(?P<{name}>{regex_pat})"
            else:
                segments[idx] = re.escape(segment)

        pattern = "^/" + "/".join(segments) + "$"
        return re.compile(pattern), params

    def add_route(self, method: str, path: str, handler, middlewares=None):
        method = method.upper()
        middlewares = middlewares or []

        has_param = (":" in path or "{" in path or "<" in path or "*" in path)
        if not has_param:
            self.static_routes[method][path] = (handler, middlewares)
            self._cache.clear()
            return

        compiled, params = self._compile_path(path)
        routes = self.dynamic_routes[method]
        route_index = len(routes)
        routes.append((compiled, params, handler, middlewares))
        first_segment = path.strip("/").split("/", 1)[0]
        if first_segment and not first_segment.startswith((":", "{", "*")):
            self._dynamic_by_first_segment[method][first_segment].append(route_index)
        else:
            self._dynamic_wildcards[method].append(route_index)
        self._cache.clear()

    def match(self, method: str, path: str):
        # Hot path: HTTP methods are already upper-case in the wire
        # protocol, so we skip `.upper()` here. add_route stores keys
        # upper-cased; call sites that pass lower-case methods should
        # normalize them at registration.
        key = (method, path)

        # Use the dict's `in` check (not `.get` is-None) because we
        # cache *misses* as `(None, DotDict(), [])` — a falsy tuple.
        cache = self._cache
        if key in cache:
            # LRU touch: bump this entry to the most-recently-used end
            # so a popular route stays cached even under churn.
            cache.move_to_end(key)
            return cache[key]

        static_table = self.static_routes.get(method)
        if static_table:
            found = static_table.get(path)
            if found:
                handler, mws = found
                result = handler, DotDict(), mws
                self._cache_result(key, result)
                return result

        routes = self.dynamic_routes.get(method, ())
        first_segment = path.lstrip("/").split("/", 1)[0]
        specific = self._dynamic_by_first_segment.get(method, {}).get(first_segment, ())
        wildcard = self._dynamic_wildcards.get(method, ())
        if specific and wildcard:
            candidate_indexes = heapq.merge(specific, wildcard)
        elif specific:
            candidate_indexes = specific
        else:
            candidate_indexes = wildcard

        for route_index in candidate_indexes:
            regex, params, handler, mws = routes[route_index]
            m = regex.match(path)
            if m:
                raw = m.groupdict()
                casted: Dict[str, Any] = {}
                for name, caster in params:
                    try:
                        casted[name] = caster(raw[name])
                    except (ValueError, TypeError):
                        # type-conversion failure -> treat as no match
                        casted = None
                        break
                if casted is None:
                    continue
                result = handler, DotDict(casted), mws
                self._cache_result(key, result)
                return result

        result = None, DotDict(), []
        self._cache_result(key, result)
        return result

    def _cache_result(self, key: Tuple[str, str], result: Tuple[Any, DotDict, List[Any]]):
        # OrderedDict is insertion-ordered; ``popitem(last=False)`` evicts
        # the least-recently-used entry (oldest inserted / coldest after
        # the move_to_end touches in ``match``).
        if len(self._cache) >= 2048:
            self._cache.popitem(last=False)
        self._cache[key] = result

class Request:
    """ASGI / RSGI request.

    Body access -- ``await req.body`` and ``await req.files`` -- share a
    single parse: the first access drains the request stream and parses
    according to its ``Content-Type``; the second access is O(1).

      * ``req.body``  returns the **fields** dict for form content-types
        (json / urlencoded / multipart / text-plain) and the raw bytes
        for anything else.
      * ``req.files`` returns the **files** dict for the same set of
        content-types. Values are :class:`flameo.flameo.File`
        instances (or a list of those for repeated field names). For
        non-multipart requests the dict is empty.

        Example::

            files = await req.files  # dict: field_name -> File
            photo = files.get("photo")
            if photo is not None:
                data = photo.content           # bytes
                name = photo.filename          # str sent by the browser
                mime = photo.content_type      # MIME declared in the part
                await photo.upload_to("uploads")

        Note: ``files[*]`` items are ``File`` **objects** (with
        attribute access to ``.content`` / ``.filename`` / ``.content_type``),
        not plain dicts shaped like ``{filename, content}``. Access the
        bytes via ``files["photo"].content``.

    The ``headers / query / cookies / url / originalUrl / protocol /
    hostname / ip / secure`` properties are computed on first access and
    cached, so a tiny request that only reads ``req.body`` never pays
    for URL parsing.
    """
    # Slots only cover the cheap, always-needed fields and the lazy-cache.
    __slots__ = (
        "scope", "receive",
        "method", "path", "params",
        "_query_raw", "_body", "_cache", "_files", "_lazy", "_max_body",
        "_recv_is_callable", "_trust_ip", "_peer_ip",
        "_upload_files",
        # Set by the session middleware (``app.use_session``) when
        # the request hits a route. Kept in a slot so the per-request
        # cost is one pointer write, not a __dict__ allocation.
        "session",
        # The Flameo app instance this request belongs to. Set by
        # ``__call__`` so handlers that take ``app`` as a parameter
        # receive the framework app, never a body field named ``app``.
        "_app",
        # Route-scoped template environment. Set by the dispatcher when
        # the matched route registered its own ``filters=``; ``None``
        # means the route renders through the app-wide environment.
        "_template_env",
        "_db_pool", "_db_acquire_context", "_db_query_manager",
        # Response back-ref (used by req.fresh / req.stale) and the
        # mount prefix exposed as req.baseUrl.
        "_res", "_base_url",
    )

    # Properties that route through the lazy cache ----------------------------
    _LAZY_PROPS = frozenset({
        "headers", "query", "cookies",
        "url", "originalUrl", "protocol", "hostname", "ip", "secure",
    })

    def __init__(self, scope: Dict[str, Any], receive: Any, max_body_size: int,
                 trust_ip=()) -> None:
        # Hot path: avoid allocating anything we don't need for `/hello`.
        self.scope = scope
        self.receive = receive
        self._max_body = max_body_size
        self._trust_ip = frozenset(trust_ip)
        client = getattr(scope, "client", None) if hasattr(scope, "method") else scope.get("client")
        self._peer_ip = _client_address(client)

        # ---- scope shape: dict (ASGI) or object (RSGI) ------------------
        # HTTP methods are already upper-case by RFC 9110 §9; skip .upper()
        # on the request path.
        if hasattr(scope, "method"):
            self.method = scope.method
            self.path = scope.path
            query_raw = getattr(scope, "query_string", b"") or b""
        else:
            self.method = scope["method"]
            self.path = scope["path"]
            query_raw = scope.get("query_string", b"") or b""

        # Path params (populated by router match, exposed as dict).
        self.params: Dict[str, Any] = {}

        # Raw query bytes are cheap to keep; the parsed dict is lazy.
        if isinstance(query_raw, (bytes, bytearray)):
            self._query_raw: bytes = bytes(query_raw)
        else:
            self._query_raw = str(query_raw).encode()

        # Lazy cache for derived properties — only allocated on first
        # access so a handler that never reads headers/query/etc. (the
        # `/hello` shape) doesn't pay for the dict construction.
        self._lazy: Dict[str, Any] | None = None

        # Body-related — _body / _cache are read on every lazy body
        # access, _files is only touched by multipart handlers.
        self._body: bytes | None = None
        self._cache: Any = None
        self._files = _FILES_SENTINEL  # populated lazily by `files` property
        self._upload_files = None
        # ASGI's ``receive`` is a callable; RSGI's is a Granian-style
        # proto that is itself awaitable but not callable. Cached here
        # so ``_load``'s inner loop stays branchless on the hot path.
        # ``None`` means "not yet probed"; first ``_load`` resolves it.
        self._recv_is_callable: Optional[bool] = None

        # Session middleware installs a dict here. Pre-set to None so
        # handlers that probe ``req.session`` before any session
        # middleware ran get an explicit ``None`` (vs. AttributeError),
        # making accidental misses easy to diagnose.
        self.session = None
        # The Flameo app instance this request belongs to. Set by
        # ``Flameo.__call__`` (and the RSGI hot path) so handlers
        # can read ``req.app`` at any time, including before body parsing.
        # Stays ``None`` on the rare test path that constructs a Request
        # outside a server context.
        self._app = None

        # Route-scoped template environment, set by the dispatcher when
        # the matched route registered its own ``filters=``. ``None``
        # means "use the app-wide environment".
        self._template_env = None
        self._db_pool = None
        self._db_acquire_context = None
        self._db_query_manager = None
        self._res = None
        self._base_url = ""

    # -------- lazy attribute access ------------------------------------
    @property
    def app(self):
        return self._app

    def __getattr__(self, name: str) -> Any:
        if name not in self._LAZY_PROPS:
            raise AttributeError(name)

        lazy = self._lazy
        if lazy is None:
            lazy = self._lazy = {}
        cached = lazy.get(name, _MISSING)
        if cached is not _MISSING:
            return cached
        value = self._compute(name)
        lazy[name] = value
        return value

    def _trusts_forwarded(self) -> bool:
        if not self._trust_ip:
            return False
        return self._peer_ip.strip("[]") in self._trust_ip

    # -------- per-property computation ---------------------------------
    def _compute(self, name: str) -> Any:
        scope = self.scope
        if hasattr(scope, "method"):  # RSGI
            raw_headers = getattr(scope, "headers", {}) or []
            scheme = getattr(scope, "scheme", "http")
            server = getattr(scope, "server", None)
            client = getattr(scope, "client", None)
        else:
            raw_headers = scope.get("headers", []) or []
            scheme = scope.get("scheme", scope.get("type", "http"))
            server = scope.get("server", None)
            client = scope.get("client", None)

        if name == "headers":
            return _normalize_headers(raw_headers)

        if name == "query":
            qs = self._query_raw.decode("latin-1")
            parsed = parse_qs(qs, keep_blank_values=True)
            for k, v in list(parsed.items()):
                if isinstance(v, list) and len(v) == 1:
                    parsed[k] = v[0]
            return parsed

        if name == "cookies":
            headers = self.headers  # ensures headers are computed first
            return _parse_cookie_header(headers.get("cookie", ""))

        if name == "originalUrl":
            # Cheap: just stitch path + query. No header work needed.
            if self._query_raw:
                return f"{self.path}?{self._query_raw.decode('latin-1')}"
            return self.path

        # protocol / secure / hostname / ip / url all depend on the URL
        # parts — compute them lazily so requests that never ask for
        # these properties skip the entire header-decode + URL rebuild.
        protocol, hostname, ip, _host, port = self._url_parts(scheme, server, client)

        if name == "protocol":
            return protocol

        if name == "secure":
            return protocol == "https"

        if name == "hostname":
            return hostname

        if name == "ip":
            return ip

        if name == "url":
            # Need hostname/protocol/port — fall through to _url_parts
            # which is cached so repeat access is O(1).
            original = self.originalUrl
            port_part = ""
            if port and not (
                (protocol == "http" and port == 80) or (protocol == "https" and port == 443)
            ):
                port_part = f":{port}"
            host_part = f"{hostname}{port_part}" if hostname else ""
            if host_part:
                return f"{protocol}://{host_part}{original}"
            return f"{protocol}://{original}"

        raise AttributeError(name)

    def _url_parts(self, scheme, server, client):
        """Return (protocol, hostname, ip, host, port), honoring Host header
        and X-Forwarded-* headers. Cached as `protocol/hostname/ip` slots
        so repeat access is free."""
        # If we've already cached the URL triplet, just pull them. The
        # cache is only created on first lazy access (see __getattr__),
        # so requests that never touch hostname/protocol/ip skip the
        # entire header-decode + URL-rebuild cost.
        lazy = self._lazy
        if lazy is not None and "protocol" in lazy:
            return (
                lazy["protocol"], lazy["hostname"], lazy["ip"],
                lazy.get("_host", ""), lazy.get("_port", 0),
            )

        host = ""
        port = 0
        if isinstance(server, (list, tuple)) and len(server) >= 2:
            host, port = server[0], server[1]
        elif isinstance(server, dict):
            host = server.get("host", server.get("hostname", ""))
            port = server.get("port", 0)

        # Headers may not have been computed yet; forcing them here
        # decodes every ASGI header pair. Only do this when something
        # has actually asked for hostname/protocol/ip (i.e. we got here
        # via __getattr__), which is exactly when the cost is justified.
        headers = self.headers
        host_header = headers.get("host", "")
        if host_header:
            host = _strip_port(host_header) or host
            if host_header.startswith("["):
                end = host_header.find("]")
                port_part = host_header[end + 1:].lstrip(":") if end != -1 else ""
            elif host_header.count(":") == 1:
                port_part = host_header.rsplit(":", 1)[1]
            else:
                port_part = ""
            if port_part:
                try:
                    port = int(port_part) or port
                except ValueError:
                    pass

        forwarded_proto = forwarded_for = None
        if self._trusts_forwarded():
            forwarded_proto = headers.get("x-forwarded-proto")
            forwarded_for = headers.get("x-forwarded-for")
            if forwarded_proto:
                scheme = _last_forwarded(forwarded_proto).lower()

        protocol = "https" if scheme == "https" else "http"
        hostname = host or ""

        if forwarded_for:
            ip = _last_forwarded(forwarded_for)
        elif client:
            ip = _client_address(client)
        else:
            ip = ""

        # Cache so subsequent calls are O(1).
        if lazy is None:
            lazy = self._lazy = {}
        lazy["protocol"] = protocol
        lazy["hostname"] = hostname
        lazy["ip"] = ip
        lazy["_host"] = host
        lazy["_port"] = port

        return protocol, hostname, ip, host, port
    
    def parse_body(self):
        # Use the original (case-preserving) Content-Type for the multipart
        # parser because RFC 2046 boundaries are case-sensitive bytes that
        # must match exactly between the header and the body. Lowercase
        # only the prefix for the dispatch branch, never the whole string.
        ct = self.headers.get("content-type", "") or ""
        ct_prefix = ct.lower()
        if not self._body:
            return {}, {}
        elif ct_prefix.startswith("application/json"):
            # RFC 8259 §8.1 says implementations MAY ignore a leading
            # BOM. orjson does NOT — it raises ``unexpected character``
            # at the BOM byte. Some JS runtimes (older IE, some shims)
            # and some Python HTTP clients DO prepend the UTF-8 BOM, so we
            # strip it here so callers don't have to care. The BOM is
            # exactly the three bytes ``EF BB BF``; nothing else is
            # valid in a JSON body before the first value.
            body_bytes = self._body
            if body_bytes.startswith(b"\xef\xbb\xbf"):
                body_bytes = body_bytes[3:]
            if not body_bytes:
                # BOM-only body (or whitespace-only after BOM) is the
                # same as no body for JSON purposes — there is no value
                # to parse. Return an empty dict so handlers that read
                # ``req.body`` for JSON don't blow up.
                return {}, {}
            try:
                return orjson.loads(body_bytes.decode("utf-8")), {}
            except (orjson.JSONDecodeError, ValueError) as exc:
                # Translate orjson's error into a clear framework-level
                # ``ValueError`` so the dispatcher can catch it and
                # return a 400 instead of bubbling a raw ``orjson.JSONDecodeError``
                # all the way up to a 500 / disconnected socket. The original
                # message is preserved so callers / logs still see where in
                # the body the parse failed.
                raise ValueError(
                    f"Could not parse JSON request body: {exc}"
                ) from exc
        elif ct_prefix.startswith("multipart/form-data"):
            fields, files = RequestParser.parse_multipart_body(ct, self._body)
            return {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in fields.items()}, files
        elif ct.startswith("application/x-www-form-urlencoded"):
            # The browser strips file bytes from urlencoded submissions
            # (per the HTML spec) and only sends the path the user
            # chose. To keep the handler side uniform -- so a single
            # handler can serve multipart + urlencoded without branching
            # -- we promote any field whose value *looks like* a file
            # path into a stub ``File`` on ``req.files``. The bytes
            # are empty; ``filename`` is the basename and ``content`` is
            # ``None`` so handlers can detect "no bytes uploaded" and
            # fall back gracefully.
            raw_fields = parse_qs(self._body.decode(), keep_blank_values=True)
            fields = {k: (v[0] if len(v) == 1 else v) for k, v in raw_fields.items()}
            fields, files = _extract_files_from_form_fields(fields)
            return fields, files
        elif ct.startswith("text/plain"):
            # Browsers serialise text/plain form fields as one per line
            # ``name=value``. Unlike urlencoded, ``&`` is not used as a
            # separator -- the body is a flat newline-separated stream.
            # We normalise it to ``name=value&...`` so ``parse_qs``
            # gives us the same flat dict shape as urlencoded.
            try:
                text_body = self._body.decode("utf-8")
            except UnicodeDecodeError:
                text_body = self._body.decode("utf-8", errors="replace")
            # Split on either ``\r\n`` or ``\n`` (Chrome uses CRLF, others LF).
            line_chunks = [
                line.strip()
                for line in re.split(r"[\r\n]+", text_body)
                if line.strip()
            ]
            # Heuristic: a text/plain body only counts as a form
            # submission when at least one line is shaped like
            # ``name=value``. Without this gate, anything sent as a
            # plain string via ``fetch(url, { body: JSON.stringify(...) })``
            # (the browser defaults that header to text/plain;charset=UTF-8)
            # gets mis-parsed as urlencoded -- parse_qs treats a key
            # with no ``=`` as a key with empty value, so the whole JSON
            # string becomes the single key. A raw string body should
            # pass through as text instead.
            looks_like_form = any(
                re.match(r"^[A-Za-z0-9_\-]+=", line) for line in line_chunks
            )
            if not looks_like_form:
                from .flameo import File
                return (
                    {"body": text_body},
                    {"body": File(
                        file_content=self._body,
                        filename=None,
                        content_type="text/plain",
                        field_name="body",
                    )},
                )
            joined = "&".join(line_chunks)
            raw_fields = parse_qs(joined, keep_blank_values=True)
            fields = {k: (v[0] if len(v) == 1 else v) for k, v in raw_fields.items()}
            fields, files = _extract_files_from_form_fields(fields)
            # Surface the whole body as a single ``File`` on
            # ``files['body']`` so handlers can read the raw text via
            # ``files['body'].content``.
            from .flameo import File
            files["body"] = File(
                file_content=self._body,
                filename=None,
                content_type="text/plain",
                field_name="body",
            )
            return fields, files
        else:
            return self._body.decode(), {}

    async def _load(self) -> None:
        if self._body is not None:
            return

        # Cross-check Content-Length against the actual bytes received so a
        # client claiming a small body but streaming a larger one is rejected.
        # Default cap is 10 MiB; per-app override via App.max_body_size.
        max_body = self._max_body
        
        # Streaming path: multipart bodies are written part-by-part to
        # ``./temp`` while network chunks arrive. The full body is never
        # materialised in RAM -- a 5 GB upload flows receive() -> temp file
        # -> os.replace(destination) without ever sitting in a contiguous
        # bytes blob. Non-multipart bodies stay buffered below (they're
        # typically tiny: JSON, urlencoded, text).
        ct = self.headers.get("content-type", "") or ""
        if ct.lower().startswith("multipart/form-data"):
            async def _chunks():
                running = 0
                while True:
                    try:
                        # Same ASGI/RSGI dispatch as the buffered path.
                        if self._recv_is_callable is None:
                            self._recv_is_callable = callable(self.receive)
                        if self._recv_is_callable:
                            msg = await self.receive()
                        else:
                            msg = await self.receive
                    except Exception:
                        return
                    if isinstance(msg, dict):
                        if msg.get("type") != "http.request":
                            continue
                        chunk = msg.get("body", b"") or b""
                        more = bool(msg.get("more_body", False))
                    elif isinstance(msg, (bytes, bytearray)):
                        chunk = bytes(msg)
                        # RSGI: empty bytes is the implicit EOF sentinel.
                        if not chunk:
                            return
                        more = True
                    else:
                        raise RuntimeError(f"Unexpected receive() type: {type(msg)}")
                    # Per-chunk + cumulative cap; same rules as the
                    # buffered path so behaviour is identical from the
                    # handler's perspective.
                    if len(chunk) > max_body:
                        raise ValueError(
                            f"Request body exceeds maximum size of {max_body} bytes"
                        )
                    running += len(chunk)
                    if running > max_body:
                        raise ValueError(
                            f"Request body exceeds maximum size of {max_body} bytes"
                        )
                    yield chunk
                    if not more:
                        return

            fields, files, _temp_paths = await parse_multipart_body_streaming(ct, _chunks())
            
            self._cache = {k: (v[0] if isinstance(v, list) and len(v) == 1 else v)
                           for k, v in fields.items()}
            self._files = files
            self._register_uploads(files)
            # Sentinel so a second ``await req.body`` is a no-op without
            # re-running the parser or re-driving the receive callable.
            self._body = b""
            return

        # Buffered path: small JSON / urlencoded / text bodies.
        chunks: list[bytes] = []
        running_total = 0  # O(1) per-chunk size check (avoids sum-per-iter)
        while True:
            try:
                # ``self.receive`` is either an ASGI callable (returning a
                # coroutine) or an RSGI Granian-style proto (itself
                # awaitable but not callable). ``self.receive()`` would
                # raise ``TypeError`` on the RSGI shape and the bare
                # ``except`` below would silently swallow it, leaving the
                # body empty. Probe once per request and dispatch to the
                # correct shape.
                if self._recv_is_callable is None:
                    self._recv_is_callable = callable(self.receive)
                if self._recv_is_callable:
                    msg = await self.receive()
                else:
                    msg = await self.receive
            except:
                break

            if isinstance(msg, dict):
                if msg["type"] == "http.request":
                    chunk = msg.get("body", b"") or b""
                    # Per-chunk cap: a single chunk larger than the entire
                    # budget is rejected immediately rather than buffered.
                    if len(chunk) > max_body:
                        raise ValueError(
                            f"Request body exceeds maximum size of {max_body} bytes"
                        )
                    running_total += len(chunk)
                    # Cumulative cap across all chunks.
                    if running_total > max_body:
                        raise ValueError(
                            f"Request body exceeds maximum size of {max_body} bytes"
                        )
                    chunks.append(chunk)
                    if not msg.get("more_body", False):
                        break
            elif isinstance(msg, (bytes, bytearray)):
                chunk = msg or b""
                if not chunk:
                    # RSGI: empty bytes is the implicit EOF sentinel
                    # (same rule the multipart streaming path applies).
                    break
                if len(chunk) > max_body:
                    raise ValueError(
                        f"Request body exceeds maximum size of {max_body} bytes"
                    )
                running_total += len(chunk)
                chunks.append(chunk)

            else:
                raise RuntimeError(f"Unexpected receive() type: {type(msg)}")

        body = b"".join(chunks)
        # Final hard cap — defense in depth if a chunk somehow slipped past.
        if len(body) > max_body:
            raise ValueError(
                f"Request body exceeds maximum size of {max_body} bytes"
            )
        self._body = body
        if len(self._body) > MIN_BODY_TO_THREAD:
            self._cache, self._files = await asyncio.to_thread(self.parse_body)
        else:
            self._cache, self._files = self.parse_body()
        self._register_uploads(self._files)

    
    @property
    async def body(self) -> Any:
        if self._body is None:
            await self._load()
        return self._cache
    
    @property
    async def files(self):
        if self._body is None:
            await self._load()
        return self._files

    def _register_uploads(self, files):
        from .flameo import File

        if not files:
            return
        uploads = []
        for value in files.values():
            if isinstance(value, File):
                uploads.append(value)
            elif isinstance(value, (list, tuple)):
                uploads.extend(item for item in value if isinstance(item, File))
        if not uploads:
            return

        if self._upload_files is None:
            self._upload_files = []
        tracker = self._upload_files
        for uploaded in uploads:
            if uploaded not in tracker:
                tracker.append(uploaded)
            uploaded._tracker = tracker

        res = self._res
        if res is not None:
            res.after(self._cleanup_uploads)

    async def _cleanup_uploads(self):
        uploads = self._upload_files
        if not uploads:
            self._upload_files = None
            return
        self._upload_files = None

        def _discard_all():
            for uploaded in uploads:
                try:
                    uploaded._discard()
                except Exception:
                    pass

        await asyncio.to_thread(_discard_all)

    # ----- Response metadata -------------------------------------------
    @property
    def host(self):
        return self.hostname

    @property
    def ips(self):
        if not self._trusts_forwarded():
            return []
        raw = self.headers.get("x-forwarded-for")
        if not raw:
            return []
        return [ip.strip() for ip in raw.split(",") if ip.strip()]

    @property
    def xhr(self):
        return self.headers.get("x-requested-with", "").lower() == "xmlhttprequest"

    @property
    def baseUrl(self):
        return self._base_url

    @property
    def base_url(self):
        return self._base_url

    @property
    def pathname(self):
        return self.path

    @property
    def original_url(self):
        return self.originalUrl

    @property
    def remote_addr(self):
        return self.ip

    @property
    def subdomains(self):
        hostname = self.hostname or ""
        if not hostname:
            return []
        parts = hostname.split(".")
        parts.reverse()
        return parts[2:]

    @property
    def fresh(self):
        return _is_fresh(self.headers, self._res)

    @property
    def stale(self):
        return not self.fresh

    def get(self, field):
        name = field.decode("latin-1") if isinstance(field, (bytes, bytearray)) else str(field)
        name = name.lower()
        if name == "referrer":
            name = "referer"
        return self.headers.get(name)

    header = get

    def accepts(self, *types):
        if len(types) == 1 and isinstance(types[0], (list, tuple)):
            types = tuple(types[0])
        parsed = _parse_accept(self.headers.get("accept", "*/*"))
        if not types:
            return [media for media, _ in parsed]
        wanted = [(original, _mime_for(original)) for original in types]
        for media, _ in parsed:
            for original, mime in wanted:
                if _media_matches(media, mime):
                    return original
        return False

    def is_(self, *types):
        content_type = self.headers.get("content-type", "")
        if not content_type:
            return None
        content_type = content_type.split(";", 1)[0].strip().lower()
        if not types:
            return content_type
        if len(types) == 1 and isinstance(types[0], (list, tuple)):
            types = tuple(types[0])
        for original in types:
            if _media_matches(_mime_for(original), content_type):
                return original
        return False

    is_type = is_

    def range(self, size, options=None):
        result = _parse_range_header(self.headers.get("range"), size)
        if options and options.get("combine") and isinstance(result, _RangeSet):
            result.sort(key=lambda item: item["start"])
            merged = _RangeSet()
            merged.type = "bytes"
            for item in result:
                if merged and item["start"] <= merged[-1]["end"] + 1:
                    merged[-1]["end"] = max(merged[-1]["end"], item["end"])
                else:
                    merged.append(dict(item))
            return merged
        return result


import asyncio as _asyncio

def _drain_finishers(res):
    """Pop every finisher registered via ``res.finish(coro)`` and
    schedule each one with ``asyncio.ensure_future``.

    Called by the response writer (after the response body has been
    handed to the wire) and by the dispatcher's ``finally`` block
    (handler-raise paths that never reached ``_send_body``). Never
    awaited -- a slow finisher cannot delay the client. Idempotent:
    calling twice is a no-op because we pop the list the first time.
    """
    finishers = res._finishers__
    if not finishers:
        return
    res._finishers__ = []
    for fn in finishers:
        try:
            result = fn()
        except Exception:
            # Sync callable raised before returning -- nothing to
            # schedule; move on.
            continue
        # Sync callable -> wrap in a coroutine so ensure_future has
        # something awaitable to schedule. Async callable already
        # returned a coroutine; pass it through.
        if not _asyncio.iscoroutine(result):
            async def _wrap(_r=result):
                return _r
            result = _wrap()
        try:
            _asyncio.ensure_future(result)
        except RuntimeError:
            pass


class Response:
    __slots__ = ("_sent", "_send", "_app", "_header_map",
                 "_sent_body", "_sent_body_size", "_status", "_sent_headers", "_sendFile",
                 "_download", "_chunks", "_wrote_head", "_ended", "_req",
                 "_streamed", "_streaming", "_response_started",
                 # Set by the session middleware (``app.use_session``)
                 # with the per-request cookie commit callable.
                 # Initialized to None in __init__ below.
                 "_finalize_session__",
                 # List of async cleanup callables registered via
                 # ``res.finish(coro)``. Drained with
                 # ``asyncio.ensure_future`` AFTER the response body
                 # has been handed to the wire -- never awaited, so
                 # a slow finisher cannot delay the client.
                 "_finishers__",
                 # Endpoint-response cache capture (``@app.cache``).
                 # The cached-dispatch wrapper sets a dict here before
                 # invoking the handler; ``_send_body`` fills it once
                 # with the raw (pre-compression) body + header state
                 # so the wrapper can store the entry. None otherwise.
                 "_cache_capture__")

    def __getattr__(self, name: str) -> Any:
        # Lazy `sendFile` builder: only built when a handler actually
        # accesses res.sendFile. Avoids the per-request closure cost on
        # the common /hello-style path.
        #
        # `sendFile` is NOT a slot (only `_sendFile` is), so every access
        # misses normal attribute lookup and lands here. We must therefore
        # short-circuit on the cached value ourselves — otherwise we'd
        # rebuild the closure on every call. (The slot is initialized to
        # None in __init__, so unbuilt == None is reliable.)
        if name == "sendFile":
            cached = self._sendFile
            if cached is not None:
                return cached
            app = object.__getattribute__(self, "_app")
            send_func = _make_send_file(app, self)
            object.__setattr__(self, "_sendFile", send_func)
            return send_func
        if name == "download":
            cached = object.__getattribute__(self, "_download")
            if cached is not None:
                return cached
            app = object.__getattribute__(self, "_app")
            download_func = _make_download(app, self)
            object.__setattr__(self, "_download", download_func)
            return download_func
        raise AttributeError(name)

    @property
    def req(self):
        return self._req

    def __init__(self, send, app=None, req=None):
        self._send = send
        self._app = app
        self._req = req  # used by res.jsonp() to read the callback name
        if req is not None:
            try:
                req._res = self
            except AttributeError:
                pass
        # Hot path: lazy header_map. The vast majority of /hello-style
        # responses never call setHeader/cookie, so we avoid the
        # `dict(base)` allocation (and the slot write) on the common
        # path. _send_body checks for None and short-circuits the
        # items() loop. setHeader/cookie materialize the dict on first
        # mutation.
        self._header_map = None
        # Streaming-mode flag. When True, ``write()`` emits each chunk
        # as its own ``http.response.body`` event instead of buffering
        # into ``self._chunks``. ``stream()`` flips this on, ``end()``
        # then emits the terminating empty body event. Kept separate
        # from ``_streamed`` (which just records that the start event
        # was already sent) so non-stream responses stay buffered.
        self._streaming = False
        self._sent = False
        self._sent_body = b""
        self._sent_body_size = 0
        self._status = 200
        self._sent_headers = ()  # populated only when request logging needs it
        # sentinel.
        self._sendFile = None
        self._download = None
        
        self._chunks: list[bytes] = []
        self._wrote_head = False
        self._ended = False
        # Streaming flag: True once ``stream()`` has emitted the
        # http.response.start event. ``end()`` reads this to decide
        # whether to close with a chunked-transfer terminator or fall
        # back to the buffered ``_send_body`` flush.
        self._streamed = False
        self._response_started = False

        # Session middleware hook — the ``app.use_session`` middleware
        # sets this to a callable that writes the signed session cookie
        # when invoked. Default None means no session middleware ran
        # for this request, so the dispatcher's ``finally`` block no-ops.
        self._finalize_session__ = None
        # Per-request async-cleanup list. ``res.finish(coro)`` appends;
        # the framework drains it with ``asyncio.ensure_future`` after
        # the response body has been handed to the wire (or, on
        # handler-raise paths, in the dispatcher's finally block).
        # Empty list = no per-request cleanup, so the cost is one
        # pointer per response on the common /hello path.
        self._finishers__ = []
        # Endpoint-response cache capture — see the ``_cache_capture__``
        # slot comment above. Only the cached-dispatch wrapper sets it.
        self._cache_capture__ = None

    # ----- res.finish(): per-request async cleanup ------------------------
    def finish(self, fn):
        """Register an async cleanup callable to run AFTER the response
        body has been handed to the wire.

        ``fn`` may be an async coroutine function or a plain sync
        callable. Each registered finisher is scheduled with
        ``asyncio.ensure_future`` -- nothing awaits it, so a slow
        finisher cannot delay the client.

        Multiple finishers may be registered on the same response --
        they run concurrently. A finisher that raises is silently
        dropped; the others still run.

        Both middlewares and route handlers may call ``res.finish()``,
        and either may register any number of finishers.
        """
        if not callable(fn):
            raise TypeError("res.finish() expects a callable")
        self._finishers__.append(fn)
        return self
        
    after = finish
    
    
    def status(self, code: int):
        """Set the response status code and return ``self`` for chaining.

        Example::

            await res.status(201).json({"created": True})
            await res.status(404).send("Not found")
        """
        if self._sent:
            return self
        if not isinstance(code, int) or not (100 <= code <= 599):
            raise ValueError(f"Invalid HTTP status code: {code!r}")
        self._status = code
        return self

    # ----- Chainable header ----------------------------------------------
    def setHeader(self, key, value):
        if self._header_map is None:
            self._header_map = dict(self._app._base_header_map)
        self._header_map[key.lower().encode()] = value.encode()
        # Auto-flip streaming mode when the caller sets an SSE-style
        # Content-Type. This makes the natural
        # ``setHeader("Content-Type", "text/event-stream") + write(...)``
        # idiom stream per-call without forcing the caller to also call
        # ``res.stream()``. Matches what EventSource clients expect.
        if (key.lower() == "content-type"
                and value.lower().startswith("text/event-stream")):
            self._streaming = True
        return self

    header = setHeader
    set = setHeader

    # ----- Chainable cookie ----------------------------------------------
    def cookie(self, name: str, value: str, options: dict = None, **kwargs):
        """Set a ``Set-Cookie`` header. Chainable.
        """
        if self._sent:
            return self
        opts = dict(options) if options else {}
        opts.update(kwargs)
        parts = [f"{name}={value}"]
        if "maxAge" in opts and opts["maxAge"] is not None:
            parts.append(f"Max-Age={int(opts['maxAge'])}")
        if opts.get("expires") is not None:
            parts.append(f"Expires={opts['expires']}")
        if opts.get("domain"):
            parts.append(f"Domain={opts['domain']}")
        path = opts.get("path", "/")
        parts.append(f"Path={path}")
        if opts.get("httpOnly"):
            parts.append("HttpOnly")
        if opts.get("secure"):
            parts.append("Secure")
        if opts.get("sameSite"):
            ss = str(opts["sameSite"])
            parts.append(f"SameSite={ss[0].upper()}{ss[1:].lower()}" if ss.lower() != "none" else "SameSite=None")
        if opts.get("partitioned"):
            parts.append("Partitioned")
        if opts.get("priority") is not None:
            pr = str(opts["priority"]).lower()
            if pr not in ("low", "medium", "high"):
                raise ValueError("priority must be one of 'low', 'medium', 'high'")
            parts.append(f"Priority={pr.capitalize()}")
        if self._header_map is None:
            self._header_map = dict(self._app._base_header_map)
        
        self._header_map[b"set-cookie"] = [("; ".join(parts)).encode()]
        return self

    def clearCookie(self, name: str, options: dict = None, **kwargs):
        """Expire a single cookie. Chainable.
        """
        if self._sent:
            return self
        merged = {**options} if options else {}
        merged.update(kwargs)
        path = merged.get("path", "/")
        parts = [f"{name}=", "Expires=Thu, 01 Jan 1970 00:00:00 GMT", f"Path={path}"]
        if merged.get("domain"):
            parts.append(f"Domain={merged['domain']}")
        if merged.get("httpOnly"):
            parts.append("HttpOnly")
        if merged.get("secure"):
            parts.append("Secure")
        if merged.get("sameSite"):
            ss = str(merged["sameSite"])
            parts.append(f"SameSite={ss[0].upper()}{ss[1:].lower()}" if ss.lower() != "none" else "SameSite=None")
        if merged.get("partitioned"):
            parts.append("Partitioned")
        if merged.get("priority") is not None:
            pr = str(merged["priority"]).lower()
            if pr not in ("low", "medium", "high"):
                raise ValueError("priority must be one of 'low', 'medium', 'high'")
            parts.append(f"Priority={pr.capitalize()}")
        chunk = ("; ".join(parts)).encode()
        if self._header_map is None:
            self._header_map = dict(self._app._base_header_map)
        # Always REPLACE any prior Set-Cookie entries, even ones written
        # via cookie(), so a clearCookie() call always wins.
        self._header_map[b"set-cookie"] = [chunk]
        return self

    def clearAllCookies(self, options: dict = None, **kwargs):
        """Expire every cookie the client sent. Chainable.

        ``options`` / ``**kwargs`` (e.g. ``path``, ``domain``) are
        forwarded for each expired cookie so the browser matches the
        original scope.
        """
        if self._sent:
            return self
        req = self._req
        names = list(req.cookies.keys()) if (req is not None and getattr(req, "cookies", None)) else []
        if not names:
            return self
        merged = {**options} if options else {}
        merged.update(kwargs)
        path = merged.get("path", "/")
        chunks = []
        for name in names:
            parts = [f"{name}=", "Expires=Thu, 01 Jan 1970 00:00:00 GMT", f"Path={path}"]
            if merged.get("domain"):
                parts.append(f"Domain={merged['domain']}")
            if merged.get("httpOnly"):
                parts.append("HttpOnly")
            if merged.get("secure"):
                parts.append("Secure")
            if merged.get("sameSite"):
                ss = str(merged["sameSite"])
                parts.append(f"SameSite={ss[0].upper()}{ss[1:].lower()}" if ss.lower() != "none" else "SameSite=None")
            if merged.get("partitioned"):
                parts.append("Partitioned")
            if merged.get("priority") is not None:
                pr = str(merged["priority"]).lower()
                if pr not in ("low", "medium", "high"):
                    raise ValueError("priority must be one of 'low', 'medium', 'high'")
                parts.append(f"Priority={pr.capitalize()}")
            chunks.append("; ".join(parts).encode())
        if self._header_map is None:
            self._header_map = dict(self._app._base_header_map)
        self._header_map[b"set-cookie"] = chunks
        return self

    # ----- Chainable redirect --------------------------------------------
    async def redirect(self, url: str, code: int = 302):
        """Send a redirect. Chainable (returns self)."""
        if self._sent:
            return self
        self.setHeader("Location", url)
        # status() returns self so we can chain, then send the empty body
        return await self.status(code).send("")

    async def jsonp(self, data, status: int = None):
        """Send a JSONP response.
        """
        if self._sent:
            return
        # Resolve the callback name from req.query (DotDict-like). When
        # we have no request handle (defensive), fall back to plain json.
        callback = None
        req = self._req
        if req is not None:
            try:
                q = req.query
            except Exception:
                q = None
            if q is not None:
                
                v = q.get("callback") if hasattr(q, "get") else q["callback"]
                if isinstance(v, list):
                    v = v[0] if v else None
                if isinstance(v, str) and _VALID_JSONP_NAME.match(v):
                    callback = v
        body = _orjson_dumps_fast(data)
        if callback:
            
            self.setHeader("X-Content-Type-Options", "nosniff")
            self.setHeader("Content-Type", "application/javascript; charset=utf-8")
            payload = b"/**/ typeof " + callback.encode() + b" === 'function' && " + callback.encode() + b"(" + body + b");"
            await self._send_body(payload, b"application/javascript; charset=utf-8",
                                  status if status is not None else self._status)
        else:
            # No callback in the query string — behave exactly like
            # res.json (still goes through the same gzip path).
            await self.json(data, status)

    
    async def write(self, chunk, encoding: str = "utf-8") -> bool:
        """Append a chunk to the response body. Returns ``True`` if
        written, ``False`` if the response has already ended.
        """
        if self._ended:
            return False
        if chunk is None:
            return True
        if isinstance(chunk, str):
            chunk = chunk.encode(encoding or "utf-8")
        elif not isinstance(chunk, (bytes, bytearray)):
            
            chunk = str(chunk).encode("utf-8")
        if not self._wrote_head:
            
            if self._header_map is None or b"content-type" not in self._header_map:
                self.setHeader("Content-Type", "text/plain; charset=utf-8")
            if self._header_map is None or b"x-content-type-options" not in self._header_map:
                self.setHeader("X-Content-Type-Options", "nosniff")
            self._wrote_head = True
        # Streaming mode: ``stream()`` or an SSE-style
        # ``setHeader("Content-Type", "text/event-stream")`` flipped
        # ``_streaming`` so each ``write()`` ships its own
        # ``http.response.body`` event with ``more_body=True`` instead
        # of being buffered until ``end()``. This makes the natural
        # ``setHeader(...) + write(chunk) + write(chunk)`` pattern
        # honour per-call delivery without forcing the caller to also
        # call ``res.stream()``.
        if self._streaming:
            # First streaming write: flush the http.response.start event
            # now (using the headers the caller has set so far) so the
            # client gets a 200 + headers + first body in one go,
            # mirroring what ``stream()`` does on its first call.
            if not self._streamed:
                headers = []
                if self._header_map is not None:
                    for k, v in self._header_map.items():
                        if isinstance(v, list):
                            headers.extend((k, item) for item in v)
                        else:
                            headers.append((k, v))
                send = self._send
                if send is not None:
                    try:
                        self._response_started = True
                        await send({
                            "type": "http.response.start",
                            "status": self._status,
                            "headers": headers,
                        })
                    except Exception:
                        self._ended = True
                        raise
                self._streamed = True
            send = self._send
            if send is None:
                self._chunks.append(bytes(chunk))
                return True
            await send({
                "type": "http.response.body",
                "body": bytes(chunk),
                "more_body": True,
            })
            return True
        self._chunks.append(bytes(chunk))
        return True

    async def end(self, chunk=None, encoding: str = "utf-8") -> None:
        if self._ended:
            return
        self._ended = True

        # Streaming close path: emit final chunk (if any) + terminator.
        if self._streamed:
            if chunk is not None:
                if isinstance(chunk, str):
                    chunk = chunk.encode(encoding or "utf-8")
                elif not isinstance(chunk, (bytes, bytearray)):
                    chunk = str(chunk).encode("utf-8")
                send = self._send
                if send is not None:
                    await send({
                        "type": "http.response.body",
                        "body": bytes(chunk),
                        "more_body": True,
                    })
            send = self._send
            if send is not None:
                # Final body event with ``more_body=False`` closes the
                # stream; ASGI servers emit the ``0\r\n\r\n`` terminator
                # for HTTP/1.1 chunked-transfer-encoding.
                await send({
                    "type": "http.response.body",
                    "body": b"",
                })
            self._sent = True
            # Streaming terminator hit the wire -- drain finishers.
            _drain_finishers(self)
            return

        # Buffered path (no prior ``stream()`` call): existing behaviour.
        if chunk is not None:
            if isinstance(chunk, str):
                chunk = chunk.encode(encoding or "utf-8")
            elif not isinstance(chunk, (bytes, bytearray)):
                chunk = str(chunk).encode("utf-8")
            if not self._wrote_head:
                # Mirror the same skip-default-if-already-set rule as write().
                if self._header_map is None or b"content-type" not in self._header_map:
                    self.setHeader("Content-Type", "text/plain; charset=utf-8")
                if self._header_map is None or b"x-content-type-options" not in self._header_map:
                    self.setHeader("X-Content-Type-Options", "nosniff")
                self._wrote_head = True
            self._chunks.append(bytes(chunk))
        if not self._chunks:
            
            await self.send("", self._status)
            return
        body = b"".join(self._chunks)
        # Pick a content-type. If the user never called write() we may
        # not have one yet; fall back to text/plain so ASGI accepts it.
        ct = self._header_map.get(b"content-type") if self._header_map is not None else None
        if ct is None:
            ct = b"text/plain; charset=utf-8"
        await self._send_body(body, ct, self._status)

    # ----- SSE-friendly writer ----------------------------------------
    async def stream(self, chunk, encoding: str = "utf-8") -> bool:
        """Flush a Server-Sent-Events chunk to the wire immediately.

        Unlike ``res.write()`` (which buffers chunks until ``end()``
        and ships them as one ``http.response.body`` event),
        ``stream()`` sends each chunk as its own body event with
        ``more_body=True`` so ASGI servers (uvicorn, hypercorn)
        flush it to the client right away. This is what SSE clients
        expect: events arrive one-by-one instead of all-at-once at
        the end of the handler.

        Headers installed on the first call (idempotent — repeated
        calls don't duplicate):

          * ``Content-Type: text/event-stream; charset=utf-8``
          * ``Cache-Control: no-cache``
          * ``Connection: keep-alive``
          * ``X-Accel-Buffering: no`` — disables nginx response
            buffering so events are flushed to the client immediately.

        ``Content-Length`` is intentionally omitted because the body
        is unbounded; HTTP/1.1 falls back to chunked-transfer-encoding.

        ``end()`` recognises the streaming state and emits a final
        empty body with ``more_body=False`` instead of falling through
        to the buffered flush.

        Returns ``True`` if the chunk was flushed, ``False`` if the
        response had already ended.
        """
        if self._ended:
            return False
        # Headers + first-flush. Honour a user-set Content-Type so the
        # ``setHeader("Content-Type", ...) + stream(...)`` pattern keeps
        # the caller's MIME. Only install the default SSE Content-Type
        # when the caller hasn't picked one. The other three SSE
        # headers are still installed unconditionally — they don't
        # conflict with anything a normal handler sets.
        if self._header_map is None or b"content-type" not in self._header_map:
            self.setHeader("Content-Type", "text/event-stream; charset=utf-8")
        self.setHeader("Cache-Control", "no-cache")
        self.setHeader("Connection", "keep-alive")
        self.setHeader("X-Accel-Buffering", "no")
        
        self._streaming = True

        # Encode the chunk (same coercion rules as ``write()``).
        if isinstance(chunk, str):
            chunk = chunk.encode(encoding or "utf-8")
        elif not isinstance(chunk, (bytes, bytearray)):
            chunk = str(chunk).encode("utf-8")
        chunk = bytes(chunk)

        # First-stream flush: emit the head WITHOUT a Content-Length
        # header (the body is unbounded). Subsequent calls reuse the
        # already-flushed head. The ASGI server handles the body-event
        # framing: each chunk becomes its own HTTP/1.1 chunked frame
        # on the wire.
        if not self._streamed:
            headers: list[tuple[bytes, bytes]] = []
            if self._header_map is not None:
                for k, v in self._header_map.items():
                    if isinstance(v, list):
                        headers.extend((k, item) for item in v)
                    else:
                        headers.append((k, v))
            send = self._send
            if send is not None:
                try:
                    self._response_started = True
                    await send({
                        "type": "http.response.start",
                        "status": self._status,
                        "headers": headers,
                    })
                except Exception:
                    self._ended = True
                    raise
            self._streamed = True
            self._sent_body = b""

        self._sent_body_size += len(chunk)

        # Flush the chunk immediately.
        send = self._send
        if send is not None:
            try:
                await send({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                })
            except Exception:
                self._ended = True
                raise
        else:
            # Test-mode path (no ASGI ``send`` callable wired up):
            # fall back to buffering so end() can still emit a body.
            self._chunks.append(chunk)
            self._wrote_head = True
            self._streamed = False
        return True

    
    async def sendStatus(self, code: int):
        """Set ``status`` and send its standard reason phrase as the body.

        Example::

            await res.sendStatus(404)   # body: "Not Found"
            await res.sendStatus(200)   # body: "OK"

        The response is terminated, just like ``res.send(...)``.
        """
        if self._sent:
            return self
        if not isinstance(code, int) or not (100 <= code <= 599):
            raise ValueError(f"Invalid HTTP status code: {code!r}")
        try:
            phrase = _HTTPStatus(code).phrase
        except ValueError:
            phrase = ""
        
        return await self.status(code).send(phrase)

    # ----- Terminal methods ----------------------------------------------
    async def json(self, data, status: int = None):
        # Hot path: orjson is already the cheapest JSON serializer. The
        # `OPT_SERIALIZE_NUMPY` flag costs every call — only opt in when
        # the payload actually contains numpy scalars, which is rare on
        # /hello-style routes. The wrapped form below detects numpy at
        # runtime only when the data is dict/list/tuple; primitive
        # payloads skip the check entirely.
        body = _orjson_dumps_fast(data)
        # Compression is decided inside ``_send_body`` (single funnel
        # for json/send/sendFile small-file paths) so the same gate
        # covers every response shape — and so we add ``Vary:
        # Accept-Encoding`` exactly once, regardless of which writer
        # was used.
        await self._send_body(body, _JSON_CT, status if status is not None else self._status)

    async def send(self, text, status: int = None):
        if self._sent: return
        body = text.encode() if isinstance(text, str) else text
        await self._send_body(body, b"text/plain; charset=utf-8", status if status is not None else self._status)

    async def export(self, body, filename: str, headers: dict = None):
        """Send ``body`` as a file download.

        Infers the content-type from ``filename`` via :mod:`mimetypes`
        (falls back to ``application/octet-stream``) and sets
        ``Content-Disposition: attachment; filename="<name>"`` so the
        browser triggers Save-As instead of trying to render the
        payload. Any extra headers the caller passes in ``headers`` are
        merged on top.

        Use this for *user-generated* file exports where the user
        picks their own library (``weasyprint``, ``openpyxl``,
        ``xlwt``, ``qrcode``, ``pillow``, ``reportlab``, ...). The
        framework does not produce the bytes — it only handles the
        download envelope::

            await res.export(pdf_bytes, "invoice.pdf")
            await res.export(xlsx_bytes, "report.xlsx",
                                 headers={"cache-control": "no-store"})

        ``body`` accepts ``bytes`` directly. Strings are encoded as
        UTF-8. For very large or already-on-disk files, prefer
        :meth:`download`.
        """
        if self._sent:
            return
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif not isinstance(body, (bytes, bytearray)):
            raise TypeError(
                "res.export() expects bytes-like body; got "
                f"{type(body).__name__}."
            )
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = "application/octet-stream"
        disposition = f'attachment; filename="{filename}"'
        if self._header_map is None:
            self._header_map = dict(self._app._base_header_map)
        self._header_map[b"content-disposition"] = disposition.encode("utf-8")
        if headers:
            for key, value in headers.items():
                self._header_map[key.lower().encode()] = str(value).encode("utf-8")
        await self._send_body(
            body,
            content_type.encode("utf-8"),
            self._status,
        )


    async def render(self, template_name: str, context: dict = None, filters=None):
        """Render a Jinja2 template to HTML.

        ``template_name`` is the path relative to the directory passed to
        :meth:`flameo.Flameo.use_template`, and SHOULD include the
        ``.html`` extension. ``context`` is an optional dict of template variables.
        ``filters`` optionally registers extra Jinja2 filters scoped to this
        single render call -- accepts the same shapes as ``use_template()``:
        a list of callables (named by ``__name__``) or a dict of
        ``{"filter_name": callable}``. App and route filters stay visible
        unless a call filter deliberately shadows them with the same name.

        Requires the app to be wired up with ``app.use_template(directory)``.
        On the render hot path we read the compiled Jinja2 ``Environment``
        off ``self._app._template_env`` and go through ``_send_body`` so
        compression + Content-Length handling stay consistent with the
        rest of the response pipeline.
        """
        if self._sent:
            return
        app = self._app
        env = getattr(app, "_template_env", None) if app is not None else None
        if env is None:
            raise RuntimeError(
                "Initialize 'app.use_template()' to render templates"
            )
        # A route may register its own ``filters=`` -- that route's
        # environment (app filters plus route filters) takes precedence
        # over the app-wide one.
        route_env = getattr(self._req, "_template_env", None) if self._req is not None else None
        if route_env is not None:
            env = route_env.resolve(app)
        # Call-scoped ``filters=`` sit on top of whatever env won above;
        # the per-call overlay is dropped after this response.
        if filters is not None:
            env = build_call_environment(env, filters)
        base = getattr(app, "_template_context", None)
        if base:
            context = {**base, **(context or {})}
        rendered = await render_template(
            env, template_name, context if context is not None else {}
        )
        body = rendered.encode("utf-8") if isinstance(rendered, str) else rendered
        await self._send_body(body, b"text/html; charset=utf-8", self._status)

    async def _send_body(self, body, content_type, status):
        # Endpoint-response cache capture (fill-once, BEFORE the session
        # finalize hook so session-minted Set-Cookie headers can never
        # enter a cached entry, and BEFORE compression so the stored
        # body is raw and replay can re-negotiate per request).
        cap = self._cache_capture__
        if cap is not None and not cap:
            hm = self._header_map
            if hm is None:
                cap["headers"] = []
            else:
                cap["headers"] = [
                    (k.decode("latin-1"),
                     v.decode("latin-1") if not isinstance(v, list)
                     else [i.decode("latin-1") for i in v])
                    for k, v in hm.items()
                ]
            cap["status"] = status
            cap["ct"] = (content_type.decode("latin-1")
                         if isinstance(content_type, (bytes, bytearray))
                         else content_type)
            cap["body"] = body

        # Session middleware hook — fires once on the way to the wire,
        # after the handler ran and after any other middleware had a
        # chance to mutate ``req.session``, but BEFORE we serialize
        # headers. This is the last point where a Set-Cookie can be
        # folded into the response. See ``sessions.make_session_middleware``.
        # Errors are swallowed inside the commit callback itself.
        finalize = getattr(self, "_finalize_session__", None)
        if finalize is not None:
            try:
                finalize()
            except Exception:
                pass

        # Hot path: the common /hello route has zero per-response headers
        # (setHeader/cookie never called), so we skip the items() walk
        # and build a 2-tuple header list directly. When the user *does*
        # call setHeader, we fall through to the list-build path.
        # Compression gate (single funnel for json / send / sendFile's
        # small-file branch). ``_maybe_compress`` runs every gate:
        # ``use_compression`` on, body clears ``gzip_min_size``,
        # content-type is on the compressible whitelist, and the
        # negotiated codec (zstd/br/gzip/deflate) is acceptable to
        # the client per ``Accept-Encoding``. The wire encoding comes
        # back so the Content-Encoding header below can match.
        app = self._app
        encoding = None
        if app.enable_gzip:
            body, encoding = await app._maybe_compress_async(self._req, body)
        body_len = len(body)
        cl = (b"content-length", str(body_len).encode())
        ct = (b"content-type", content_type)
        header_map = self._header_map
        if header_map is not None:
            
            if b"content-type" in header_map:
                ct = (b"content-type", header_map.pop(b"content-type"))
            headers = [(k, v) for k, v in header_map.items() for v in (v if isinstance(v, list) else [v]) if k != b"content-type"]
            headers.append(ct)
            headers.append(cl)
            if encoding is not None:
                # ``encoding`` carries the wire token (gzip/br/zstd/
                # deflate) so the right Content-Encoding header
                # goes out for whatever codec was negotiated.
                headers.append((b"content-encoding", encoding.encode()))
                # Cache honesty: the response varies on what the
                # client asked for. Replace any existing Vary so
                # the header list stays flat.
                if any(k == b"vary" for k, _ in headers):
                    headers = [
                        (k, v) for k, v in headers if k != b"vary"
                    ]
                headers.append((b"vary", b"Accept-Encoding"))
            headers_t = headers  # only used by the logging branch
        else:
            if encoding is not None:
                headers = [
                    ct, cl,
                    (b"content-encoding", encoding.encode()),
                    (b"vary", b"Accept-Encoding"),
                ]
            else:
                headers = [ct, cl]
            headers_t = headers

        # Mirror the response onto instance slots so the request logger
        # sees the real status/body length. _sent_headers is only kept
        # when logging is enabled; the cheap bool check avoids the
        # list copy on the common (logging-off) path.
        self._sent_body = body
        self._sent_body_size = len(body)
        self._status = status
        if app._show_requests:
            self._sent_headers = headers_t

        try:
            send = self._send
            if send is not None:
                self._response_started = True
                await send({
                    "type": "http.response.start",
                    "status": status,
                    "headers": headers,
                })
                await send({
                    "type": "http.response.body",
                    "body": body,
                })

            self._sent = True
        except Exception as e:
            raise ValueError(e)
        # Response handed to the wire -- drain finishers.
        _drain_finishers(self)

    async def _send_file_streaming(self, *, resolved_path: str,
                                   content_type: bytes, status: int,
                                   chunk_size: int, file_size,
                                   extra_headers):
        """Stream a file in chunks via ASGI ``http.response.body``.

        Replaces the old ``f.read()`` + ``_send_body(...)`` path used by
        ``sendFile`` / ``download``. The whole file is never held in
        memory at once: each ``chunk_size`` slice is read in an executor
        thread and immediately written to the ASGI send channel. When
        the server is configured for HTTP/1.1 chunked transfer encoding
        this also avoids needing a pre-computed Content-Length (we set
        one anyway for compatibility with servers/clients that expect it).

        The final ``more_body=False`` body event closes the response,
        which the ASGI server translates into ``0\r\n\r\n`` for HTTP/1.1.
        """
        send = self._send
        if send is None:
            return

        loop = asyncio.get_running_loop()

        # ------------------------------------------------------------------
        # Adaptive chunk-size policy.
        # ------------------------------------------------------------------
        # The caller picks a default (typically 256 KiB) but we override it
        # based on the file's actual size once we've probed ``file_size``:
        #
        #   * Tiny files  (< 64 KiB): we skip the streaming path entirely
        #     and write the body in one ``http.response.body`` event. The
        #     overhead of chunked-transfer framing + per-chunk executor
        #     hops exceeds the cost of just copying a small blob.
        #   * Mid files   (64 KiB ... 4 MiB): honour the caller's
        #     ``chunk_size`` (default 256 KiB) — a sensible balance between
        #     syscall frequency and per-event overhead.
        #   * Large files (>= 4 MiB): use 1 MiB chunks so the amortized
        #     cost per syscall drops; a 100 MB JS bundle then needs ~400
        #     events instead of ~1500, which meaningfully reduces Python-
        #     side send-channel overhead under load.
        #
        # ``file_size`` may be ``None`` if ``os.path.getsize`` failed
        # earlier (e.g. file removed mid-request). In that case we fall
        # back to the caller's default and let the streaming path discover
        # EOF naturally.
        # ------------------------------------------------------------------
        if file_size is not None and file_size < 64 * 1024:
            small_body = await loop.run_in_executor(
                None, _read_whole_file, resolved_path,
            )
            # Compression gate (mirrors _send_body). Small files go
            # through the same single-shot path, so we get a precise
            # Content-Length on the wire instead of chunked transfer —
            # which is what HTTP/1.1 clients overwhelmingly prefer for
            # sub-64KiB assets.
            app = self._app
            # Codec-aware single-shot compress: returns
            # ``(body, encoding)``. Small files (< 64 KiB) share the
            # same single-shot funnel with ``_send_body`` so the
            # on-the-wire Content-Length stays pinned.
            small_body, encoding = await app._maybe_compress_async(self._req, small_body)
            # Build headers (same logic as the streaming path below).
            headers: list[tuple[bytes, bytes]] = []
            if self._header_map is not None:
                for k, v in self._header_map.items():
                    if isinstance(v, list):
                        headers.extend((k, item) for item in v)
                    else:
                        headers.append((k, v))
            headers.append((b"content-type", content_type))
            # We know the size — pin Content-Length so HTTP/1.1 clients
            # don't have to buffer chunked transfer.
            headers.append((b"content-length", str(len(small_body)).encode()))
            for k, v in extra_headers:
                headers = [(hk, hv) for hk, hv in headers if hk != k]
                headers.append((k, v))
            if encoding is not None:
                # Mirror what _send_body does for compressed bodies:
                # the right Content-Encoding for whatever codec won,
                # plus Vary: Accept-Encoding so caches don't serve
                # the compressed bytes to a client that didn't ask
                # for them.
                headers.append((b"content-encoding", encoding.encode()))
                if any(k == b"vary" for k, _ in headers):
                    headers = [
                        (k, v) for k, v in headers if k != b"vary"
                    ]
                headers.append((b"vary", b"Accept-Encoding"))

            self._sent_body = small_body
            self._sent_body_size = len(small_body)
            self._status = status
            if app._show_requests:
                self._sent_headers = headers

            try:
                self._response_started = True
                await send({
                    "type": "http.response.start",
                    "status": status,
                    "headers": headers,
                })
                # Single-shot body — no ``more_body`` flag means the
                # server emits the terminator for us.
                await send({
                    "type": "http.response.body",
                    "body": small_body,
                })
                self._sent = True
            except Exception:
                raise
            # Response handed to the wire -- drain finishers.
            _drain_finishers(self)
            return

        # Decide streaming chunk size based on the file's magnitude.
        if file_size is None:
            effective_chunk = chunk_size  # unknown — fall back to caller default
        elif file_size >= 4 * 1024 * 1024:
            effective_chunk = 1024 * 1024  # 1 MiB to amortize executor/send hops
        else:
            effective_chunk = chunk_size  # mid-size — honour caller default

        # Compression gate for the streaming path. Same rules as
        # the single-shot funnel in _send_body: compression enabled,
        # file clears gzip_min_size, content-type is on the
        # compressible list, and the negotiated codec is acceptable
        # to the client. ``stream_compress_factory`` returns
        # ``(encoding, factory)`` when all gates clear; we then use
        # ``factory()`` to materialise the codec's (compress,
        # finalize, header) 3-tuple inside the streaming loop.
        # ``file_size is None`` opts out of streaming compression
        # because we can't reliably emit a Content-Length on the
        # wire for compressed bytes anyway.
        app = self._app
        stream_encoding, stream_factory = app.stream_compress_factory(
            self._req, content_type, file_size,
        )
        use_stream_compress = stream_encoding is not None

        # Build headers: user-provided map first, then our file headers,
        # then any extras (e.g. Content-Disposition for downloads).
        headers: list[tuple[bytes, bytes]] = []
        if self._header_map is not None:
            for k, v in self._header_map.items():
                if isinstance(v, list):
                    headers.extend((k, item) for item in v)
                else:
                    headers.append((k, v))
        headers.append((b"content-type", content_type))
        if file_size is not None and not use_stream_compress:
            # Raw streaming path: we know exactly how many bytes will
            # hit the wire, so we can pin Content-Length. The compressed
            # path omits it (the compressed size is only known at the
            # end of the stream) and relies on HTTP/1.1 chunked transfer.
            headers.append((b"content-length", str(file_size).encode()))
        for k, v in extra_headers:
            # Last-write-wins so the caller (download) can override.
            headers = [(hk, hv) for hk, hv in headers if hk != k]
            headers.append((k, v))
        if use_stream_compress:
            # Wire the negotiated codec + Vary: Accept-Encoding so
            # caches don't serve the compressed body to a client that
            # didn't ask for it.
            headers.append((b"content-encoding", stream_encoding.encode()))
            if any(k == b"vary" for k, _ in headers):
                headers = [
                    (k, v) for k, v in headers if k != b"vary"
                ]
            headers.append((b"vary", b"Accept-Encoding"))

        self._sent_body = b""
        self._sent_body_size = 0
        self._status = status
        # Mirror onto instance state so the request logger sees the
        # real size. If the file is huge this just shows the streamed
        # size (sum of chunks sent) which is fine for the access log.
        if app._show_requests:
            self._sent_headers = headers

        # Streaming strategy: open the file once, then for each chunk
        # hop into the default executor to call ``f.read(effective_chunk)``
        # — never blocking the event loop — and immediately write that
        # chunk to the ASGI send channel as an ``http.response.body``
        # event with ``more_body=True``. The total bytes on the wire
        # therefore equal the file size; the only memory held at any
        # moment is one ``effective_chunk`` buffer plus the ASGI server's
        # internal write buffer.
        try:
            self._response_started = True
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            })

            if use_stream_compress:
                # Build the codec-specific streaming state machine
                # lazily. ``stream_factory`` is the per-codec 3-tuple
                # builder (``stream_gzip_factory``,
                # ``stream_zstd_factory``, etc.); each returns
                # ``(compress_fn, finalize_fn, header_bytes)`` so the
                # loop below stays codec-agnostic. ``header_bytes`` is
                # the per-codec stream prefix (gzip header for gzip,
                # empty for zstd/deflate/brotli whose libraries embed
                # the magic in the first ``compress()`` call).
                compress_fn, finalize, codec_header = stream_factory()
                if codec_header:
                    self._sent_body_size += len(codec_header)
                    await send({
                        "type": "http.response.body",
                        "body": codec_header,
                        "more_body": True,
                    })
                with open(resolved_path, "rb") as f:
                    read_chunk = functools.partial(f.read, effective_chunk)
                    while True:
                        chunk = await loop.run_in_executor(None, read_chunk)
                        if not chunk:
                            break
                        # Per-chunk deflate runs in the executor so we
                        # never block the event loop.
                        wire = await loop.run_in_executor(
                            None, compress_fn, chunk,
                        )
                        self._sent_body_size += len(wire)
                        if wire:
                            await send({
                                "type": "http.response.body",
                                "body": wire,
                                "more_body": True,
                            })
                # Close the codec-specific stream and append any
                # trailer (gzip: CRC32 + ISIZE; zstd / brotli /
                # deflate: just the deflate Z_FINISH tail, with the
                # stored-empty marker stripped). Together with the
                # codec-specific header above this forms a complete
                # compressed stream for whatever codec won.
                trailer = await loop.run_in_executor(None, finalize)
                if trailer:
                    self._sent_body_size += len(trailer)
                    await send({
                        "type": "http.response.body",
                        "body": trailer,
                        "more_body": True,
                    })
            else:
                with open(resolved_path, "rb") as f:
                    # Bind a no-arg partial so the executor only sees one
                    # argument (the file handle) and we can swap it in a
                    # single statement. ``effective_chunk`` is the size-tier-
                    # adjusted chunk (typically the caller's default for mid-
                    # size files, 1 MiB for large ones).
                    read_chunk = functools.partial(f.read, effective_chunk)
                    while True:
                        chunk = await loop.run_in_executor(None, read_chunk)
                        if not chunk:
                            break
                        self._sent_body_size += len(chunk)
                        await send({
                            "type": "http.response.body",
                            "body": chunk,
                            # Mark every intermediate chunk as
                            # ``more_body=True``; the trailing empty body
                            # below terminates the transfer.
                            "more_body": True,
                        })

            # Terminate the body. ASGI requires the final message to
            # have more_body=False (default) and may carry an empty
            # body — uvicorn/uvloop emit the terminating 0\r\n\r\n.
            await send({"type": "http.response.body", "body": b""})
            self._sent = True
        except Exception:
            # Re-raise; the ASGI dispatcher in Flameo.__call__ logs
            # the real exception in debug mode. Don't leave the response
            # marked sent — a downstream try/except may want to retry.
            raise
        # Streaming terminator hit the wire -- drain finishers.
        _drain_finishers(self)


class RSGIResponse:
    # NOTE: `sendFile` is intentionally NOT a slot — the lazy
    # __getattr__ builder below is the canonical source. `_sendFile`
    # caches the closure after first access.
    __slots__ = ("_hdr", "_sent", "_body", "_status", "_sendFile",
                 "_download", "_chunks", "_wrote_head", "_ended", "_req",
                 "_streamed", "_streaming", "_proto", "_transport", "_app",
                 # Set by the session middleware (``app.use_session``)
                 # with the per-request cookie commit callable.
                 "_finalize_session__",
                 # List of async cleanup callables registered via
                 # ``res.finish(coro)``. Drained with
                 # ``asyncio.ensure_future`` AFTER the response body
                 # has been handed to the wire -- never awaited, so
                 # a slow finisher cannot delay the client.
                 "_finishers__",
                 # Endpoint-response cache capture — mirrors the ASGI
                 # ``Response._cache_capture__`` slot above.
                 "_cache_capture__")

    def __getattr__(self, name: str) -> Any:
        # Lazy sendFile: only built when a handler actually accesses it.
        if name == "sendFile":
            cached = self._sendFile
            if cached is not None:
                return cached
            send_func = _make_rsgi_send_file(self)
            object.__setattr__(self, "_sendFile", send_func)
            return send_func
        if name == "download":
            cached = object.__getattribute__(self, "_download")
            if cached is not None:
                return cached
            download_func = _make_rsgi_download(self)
            object.__setattr__(self, "_download", download_func)
            return download_func
        raise AttributeError(name)

    @property
    def req(self):
        return self._req

    def __init__(self, req=None, proto=None, app=None) -> None:
        self._hdr: list[tuple[str, str]] = []
        self._sent = False
        self._body: bytes = b""
        self._status: int = 200
        self._app = app  # back-ref for res.render() -> jinja2 environment
        self._sendFile = None  # lazy sendFile cache (see __getattr__)
        self._download = None
        self._req = req
        if req is not None:
            try:
                req._res = self
            except AttributeError:
                pass
        
        self._chunks: list[bytes] = []
        self._wrote_head = False
        
        self._streamed = False
        self._streaming = False
        self._proto = proto
        self._transport = None
        self._ended = False
        # Set by the session middleware; see ``Response._finalize_session__``.
        self._finalize_session__ = None
        # Per-request async-cleanup list. ``res.finish(coro)`` appends;
        # the framework drains it with ``asyncio.ensure_future`` after
        # the response body has been handed to the wire (or, on
        # handler-raise paths, in the dispatcher's finally block).
        self._finishers__ = []
        # Endpoint-response cache capture — see ``Response``.
        self._cache_capture__ = None

    # ----- res.finish(): per-request async cleanup ------------------------
    def finish(self, fn):
        """RSGI mirror of ``Response.finish``.

        Same contract: callable is appended to ``_finishers__`` and
        drained with ``asyncio.ensure_future`` after the response
        body has been handed to the wire (or in the dispatcher
        finally block for handler-raise paths).
        """
        if not callable(fn):
            raise TypeError("res.finish() expects a callable")
        self._finishers__.append(fn)
        return self

    after = finish

    def status(self, code: int):
        if self._sent:
            return self
        if not isinstance(code, int) or not (100 <= code <= 599):
            raise ValueError(f"Invalid HTTP status code: {code!r}")
        self._status = code
        return self

    # ----- Chainable header ----------------------------------------------
    def setHeader(self, key: str, value: str):
        k, v = key.lower(), value
        self._hdr = [(hk, hv) for hk, hv in self._hdr if hk != k]
        self._hdr.append((k, v))
        # Auto-flip streaming mode on SSE Content-Type, mirroring the
        # ASGI Response. Lets the
        # ``setHeader("Content-Type", "text/event-stream") + write(...)``
        # idiom stream per-call without forcing the caller to also call
        # ``res.stream()`` first.
        if k == "content-type" and v.lower().startswith("text/event-stream"):
            self._streaming = True
        return self

    header = setHeader
    set = setHeader

    # ----- Chainable cookie ----------------------------------------------
    def cookie(self, name: str, value: str, options: dict = None, **kwargs):
        if self._sent:
            return self
        opts = dict(options) if options else {}
        opts.update(kwargs)
        parts = [f"{name}={value}"]
        if "maxAge" in opts and opts["maxAge"] is not None:
            parts.append(f"Max-Age={int(opts['maxAge'])}")
        if opts.get("expires") is not None:
            parts.append(f"Expires={opts['expires']}")
        if opts.get("domain"):
            parts.append(f"Domain={opts['domain']}")
        parts.append(f"Path={opts.get('path', '/')}")
        if opts.get("httpOnly"):
            parts.append("HttpOnly")
        if opts.get("secure"):
            parts.append("Secure")
        if opts.get("sameSite"):
            ss = str(opts["sameSite"])
            parts.append(f"SameSite={ss[0].upper()}{ss[1:].lower()}" if ss.lower() != "none" else "SameSite=None")
        if opts.get("partitioned"):
            parts.append("Partitioned")
        if opts.get("priority") is not None:
            pr = str(opts["priority"]).lower()
            if pr not in ("low", "medium", "high"):
                raise ValueError("priority must be one of 'low', 'medium', 'high'")
            parts.append(f"Priority={pr.capitalize()}")
        self._hdr = [(hk, hv) for hk, hv in self._hdr if hk != "set-cookie"]
        self._hdr.append(("set-cookie", "; ".join(parts)))
        return self

    def clearCookie(self, name: str, options: dict = None, **kwargs):
        """Expire a single cookie. Chainable.
        """
        if self._sent:
            return self
        merged = {**options} if options else {}
        merged.update(kwargs)
        path = merged.get("path", "/")
        self._hdr = [(hk, hv) for hk, hv in self._hdr if hk != "set-cookie"]
        parts = [f"{name}=", "Expires=Thu, 01 Jan 1970 00:00:00 GMT", f"Path={path}"]
        if merged.get("domain"):
            parts.append(f"Domain={merged['domain']}")
        if merged.get("httpOnly"):
            parts.append("HttpOnly")
        if merged.get("secure"):
            parts.append("Secure")
        if merged.get("sameSite"):
            ss = str(merged["sameSite"])
            parts.append(f"SameSite={ss[0].upper()}{ss[1:].lower()}" if ss.lower() != "none" else "SameSite=None")
        if merged.get("partitioned"):
            parts.append("Partitioned")
        if merged.get("priority") is not None:
            pr = str(merged["priority"]).lower()
            if pr not in ("low", "medium", "high"):
                raise ValueError("priority must be one of 'low', 'medium', 'high'")
            parts.append(f"Priority={pr.capitalize()}")
        self._hdr.append(("set-cookie", "; ".join(parts)))
        return self

    def clearAllCookies(self, options: dict = None, **kwargs):
        """Expire every cookie the client sent. Chainable.

        ``options`` / ``**kwargs`` (e.g. ``path``, ``domain``) are
        forwarded for each expired cookie so the browser matches the
        original scope.
        """
        if self._sent:
            return self
        req = self._req
        names = list(req.cookies.keys()) if (req is not None and getattr(req, "cookies", None)) else []
        if not names:
            return self
        merged = {**options} if options else {}
        merged.update(kwargs)
        path = merged.get("path", "/")
        # Always drop existing Set-Cookie entries first so the bulk clear
        # wins over any prior cookie() call.
        self._hdr = [(hk, hv) for hk, hv in self._hdr if hk != "set-cookie"]
        for name in names:
            parts = [f"{name}=", "Expires=Thu, 01 Jan 1970 00:00:00 GMT", f"Path={path}"]
            if merged.get("domain"):
                parts.append(f"Domain={merged['domain']}")
            if merged.get("httpOnly"):
                parts.append("HttpOnly")
            if merged.get("secure"):
                parts.append("Secure")
            if merged.get("sameSite"):
                ss = str(merged["sameSite"])
                parts.append(f"SameSite={ss[0].upper()}{ss[1:].lower()}" if ss.lower() != "none" else "SameSite=None")
            if merged.get("partitioned"):
                parts.append("Partitioned")
            if merged.get("priority") is not None:
                pr = str(merged["priority"]).lower()
                if pr not in ("low", "medium", "high"):
                    raise ValueError("priority must be one of 'low', 'medium', 'high'")
                parts.append(f"Priority={pr.capitalize()}")
            self._hdr.append(("set-cookie", "; ".join(parts)))
        return self

    # ----- Chainable redirect --------------------------------------------
    async def redirect(self, url: str, code: int = 302):
        if self._sent:
            return self
        self.setHeader("Location", url)
        return await self.status(code).send("")

    async def jsonp(self, data, status: int = None) -> None:
        if self._sent:
            return
        callback = None
        req = self._req
        if req is not None:
            try:
                q = req.query
            except Exception:
                q = None
            if q is not None:
                v = q.get("callback") if hasattr(q, "get") else q["callback"]
                if isinstance(v, list):
                    v = v[0] if v else None
                if isinstance(v, str) and _VALID_JSONP_NAME.match(v):
                    callback = v
        body = orjson.dumps(data, default=_orjson_default)
        if callback:
            self.setHeader("X-Content-Type-Options", "nosniff")
            self.setHeader("Content-Type", "application/javascript; charset=utf-8")
            payload = b"/**/ typeof " + callback.encode() + b" === 'function' && " + callback.encode() + b"(" + body + b");"
            self._body = payload
            if status is not None:
                self._status = status
            self._sent = True
        else:
            await self.json(data, status)
    
    async def write(self, chunk, encoding: str = "utf-8") -> bool:
        if self._ended:
            return False
        if chunk is None:
            return True
        if isinstance(chunk, str):
            chunk = chunk.encode(encoding or "utf-8")
        elif not isinstance(chunk, (bytes, bytearray)):
            chunk = str(chunk).encode("utf-8")
        if not self._wrote_head:
            # Skip the default install when the user has already
            # set the header (e.g. via ``res.stream()`` or an
            # explicit ``setHeader("Content-Type", ...)``).
            if not any(hk == "content-type" for hk, _ in self._hdr):
                self.setHeader("Content-Type", "text/plain; charset=utf-8")
            if not any(hk == "x-content-type-options" for hk, _ in self._hdr):
                self.setHeader("X-Content-Type-Options", "nosniff")
            self._wrote_head = True
        # Streaming-mode: ``stream()`` (or an SSE Content-Type via
        # ``setHeader``) flipped ``_streaming`` so each ``write()``
        # ships its own chunk via the RSGI ``response_stream``
        # transport instead of being buffered into ``self._chunks``
        # and emitted in one shot at ``end()``. The first such call
        # opens the transport; subsequent calls reuse it.
        if self._streaming:
            await self._ensure_stream_transport()
            if self._transport is None:
                self._chunks.append(bytes(chunk))
                return True
            await self._transport.send_bytes(bytes(chunk))
            return True
        self._chunks.append(bytes(chunk))
        return True

    async def _ensure_stream_transport(self) -> None:
        """Open the RSGI ``response_stream`` transport on first use.

        RSGI's ``proto.response_stream(status, headers)`` returns a
        ``RSGIHTTPStreamTransport`` whose ``send_bytes`` method writes
        directly to the wire. We acquire it lazily — once per
        streaming response — and hold a reference on ``self`` so it
        isn't GC'd before ``end()`` (granian's Rust transport probably
        finalises the response on drop).
        """
        if self._transport is not None:
            return
        proto = self._proto
        if proto is None:
            # Test/synthesis path with no real granian protocol
            # object. Fall back to buffering so handlers keep working
            # under unit tests that construct an ``RSGIResponse`` with
            # ``proto=None``.
            return
        # ``_hdr`` is native str already — granian's RSGI transport takes
        # str pairs, so no decode round trip remains. Hand a shallow copy
        # so the transport owns its list.
        headers_str = list(self._hdr)
        self._transport = proto.response_stream(self._status, headers_str)
        self._streamed = True
        self._sent = True

    async def end(self, chunk=None, encoding: str = "utf-8") -> None:
        if self._ended:
            return
        self._ended = True
        if chunk is not None:
            if isinstance(chunk, str):
                chunk = chunk.encode(encoding or "utf-8")
            elif not isinstance(chunk, (bytes, bytearray)):
                chunk = str(chunk).encode("utf-8")
            # Streaming close path: send the final chunk via the
            # transport, then drop it so granian can finalise the
            # response. We deliberately don't append to ``_chunks``
            # here — that would defeat per-call delivery.
            if self._streaming:
                await self._ensure_stream_transport()
                if self._transport is not None:
                    await self._transport.send_bytes(bytes(chunk))
                    self._transport = None
                    return
                self._chunks.append(bytes(chunk))
                self._streaming = False
                self._transport = None
                if not self._wrote_head:
                    if not any(hk == "content-type" for hk, _ in self._hdr):
                        self.setHeader("Content-Type", "text/plain; charset=utf-8")
                    if not any(hk == "x-content-type-options" for hk, _ in self._hdr):
                        self.setHeader("X-Content-Type-Options", "nosniff")
                    self._wrote_head = True
            else:
                if not self._wrote_head:
                    # Mirror the same skip-default-if-already-set rule as write().
                    if not any(hk == "content-type" for hk, _ in self._hdr):
                        self.setHeader("Content-Type", "text/plain; charset=utf-8")
                    if not any(hk == "x-content-type-options" for hk, _ in self._hdr):
                        self.setHeader("X-Content-Type-Options", "nosniff")
                    self._wrote_head = True
                self._chunks.append(bytes(chunk))
        # Streaming close without a trailing chunk: just drop the
        # transport so granian finalises the response.
        if self._streamed:
            self._transport = None
            return
        if not self._chunks:
            await self.send("", self._status)
            return
        self._body = b"".join(self._chunks)
        ct = next((v for k, v in self._hdr if k == "content-type"), None)
        if ct is None:
            self.setHeader("Content-Type", "text/plain; charset=utf-8")
        self._sent = True
        # Response assembled and marked sent -- drain finishers so any
        # cleanup runs as soon as the wire-send is complete (the
        # RSGI dispatcher reads _body/_hdr/_status in __rsgi__ and
        # hands them to granian right after we return).
        _drain_finishers(self)

    # ----- SSE-friendly writer ----------------------------------------
    async def stream(self, chunk, encoding: str = "utf-8") -> bool:
        """Flush a Server-Sent-Events chunk to the wire immediately.

        Honours a user-set Content-Type (only installs the SSE default
        when the caller hasn't already picked one), flips ``_streaming``
        so subsequent ``res.write(...)`` calls in the same handler also
        stream, and uses the RSGI ``response_stream`` transport to send
        each chunk without buffering. Mirrors the ASGI Response.
        """
        if self._ended:
            return False
        # Headers: install SSE defaults idempotently, but keep a
        # caller-set Content-Type intact so the
        # ``setHeader(...) + stream(...)`` pattern survives.
        if not any(hk == "content-type" for hk, _ in self._hdr):
            self.setHeader("Content-Type", "text/event-stream; charset=utf-8")
        self.setHeader("Cache-Control", "no-cache")
        self.setHeader("Connection", "keep-alive")
        self.setHeader("X-Accel-Buffering", "no")
        self._streaming = True
        # Encode + ship via the streaming path (bypasses write()'s
        # default-header installation because we're already past
        # head-time).
        if isinstance(chunk, str):
            chunk = chunk.encode(encoding or "utf-8")
        elif not isinstance(chunk, (bytes, bytearray)):
            chunk = str(chunk).encode("utf-8")
        if not self._wrote_head:
            self._wrote_head = True
        await self._ensure_stream_transport()
        if self._transport is not None:
            await self._transport.send_bytes(bytes(chunk))
        else:
            # No real RSGI transport (test mode): fall back to
            # buffering so the handler still produces a coherent body.
            self._chunks.append(bytes(chunk))
        return True

    async def sendStatus(self, code: int):
        if self._sent:
            return self
        if not isinstance(code, int) or not (100 <= code <= 599):
            raise ValueError(f"Invalid HTTP status code: {code!r}")
        try:
            phrase = _HTTPStatus(code).phrase
        except ValueError:
            phrase = ""
        return await self.status(code).send(phrase)

    # ----- Terminal methods ----------------------------------------------
    async def json(self, data, status: int = None) -> None:
        if self._sent:
            return
        body = orjson.dumps(data, default=_orjson_default)
        await self._send_body(body, b"application/json", status if status is not None else self._status)

    async def send(self, data: str | bytes, status: int = None) -> None:
        if self._sent:
            return
        body = data.encode() if isinstance(data, str) else data
        await self._send_body(body, b"text/plain; charset=utf-8", status if status is not None else self._status)
    async def export(self, body, filename: str, headers: dict = None) -> None:
        """RSGI mirror of :meth:`Response.attachment`.

        See the ASGI version for the full contract — this method
        only differs in the header-list data structure (``_hdr`` is a
        list of byte-tuples here, vs. the ASGI dict). It still
        infers content-type from the filename, sets
        ``Content-Disposition: attachment; filename="<name>"``,
        and merges any caller-supplied headers on top.
        """
        if self._sent:
            return
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif not isinstance(body, (bytes, bytearray)):
            raise TypeError(
                "res.export() expects bytes-like body; got "
                f"{type(body).__name__}."
            )
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = "application/octet-stream"
        disposition = f'attachment; filename="{filename}"'
        # Rebuild the header list to dedupe — same pattern as setHeader.
        def _put(hdr_list, key, value):
            kl = key.lower()
            return [(hk, hv) for hk, hv in hdr_list if hk != kl] + [(kl, value)]
        self._hdr = _put(self._hdr, "content-disposition", disposition)
        if headers:
            for key, value in headers.items():
                self._hdr = _put(self._hdr, key, str(value))
        await self._send_body(
            body,
            content_type.encode("utf-8"),
            self._status,
        )


    async def render(self, template_name: str, context: dict = None, filters=None) -> None:
        """Render a Jinja2 template to HTML. RSGI mirror of ``Response.render``.

        ``filters`` optionally registers extra Jinja2 filters scoped to this
        single render call -- same shapes as ``use_template()`` (list of
        callables or a dict of ``{"filter_name": callable}``).
        """
        if self._sent:
            return
        app = self._app
        env = getattr(app, "_template_env", None) if app is not None else None
        if env is None:
            raise RuntimeError(
                "Initialize 'app.use_template()' to render templates"
            )
        # A route may register its own ``filters=`` -- that route's
        # environment (app filters plus route filters) takes precedence
        # over the app-wide one.
        route_env = getattr(self._req, "_template_env", None) if self._req is not None else None
        if route_env is not None:
            env = route_env.resolve(app)
        # Call-scoped ``filters=`` sit on top of whatever env won above;
        # the per-call overlay is dropped after this response.
        if filters is not None:
            env = build_call_environment(env, filters)
        base = getattr(app, "_template_context", None)
        if base:
            context = {**base, **(context or {})}
        rendered = await render_template(
            env, template_name, context if context is not None else {}
        )
        body = rendered.encode("utf-8") if isinstance(rendered, str) else rendered
        await self._send_body(body, b"text/html; charset=utf-8", self._status)

    async def _send_body(self, body: bytes, content_type: bytes, status: int) -> None:
        """Mirror of ``Response._send_body`` for the RSGI protocol.

        ASGI emits via the ``send`` callable from the per-request scope;
        RSGI defers that to ``__rsgi__`` which calls ``_into_bytes()``.
        This method populates the same slots (``_body``, ``_status``,
        ``_hdr``, ``_sent``) so a shared dispatcher like
        ``_send_default_not_found`` can write to either protocol without
        caring which response object it's holding.
        """
        # Endpoint-response cache capture (fill-once, BEFORE the session
        # finalize hook so session cookies can't enter a cached entry).
        # The body stays raw here — ``__rsgi__`` compresses later, so
        # replaying through this funnel re-compresses per request.
        cap = self._cache_capture__
        if cap is not None and not cap:
            cap["headers"] = [(k, v) for k, v in self._hdr]
            cap["status"] = status
            cap["ct"] = (content_type.decode("latin-1")
                         if isinstance(content_type, (bytes, bytearray))
                         else content_type)
            cap["body"] = body

        # Session middleware hook — see ASGI Response._send_body.
        # Session middleware hook — see ASGI Response._send_body.

        finalize = getattr(self, "_finalize_session__", None)
        if finalize is not None:
            try:
                finalize()
            except Exception:
                pass

        if self._sent:
            return
        self._body = body
        self._status = status

        hdr = self._hdr
        has_ct = False
        has_cl = False
        for k, _ in hdr:
            if k == "content-type":
                has_ct = True
            elif k == "content-length":
                has_cl = True
        if not has_ct:
            hdr.append(("content-type", content_type.decode("latin-1")))
        if has_cl:
            hdr = [(k, v) for k, v in hdr if k != "content-length"]
            self._hdr = hdr
        hdr.append(("content-length", str(len(body))))
        self._sent = True
        # Body assembled and marked sent -- drain finishers. The actual
        # wire-send happens later in __rsgi__ which calls _into_bytes
        # and hands the bytes to granian; the dispatcher's finally
        # block will also drain in case _into_bytes raises.
        _drain_finishers(self)

    def _into_bytes(self) -> tuple[int, list[tuple[str, str]], bytes]:
        return self._status, self._hdr, self._body

    async def _send_file_streaming(self, *, resolved_path: str,
                                   content_type: bytes, status: int,
                                   chunk_size: int, file_size,
                                   extra_headers: tuple):
        """RSGI counterpart of ``Response._send_file_streaming``.
        """
        loop = asyncio.get_running_loop()
        # RSGI's response_bytes path buffers the complete response. Read it
        # once rather than scheduling one executor hop per chunk and joining
        # a second full-size bytes object afterward.
        body = await loop.run_in_executor(None, _read_whole_file, resolved_path)

        self._body = body
        self._status = status
        # ``content-type`` is always set; Content-Length goes alongside
        # it on the success path so a request that never touched the
        # framework's ``setHeader`` still sends a valid framed response.
        # ``extra_headers`` arrives as bytes tuples (the contract shared
        # with the ASGI mirror) — decoded once here at the protocol
        # boundary because granian's RSGI requires native str pairs.
        headers = [("content-type", content_type.decode("latin-1"))]
        if file_size is not None:
            headers.append(("content-length", str(len(body))))
        headers.extend(
            (k.decode("latin-1"), v.decode("latin-1"))
            for k, v in extra_headers
        )
        self._hdr = headers
        self._sent = True
        # Body assembled and marked sent -- drain finishers.
        _drain_finishers(self)
