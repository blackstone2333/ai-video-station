from __future__ import annotations

import hmac
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional, Tuple

from flask import Flask, g, request

from .config import Settings
from .errors import RateLimitError, UnauthorizedError


class SimpleRateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def consume(self, key: str) -> Tuple[int, int]:
        now = time.monotonic()
        with self._lock:
            values = self._requests[key]
            while values and values[0] <= now - self.window_seconds:
                values.popleft()
            if len(values) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - values[0])))
                raise RateLimitError(retry_after)
            values.append(now)
            return self.limit - len(values), int(time.time() + self.window_seconds)


def install_middleware(app: Flask, settings: Settings, agents: Optional[Any] = None) -> None:
    limiter = SimpleRateLimiter(settings.rate_limit_per_minute)

    @app.before_request
    def before_request() -> None:
        g.request_id = request.headers.get("X-Request-Id", uuid.uuid4().hex)
        g.started_at = time.monotonic()
        g.rate_remaining = settings.rate_limit_per_minute
        g.rate_reset = int(time.time() + 60)
        if not request.path.startswith("/api") or request.method == "OPTIONS":
            return
        key = request.headers.get("X-Api-Key") or request.remote_addr or "unknown"
        g.rate_remaining, g.rate_reset = limiter.consume(key)
        expected = settings.api_key_value()
        g.auth_kind = "admin" if not expected else None
        g.agent = None
        if expected:
            authorization = request.headers.get("Authorization", "")
            supplied = request.headers.get("X-Api-Key", "")
            if authorization.lower().startswith("bearer "):
                supplied = authorization[7:].strip()
            if hmac.compare_digest(expected, supplied):
                g.auth_kind = "admin"
            elif agents:
                agent = agents.authenticate(supplied)
                if not agent:
                    raise UnauthorizedError()
                g.auth_kind = "agent"
                g.agent = agent
            else:
                raise UnauthorizedError()

    @app.after_request
    def after_request(response):
        response.headers["X-Request-Id"] = getattr(g, "request_id", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(getattr(g, "rate_remaining", settings.rate_limit_per_minute))
        response.headers["X-RateLimit-Reset"] = str(getattr(g, "rate_reset", int(time.time() + 60)))
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin in settings.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Api-Key, X-Request-Id"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        return response
