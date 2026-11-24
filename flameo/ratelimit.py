import hashlib
import inspect
import math


class RateLimitSpec:
    __slots__ = ("count", "seconds", "key_function")

    def __init__(self, count, seconds, key_function=None):
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("[flameo] rate limit count must be a positive integer")
        try:
            valid_seconds = (
                not isinstance(seconds, bool)
                and isinstance(seconds, (int, float))
                and math.isfinite(float(seconds))
                and seconds > 0
                and seconds <= (2**63 - 1) / 1000
            )
        except (OverflowError, TypeError, ValueError):
            valid_seconds = False
        if not valid_seconds:
            raise ValueError("[flameo] rate limit seconds must be a positive number")
        if key_function is not None and not callable(key_function):
            raise TypeError("[flameo] key_function must be callable or None")
        self.count = count
        self.seconds = seconds
        self.key_function = key_function

    async def check(self, req, res, host, method, path):
        identity = "all"
        if self.key_function is not None:
            identity_value = self.key_function(req)
            if inspect.isawaitable(identity_value):
                identity_value = await identity_value
            identity = hashlib.sha256(str(identity_value).encode("utf-8")).hexdigest()

        prefix = getattr(req, "_base_url", "") or ""
        endpoint = f"{prefix}{path}"
        key = f"flameo:ratelimit:{method.upper()}:{endpoint}:{identity}"
        store = host.CACHE if host.CACHE is not None else host._memory_rate_limit
        window_ms = max(1, math.ceil(float(self.seconds) * 1000))
        current, remaining_ms = await store.rate_limit_hit(key, window_ms)
        if current <= self.count:
            return True

        retry_after = max(1, math.ceil(remaining_ms / 1000))
        res.setHeader("Retry-After", str(retry_after))
        await res.json({"error": "Too Many Requests"}, status=429)
        return False
