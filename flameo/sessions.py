from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional


_SESSION_COOKIE_NAME = "session"


def _b64url_encode(raw: bytes) -> str:
    """URL-safe base64 with stripped ``=`` padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Inverse of :func:`_b64url_encode`. Re-adds stripped padding."""
    pad = (-len(text)) % 4
    return base64.urlsafe_b64decode(text + ("=" * pad))


def _sign(secret_key: str, payload_b64: str, ts: int) -> bytes:
    """Compute the HMAC-SHA256 signature for a payload.

    The signed string is ``"<ts>.<b64>"`` so an attacker who flips the
    timestamp inside the payload can't reuse the old signature.
    """
    msg = f"{ts}.{payload_b64}".encode("ascii")
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).digest()


def _encode_session(secret_key: str, data: Dict[str, Any]) -> str:
    """Serialize ``data`` + signature into a cookie value."""
    ts = int(time.time())
    payload = json.dumps({"ts": ts, "data": data}, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    payload_b64 = _b64url_encode(payload)
    sig = _sign(secret_key, payload_b64, ts)
    return f"{payload_b64}.{_b64url_encode(sig)}"


def _decode_session(secret_key: str, cookie_value: str,
                    max_age: Optional[int]) -> Optional[Dict[str, Any]]:
    """Verify and unpack a signed session cookie.
    """
    if not cookie_value or "." not in cookie_value:
        return None
    payload_b64, _, sig_b64 = cookie_value.rpartition(".")
    if not payload_b64 or not sig_b64:
        return None

    try:
        sig = _b64url_decode(sig_b64)
        payload = _b64url_decode(payload_b64)
    except Exception:
        return None

    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(obj, dict):
        return None
    ts = obj.get("ts")
    data = obj.get("data")
    if not isinstance(ts, int) or not isinstance(data, dict):
        return None

    expected = _sign(secret_key, payload_b64, ts)
    # Constant-time compare so the verification cost doesn't leak which
    # byte failed the match.
    if not hmac.compare_digest(sig, expected):
        return None

    if max_age is not None and max_age > 0:
        if int(time.time()) - ts > max_age:
            return None

    return data


class Session(dict):
    """A ``dict`` that tracks whether it was mutated during the request.

    Behaves like a normal dict (``request.session["x"] = 1``,
    ``request.session.get("x")``, ``"x" in session``, iteration, etc.)
    but flips ``_dirty`` whenever a write happens. The session
    middleware inspects ``_dirty`` after the handler returns to decide
    whether to re-emit the cookie.

    Reads via ``session[key]`` for a missing key still raise ``KeyError``
    (matching common session behaviour, where ``session.foo`` on a missing
    property is undefined). Use ``.get(key)`` for a default.

    ``session.clear()`` and ``session.destroy()`` are aliases -- both
    drop every key and mark the session dirty so the cookie is expired
    on the response (``Set-Cookie: session=; Max-Age=0``).
    """

    __slots__ = ("_dirty",)

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        if initial:
            # Use ``update`` so subclasses (us) don't double-mark dirty
            # for the initial population. Direct __setitem__ would set
            # _dirty=True even when copying data we just decoded from
            # the cookie, which would force a needless Set-Cookie write
            # on every read.
            dict.update(self, initial)
        self._dirty = False

    def __setitem__(self, key, value) -> None:
        self._dirty = True
        super().__setitem__(key, value)

    def __delitem__(self, key) -> None:
        self._dirty = True
        super().__delitem__(key)

    def setdefault(self, key, default=None):
        if key not in self:
            self._dirty = True
            super().__setitem__(key, default)
        return self[key]

    def pop(self, key, *args):
        if key in self:
            self._dirty = True
        return super().pop(key, *args)

    def popitem(self):
        self._dirty = True
        return super().popitem()

    def clear(self) -> None:
        """Drop every key from the session.

        Marks the session dirty so the middleware will expire the
        cookie on the way out (``res.clearCookie(name, ...)`` with an expired date).
        """
        if len(self):
            self._dirty = True
        super().clear()

    # ``destroy`` is just an alias for ``clear`` -- some users expect
    # the short name. Both do the same job: drop every key and
    # mark the session dirty so the cookie is expired on response.
    destroy = clear

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        self._dirty = True
        super().update(*args, **kwargs)

    def mark_clean(self) -> None:
        """Reset the dirty flag (used by the middleware after writing)."""
        self._dirty = False


def _coerce_options(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the option dict from ``use_session(**kwargs)``.

    Accepts both snake_case and camelCase keys. Returns a canonical
    dict with only the keys the middleware actually consumes.
    """
    if not raw:
        return {}

    # camelCase -> snake_case map for every option we recognise.
    aliases = {
        "secret_key": "secret_key", "secretKey": "secret_key",
        "max_age": "max_age", "maxAge": "max_age",
        "https_only": "https_only", "httpsOnly": "https_only",
        "same_site": "same_site", "sameSite": "same_site",
        "path": "path",
        "name": "name",
    }

    out: Dict[str, Any] = {}
    for k, v in raw.items():
        canonical = aliases.get(k)
        if canonical is None:
            continue
        
        out[canonical] = v
    return out


def make_session_middleware(app, **options):
    """Build the session middleware that ``app.use_session`` installs.

    ``options`` is passed through ``_coerce_options`` first, so both
    ``secret_key=`` and ``secretKey=`` (and the other pairs) are
    accepted.
    """
    opts = _coerce_options(options)

    secret_key = opts.get("secret_key")
    if not isinstance(secret_key, str) or len(secret_key) < 10:
        raise ValueError(
            "use_session: 'secret_key' (or 'secretKey') is required and "
            "must be at least 10 characters long."
        )

    max_age = opts.get("max_age")
    if max_age is None:
        max_age = 86400 * 7  # 7 days
    elif not isinstance(max_age, int) or max_age < 0:
        raise ValueError(
            "use_session: 'max_age' (or 'maxAge') must be a non-negative "
            "integer number of seconds."
        )

    https_only = bool(opts.get("https_only", False))

    same_site = opts.get("same_site")
    if same_site is None:
        same_site = "lax"
    same_site = str(same_site).lower()
    if same_site not in ("lax", "strict", "none"):
        raise ValueError(
            "use_session: 'same_site' (or 'sameSite') must be one of "
            "'lax', 'strict', or 'none'."
        )
    if same_site == "none" and not https_only:
        import warnings
        warnings.warn(
            "use_session: sameSite='none' requires httpsOnly=True; the "
            "cookie will be rejected by browsers until you enable it.",
            UserWarning,
            stacklevel=3,
        )

    path = opts.get("path", "/")
    if not isinstance(path, str) or not path:
        raise ValueError("use_session: 'path' must be a non-empty string.")

    name = opts.get("name", _SESSION_COOKIE_NAME)
    if not isinstance(name, str) or not name:
        raise ValueError("use_session: 'name' must be a non-empty string.")

    async def session_middleware(req, res):
        """The per-request session middleware.

        Reads the existing signed cookie (if any), attaches a
        :class:`Session` to ``req``, and after the route handler
        returns, writes the updated cookie back if it changed.
        """
        cookies = req.cookies or {}
        existing = cookies.get(name)
        initial = _decode_session(secret_key, existing, max_age)
        if initial is None:
            initial = {}

        session = Session(initial)
        req.session = session

        def _commit_session() -> None:
            """Write the session cookie back to the response.

            Fired by the framework after the route handler completes,
            regardless of whether the handler called ``res.send`` /
            ``res.json`` / raised. We use ``res.cookie(...)`` so the
            Set-Cookie goes through the same path as any user-set
            cookie and respects the response's _sent guard.

            If the session is empty (e.g. user cleared it), expire the
            cookie instead so the browser drops it.
            """
            try:
                if not session._dirty:
                    # No mutations this request -> leave the cookie
                    # alone. A fresh, empty session that the user
                    # never wrote to produces NO Set-Cookie at all,
                    # which matches common cookie-session
                    # behaviour and avoids needless writes on /hello.
                    return
                if not session:
                    # Mutated then emptied -> expire the cookie.
                    # ``clearCookie`` here only touches OUR cookie name,
                    # never any unrelated ones the user may have set.
                    res.clearCookie(
                        name,
                        path=path,
                        httpOnly=True,
                        secure=https_only,
                        sameSite=same_site,
                    )
                    session.mark_clean()
                    return
                value = _encode_session(secret_key, dict(session))
                res.cookie(name, value, options={
                    "path": path,
                    "httpOnly": True,        # always httpOnly per spec
                    "secure": https_only,
                    "sameSite": same_site,
                    "maxAge": max_age,
                })
                session.mark_clean()
            except Exception:
                # Never let a cookie write failure blow up the response.
                # The user can still call res.cookie() manually.
                pass

        res._finalize_session__ = _commit_session

    return session_middleware
