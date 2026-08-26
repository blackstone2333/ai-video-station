"""Minimal asynchronous REST client used by the MCP tools.

This module deliberately has no knowledge of providers, download clients, paths, or
media files.  AVS remains the only owner of that business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx


DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AVSSettings:
    """Connection settings loaded only from the adapter process environment."""

    url: str
    token: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AVSSettings":
        values = os.environ if environ is None else environ
        url = values.get("AVS_URL", "").strip().rstrip("/")
        token = values.get("AVS_TOKEN", "").strip()
        raw_timeout = values.get("AVS_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
        if not url:
            raise ValueError("AVS_URL must be set (for example http://127.0.0.1:16666)")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AVS_URL must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("AVS_URL must not contain credentials")
        if not token:
            raise ValueError("AVS_TOKEN must be set to an Agent token or administrator API key")
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("AVS_TIMEOUT_SECONDS must be a number") from exc
        if not 0 < timeout_seconds <= 300:
            raise ValueError("AVS_TIMEOUT_SECONDS must be greater than 0 and at most 300")
        return cls(url=url, token=token, timeout_seconds=timeout_seconds)


class AVSClientError(RuntimeError):
    """A stable, structured error representation for a REST call."""

    def __init__(
        self,
        *,
        status: int,
        title: str,
        detail: str,
        request_id: str | None = None,
        errors: list[Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.request_id = request_id
        self.errors = errors or []

    def to_dict(self) -> dict[str, Any]:
        """Return fields agents need for retries and user-facing diagnostics."""
        result: dict[str, Any] = {
            "status": self.status,
            "title": self.title,
            "detail": self.detail,
            "errors": self.errors,
        }
        if self.request_id:
            result["request_id"] = self.request_id
        return result


class AVSClient:
    """Async client for the small, fixed AVS API surface exposed by this adapter."""

    def __init__(
        self,
        settings: AVSSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "avs-mcp-adapter/0.2.0",
                **self._auth_headers(settings.token),
            },
        )

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        # AVS's revocable agent credentials must use Bearer.  A non-agent token is
        # deliberately sent only as the administrator key, never in both headers.
        if token.startswith("avs_agent_"):
            return {"Authorization": f"Bearer {token}"}
        return {"X-Api-Key": token}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call one AVS path and preserve API problem details in failures."""
        if not path.startswith("/api/"):
            raise ValueError("adapter requests must target a fixed /api/ path")
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise AVSClientError(status=504, title="AVS timeout", detail="AVS did not respond before the configured timeout") from exc
        except httpx.HTTPError as exc:
            raise AVSClientError(status=502, title="AVS unavailable", detail="could not connect to AVS") from exc

        request_id = response.headers.get("X-Request-Id")
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None

        if response.is_error:
            problem = body if isinstance(body, dict) else {}
            errors = problem.get("errors")
            raise AVSClientError(
                status=response.status_code,
                title=str(problem.get("title") or f"AVS request failed ({response.status_code})"),
                detail=str(problem.get("detail") or "AVS returned an error response"),
                request_id=str(problem.get("request_id") or request_id) if (problem.get("request_id") or request_id) else None,
                errors=errors if isinstance(errors, list) else [],
            )

        if response.status_code == 204:
            return {"success": True, "status_code": 204, **({"request_id": request_id} if request_id else {})}
        if not isinstance(body, dict):
            raise AVSClientError(
                status=502,
                title="Invalid AVS response",
                detail="AVS returned a successful response without a JSON object",
                request_id=request_id,
            )
        # Do not replace a body-provided request_id.  The header is retained when
        # AVS did not include one in its JSON response.
        if request_id and "request_id" not in body:
            return {**body, "request_id": request_id}
        return body
