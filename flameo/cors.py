from typing import Any, Dict, Iterable, List, Union

_ALIASES: Dict[str, str] = {
    "allow_origins": "origins",
    "allow_credentials": "credentials",
    "allow_methods": "methods",
    "allow_headers": "headers",
    "expose_headers": "expose_headers",
    "exposeHeaders": "expose_headers",
    "max_age": "max_age",
    "maxAge": "max_age",
    "origins": "origins",
    "credentials": "credentials",
    "methods": "methods",
    "headers": "headers",
}

_DEFAULTS: Dict[str, Any] = {
    "origins": ["*"],
    "credentials": False,
    "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "QUERY"],
    "headers": ["*"],
    "expose_headers": [],
    "max_age": 600,
}


def _coerce_options(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the option dict from ``use_cors(**kwargs)``.

    Accepts both naming styles (``origins`` / ``allow_origins`` etc.) and
    applies defaults for any option the caller did not provide. Unknown
    keys are dropped silently.
    """
    out: Dict[str, Any] = dict(_DEFAULTS)
    if raw:
        for k, v in raw.items():
            canonical = _ALIASES.get(k)
            if canonical is None:
                continue
            out[canonical] = v
    return out


def _as_list(value: Union[str, Iterable[str], None]) -> List[str]:
    """Coerce a string / iterable / None into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        if "," in value and value.strip() != "*":
            return [piece.strip() for piece in value.split(",") if piece.strip()]
        return [value]
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


def _wildcard(items: List[str]) -> bool:
    return any(item == "*" for item in items)


def make_cors_middleware(app, **options):
    """Build the CORS middleware that ``app.use_cors`` installs.

    Returns an ``async (req, res)`` callable that:

      * echoes ``Access-Control-Allow-Origin`` (``*`` when ``origins`` is
        ``["*"]`` and credentials are off; the request Origin otherwise);
      * sets ``Access-Control-Allow-Credentials`` when credentials are
        on;
      * sets ``Access-Control-Expose-Headers`` when the user supplied
        any;
      * on ``OPTIONS`` preflight, sets the
        ``Access-Control-Allow-Methods`` / ``-Allow-Headers`` headers
        and ends the response with 204.
    """
    opts = _coerce_options(options)

    origins: List[str] = _as_list(opts.get("origins"))
    credentials: bool = bool(opts.get("credentials"))
    methods: List[str] = _as_list(opts.get("methods"))
    headers: List[str] = _as_list(opts.get("headers"))
    expose_headers: List[str] = _as_list(opts.get("expose_headers"))
    max_age = opts.get("max_age")

    if max_age is None:
        max_age_str = ""
    elif not isinstance(max_age, int) or max_age < 0:
        raise ValueError(
            "use_cors: 'max_age' must be a non-negative integer number of seconds."
        )
    else:
        max_age_str = str(max_age)

    allow_all_origins = _wildcard(origins)
    allow_all_headers = _wildcard(headers)
    allow_all_methods = _wildcard(methods)

    if credentials and allow_all_origins:
        raise ValueError(
            "use_cors: origins=['*'] cannot be combined with credentials=True; "
            "list explicit origins instead"
        )

    def _is_origin_allowed(origin: str) -> bool:
        if allow_all_origins:
            return True
        return origin in origins

    methods_csv = "*" if allow_all_methods else ", ".join(methods)
    headers_csv = "*" if allow_all_headers else ", ".join(headers)
    expose_csv = ", ".join(expose_headers)

    async def cors_middleware(req, res):
        request_origin = req.headers.get("origin") if getattr(req, "headers", None) else None

        # No Origin header → not a cross-origin request, skip entirely.
        if not request_origin:
            return

        if not _is_origin_allowed(request_origin):
            return

        # ``*`` is fine unless credentials are on — the spec forces us to
        # echo the specific origin in that case.
        if allow_all_origins and not credentials:
            allow_origin_value = "*"
        else:
            allow_origin_value = request_origin

        # Vary: Origin so shared caches don't leak responses between origins.
        existing_vary = res.headers.get("Vary") if hasattr(res, "headers") else None
        if existing_vary:
            vary_values = {v.strip().lower() for v in existing_vary.split(",") if v.strip()}
            vary_values.add("origin")
            res.setHeader("Vary", ", ".join(sorted(vary_values)))
        else:
            res.setHeader("Vary", "Origin")

        res.setHeader("Access-Control-Allow-Origin", allow_origin_value)

        if credentials:
            res.setHeader("Access-Control-Allow-Credentials", "true")

        if expose_csv:
            res.setHeader("Access-Control-Expose-Headers", expose_csv)

        # Preflight short-circuit.
        if req.method == "OPTIONS":
            res.setHeader("Access-Control-Allow-Methods", methods_csv)

            requested = req.headers.get("access-control-request-headers")
            if requested:
                if allow_all_headers:
                    allowed_headers = requested
                else:
                    allowed = {h.strip().lower() for h in headers}
                    asked = [h.strip().lower() for h in requested.split(",") if h.strip()]
                    allowed_headers = requested if all(h in allowed for h in asked) else headers_csv
                res.setHeader("Access-Control-Allow-Headers", allowed_headers)
            elif headers_csv and not (allow_all_headers and credentials):
                res.setHeader("Access-Control-Allow-Headers", headers_csv)

            if max_age_str:
                res.setHeader("Access-Control-Max-Age", max_age_str)

            await res.send("", status=204)
            return

        if max_age_str:
            res.setHeader("Access-Control-Max-Age", max_age_str)

    return cors_middleware