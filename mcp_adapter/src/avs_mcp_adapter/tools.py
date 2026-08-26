"""The explicit, allow-listed MCP-to-REST tool bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from .client import AVSClient, AVSClientError


MediaType = Literal["auto", "movie", "tv", "anime", "custom"]
ViewingMode = Literal["daily", "collection", "compact"]
CleanupPolicy = Literal["quality_first", "space_first"]
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
    ToolSpec("avs_preview_manual_download", "Identify and preview a manual magnet link without adding a downloader task.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_add_manual_download", "Add a previewed manual magnet link to AVS, with optional episodic subscription.", ToolAnnotations(False, False, False, True)),
    ToolSpec("avs_list_downloads", "List current AVS downloader tasks.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_list_naming_jobs", "List AVS naming jobs with optional status and pagination.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_retry_naming_job", "Retry an existing AVS naming and hardlink job.", ToolAnnotations(False, False, False, True)),
    ToolSpec("avs_list_hardlinks", "List AVS hardlink records with optional status and pagination.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_list_watchlist", "List AVS watchlist items.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_add_watchlist", "Add an AVS watchlist item; AVS returns an existing matching item when present.", ToolAnnotations(False, False, True, True)),
    ToolSpec("avs_update_watchlist", "Update an AVS watchlist viewing mode.", ToolAnnotations(False, False, True, True)),
    ToolSpec("avs_check_watchlist", "Run an AVS watchlist check, optionally for one item.", ToolAnnotations(False, False, False, True)),
    ToolSpec("avs_scan_duplicates", "Scan enabled AVS media paths and persist a non-destructive duplicate cleanup plan.", ToolAnnotations(False, False, False, True)),
    ToolSpec("avs_list_cleanup_plans", "List persisted AVS duplicate cleanup plans.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_get_cleanup_plan", "Get one AVS duplicate cleanup plan with its versions and paths.", ToolAnnotations(True, False, True, True)),
    ToolSpec("avs_execute_cleanup_plan", "Delete explicitly selected duplicate versions after AVS revalidates every path. This is destructive.", ToolAnnotations(False, True, False, True)),
    ToolSpec("avs_retry_cleanup_plan", "Retry only failed items from a previously confirmed cleanup execution. This can be destructive.", ToolAnnotations(False, True, False, True)),
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

    async def preview_manual_download(
        self,
        download_link: str,
        title: str | None = None,
        media_type: MediaType = "auto",
        path_rule_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"download_link": download_link, "type": media_type}
        if title is not None:
            body["title"] = title
        if path_rule_id is not None:
            body["path_rule_id"] = path_rule_id
        return await self.client.request("POST", "/api/download/manual/preview", json=body)

    async def add_manual_download(
        self,
        download_link: str,
        title: str | None = None,
        media_type: MediaType = "auto",
        path_rule_id: str | None = None,
        subscribe: bool = False,
        viewing_mode: ViewingMode = "daily",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"download_link": download_link, "type": media_type}
        if title is not None:
            body["title"] = title
        if path_rule_id is not None:
            body["path_rule_id"] = path_rule_id
        if subscribe:
            body["subscribe"] = True
            body["viewing_mode"] = viewing_mode
        return await self.client.request("POST", "/api/download/manual", json=body)

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
        self,
        keyword: str,
        media_type: MediaType = "auto",
        path_rule_id: str | None = None,
        viewing_mode: ViewingMode = "daily",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"keyword": keyword, "type": media_type, "viewing_mode": viewing_mode}
        if path_rule_id is not None:
            body["path_rule_id"] = path_rule_id
        return await self.client.request("POST", "/api/watchlist/add", json=body)

    async def update_watchlist(
        self, item_id: str, viewing_mode: ViewingMode
    ) -> dict[str, Any]:
        if not item_id.strip():
            raise ValueError("item_id must not be blank")
        return await self.client.request(
            "PATCH", f"/api/watchlist/{item_id}", json={"viewing_mode": viewing_mode}
        )

    async def check_watchlist(self, item_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if item_id is not None:
            body["item_id"] = item_id
        return await self.client.request("POST", "/api/watchlist/check", json=body)

    async def scan_duplicates(
        self,
        policy: CleanupPolicy = "quality_first",
        media_type: Literal["movie", "tv", "anime", "custom"] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"policy": policy}
        if media_type is not None:
            body["media_type"] = media_type
        return await self.client.request("POST", "/api/cleanup/scan", json=body)

    async def list_cleanup_plans(
        self, page: int = 1, per_page: int = 20, status: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = _page_params(page, per_page)
        if status:
            params["status"] = status
        return await self.client.request("GET", "/api/cleanup/plans", params=params)

    async def get_cleanup_plan(self, plan_id: str) -> dict[str, Any]:
        if not plan_id.strip() or "/" in plan_id:
            raise ValueError("plan_id must be a non-blank path segment")
        return await self.client.request("GET", f"/api/cleanup/plans/{plan_id}")

    async def execute_cleanup_plan(
        self,
        plan_id: str,
        selections: list[dict[str, Any]],
        confirmation: Literal["DELETE_SELECTED_DUPLICATES"],
        delete_source: bool = False,
    ) -> dict[str, Any]:
        if not plan_id.strip() or "/" in plan_id:
            raise ValueError("plan_id must be a non-blank path segment")
        if not selections:
            raise ValueError("selections must not be empty")
        normalized: list[dict[str, Any]] = []
        for selection in selections:
            if not isinstance(selection, dict):
                raise ValueError("each cleanup selection must be an object")
            group_id = selection.get("group_id")
            version_ids = selection.get("delete_version_ids")
            if not isinstance(group_id, str) or not group_id.strip():
                raise ValueError("each cleanup selection needs a group_id")
            if not isinstance(version_ids, list) or not version_ids or not all(isinstance(item, str) and item.strip() for item in version_ids):
                raise ValueError("each cleanup selection needs non-empty delete_version_ids")
            normalized.append({"group_id": group_id, "delete_version_ids": version_ids})
        return await self.client.request(
            "POST",
            f"/api/cleanup/plans/{plan_id}/execute",
            json={
                "selections": normalized,
                "delete_source": delete_source,
                "confirmation": confirmation,
            },
        )

    async def retry_cleanup_plan(self, plan_id: str) -> dict[str, Any]:
        if not plan_id.strip() or "/" in plan_id:
            raise ValueError("plan_id must be a non-blank path segment")
        return await self.client.request("POST", f"/api/cleanup/plans/{plan_id}/retry", json={})


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
