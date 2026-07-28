from __future__ import annotations

from typing import Any, Dict, List, Optional


class AppError(Exception):
    def __init__(
        self,
        title: str,
        detail: str,
        code: str,
        status_code: int,
        errors: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(detail)
        self.title = title
        self.detail = detail
        self.code = code
        self.status_code = status_code
        self.errors = errors or []


class ValidationAppError(AppError):
    def __init__(self, detail: str, errors: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__("Validation Error", detail, "validation-error", 422, errors)


class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            "Not Found",
            f"{resource} not found: {resource_id}",
            "not-found",
            404,
        )


class UnauthorizedError(AppError):
    def __init__(self) -> None:
        super().__init__("Unauthorized", "API key is missing or invalid", "unauthorized", 401)


class RateLimitError(AppError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "Too Many Requests",
            f"Rate limit exceeded; retry after {retry_after} seconds",
            "rate-limit-exceeded",
            429,
        )
        self.retry_after = retry_after


class UpstreamError(AppError):
    def __init__(self, service: str, detail: str, timeout: bool = False) -> None:
        super().__init__(
            "Upstream Timeout" if timeout else "Upstream Service Error",
            f"{service}: {detail}",
            "upstream-timeout" if timeout else "upstream-error",
            504 if timeout else 502,
        )


class ServiceUnavailableError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__("Service Unavailable", detail, "service-unavailable", 503)
