from __future__ import annotations

import hmac
import logging
import re
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from flask import Flask, g, request

from .config import Settings
from .errors import RateLimitError, UnauthorizedError


logger = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class SimpleRateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._requests: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def consume(self, key: str) -> Tuple[int, int]:
        now = time.monotonic()
        with self._lock:
            values = self._requests.get(key)
            if values is None:
                self._purge_expired(now)
                # A bounded shared bucket prevents unbounded memory use if clients
                # cycle through source identities (or a proxy passes them through).
                key = key if len(self._requests) < self.max_keys else "__overflow__"
                values = self._requests.setdefault(key, deque())
            while values and values[0] <= now - self.window_seconds:
                values.popleft()
            if len(values) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - values[0])))
                raise RateLimitError(retry_after)
            values.append(now)
            return self.limit - len(values), int(time.time() + self.window_seconds)

    def _purge_expired(self, now: float) -> None:
        expired = []
        for key, values in self._requests.items():
            while values and values[0] <= now - self.window_seconds:
                values.popleft()
            if not values:
                expired.append(key)
        for key in expired:
            del self._requests[key]


def install_middleware(app: Flask, settings: Settings, agents: Optional[Any] = None) -> None:
    limiter = SimpleRateLimiter(settings.rate_limit_per_minute)

    @app.before_request
    def before_request() -> None:
        supplied_request_id = request.headers.get("X-Request-Id", "")
        g.request_id = supplied_request_id if _REQUEST_ID_RE.fullmatch(supplied_request_id) else uuid.uuid4().hex
        g.started_at = time.monotonic()
        g.rate_remaining = settings.rate_limit_per_minute
        g.rate_reset = int(time.time() + 60)
        if not request.path.startswith("/api") or request.method == "OPTIONS":
            return
        # Never bucket by a caller-supplied API key: invalid keys otherwise create
        # unlimited buckets and can evade the limiter by changing their value.
        source = request.remote_addr or "unknown"
        g.rate_remaining, g.rate_reset = limiter.consume(source)
        expected = settings.api_key_value()
        g.auth_kind = None
        g.agent = None
        if not settings.has_usable_api_key():
            if not settings.allow_insecure_lan:
                raise UnauthorizedError()
            g.auth_kind = "admin"
        elif expected:
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
        if request.path.startswith("/api") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            actor_kind = getattr(g, "auth_kind", None) or "anonymous"
            agent = getattr(g, "agent", None) or {}
            logger.info(
                "api_mutation",
                extra={
                    "request_id": getattr(g, "request_id", None),
                    "actor_kind": actor_kind,
                    "actor_id": agent.get("id") if actor_kind == "agent" else actor_kind,
                    "method": request.method,
                    "path": request.path,
                    "status_code": response.status_code,
                },
            )
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
