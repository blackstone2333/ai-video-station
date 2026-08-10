"""The explicit, allow-listed MCP-to-REST tool bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from .client import AVSClient, AVSClientError


MediaType = Literal["auto", "movie", "tv", "anime", "custom"]
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolAnnotations:
    """MCP annotation values, kept SDK-independent for unit testing."""

    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    annotations: ToolAnnotations


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("avs_search_media", "Search enabled AVS media sources. This never adds a watchlist item.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_add_download", "Add one selected AVS search result to its configured download queue.", ToolAnnotations(False, False, False, True)),
    ToolSpec("avs_list_downloads", "List current AVS downloader tasks.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_list_naming_jobs", "List AVS naming jobs with optional status and pagination.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_retry_naming_job", "Retry an existing AVS naming and hardlink job.", ToolAnnotations(False, False, False, True)),
    ToolSpec("avs_list_hardlinks", "List AVS hardlink records with optional status and pagination.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_list_watchlist", "List AVS watchlist items.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_add_watchlist", "Add an AVS watchlist item; AVS returns an existing matching item when present.", ToolAnnotations(False, False, True, True)),
    ToolSpec("avs_check_watchlist", "Run an AVS watchlist check, optionally for one item.", ToolAnnotations(False, False, False, True)),
)


def _optional(value: Any) -> Any:
    return value if value is not None else None


def _page_params(page: int, per_page: int) -> dict[str, int]:
    if page < 1:
        raise ValueError("page must be at least 1")
    if not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")
    return {"page": page, "per_page": per_page}


class MCPToolBindings:
    """Async callables whose only responsibility is translating tool arguments."""

    def __init__(self, client: AVSClient) -> None:
        self.client = client

    async def search_media(self, keyword: str, media_type: MediaType = "auto") -> dict[str, Any]:
        return await self.client.request(
            "POST", "/api/search", json={"keyword": keyword, "type": media_type, "add_to_watchlist": False}
        )

    async def add_download(
        self,
        result_id: str,
        download_link: str,
        title: str,
        media_type: MediaType = "auto",
        path_rule_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "result_id": result_id,
            "download_link": download_link,
            "title": title,
            "type": media_type,
        }
        if path_rule_id is not None:
            body["path_rule_id"] = path_rule_id
        return await self.client.request("POST", "/api/download", json=body)

    async def list_downloads(self) -> dict[str, Any]:
        return await self.client.request("GET", "/api/downloader/tasks")

    async def list_naming_jobs(self, page: int = 1, per_page: int = 20, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = _page_params(page, per_page)
        if status:
            params["status"] = status
        return await self.client.request("GET", "/api/naming/jobs", params=params)

    async def retry_naming_job(self, job_id: str) -> dict[str, Any]:
        if not job_id.strip():
            raise ValueError("job_id must not be blank")
        return await self.client.request("POST", f"/api/naming/jobs/{job_id}/retry", json={})

    async def list_hardlinks(self, status: str = "all", page: int = 1, per_page: int = 20) -> dict[str, Any]:
        params: dict[str, Any] = {"status": status, **_page_params(page, per_page)}
        return await self.client.request("GET", "/api/hardlinks", params=params)

    async def list_watchlist(self) -> dict[str, Any]:
        return await self.client.request("GET", "/api/watchlist")

    async def add_watchlist(
        self, keyword: str, media_type: MediaType = "auto", path_rule_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"keyword": keyword, "type": media_type}
        if path_rule_id is not None:
            body["path_rule_id"] = path_rule_id
        return await self.client.request("POST", "/api/watchlist/add", json=body)

    async def check_watchlist(self, item_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if item_id is not None:
            body["item_id"] = item_id
        return await self.client.request("POST", "/api/watchlist/check", json=body)


async def tool_result(call: ToolHandler, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Return REST errors as structured MCP tool content, including request IDs."""
    try:
        return {"ok": True, "result": await call(*args, **kwargs)}
    except AVSClientError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except ValueError as exc:
        return {
            "ok": False,
            "error": {"status": 400, "title": "Invalid tool input", "detail": str(exc), "errors": []},
        }
