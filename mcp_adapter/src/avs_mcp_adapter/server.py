"""Official MCP SDK v2 stdio server entry point."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from .client import AVSClient, AVSSettings
from .tools import MCPToolBindings, MediaType, TOOL_SPECS, tool_result


def _sdk_annotations(spec: Any) -> Any:
    """Create the official SDK's annotation model from our testable metadata."""
    from mcp.types import ToolAnnotations  # Imported lazily to keep REST tests isolated.

    annotation = spec.annotations
    return ToolAnnotations(
        readOnlyHint=annotation.read_only,
        destructiveHint=annotation.destructive,
        idempotentHint=annotation.idempotent,
        openWorldHint=annotation.open_world,
    )


def create_server(client: AVSClient | None = None) -> Any:
    """Build the stdio server using MCP SDK v2's ``MCPServer`` API."""
    from mcp.server import MCPServer

    avs_client = client or AVSClient(AVSSettings.from_env())
    tools = MCPToolBindings(avs_client)
    specs = {spec.name: spec for spec in TOOL_SPECS}

    @asynccontextmanager
    async def lifespan(_: Any):
        try:
            yield None
        finally:
            await avs_client.aclose()

    server = MCPServer(name="ai_video_station_mcp", title="AI Video Station", lifespan=lifespan)

    @server.tool(
        name="avs_search_media",
        description=specs["avs_search_media"].description,
        annotations=_sdk_annotations(specs["avs_search_media"]),
    )
    async def avs_search_media(keyword: str, media_type: MediaType = "auto") -> dict[str, Any]:
        """Search AVS sources without creating a watchlist item."""
        return await tool_result(tools.search_media, keyword, media_type)

    @server.tool(
        name="avs_add_download",
        description=specs["avs_add_download"].description,
        annotations=_sdk_annotations(specs["avs_add_download"]),
    )
    async def avs_add_download(
        result_id: str,
        download_link: str,
        title: str,
        media_type: MediaType = "auto",
        path_rule_id: str | None = None,
    ) -> dict[str, Any]:
        """Add a selected search result to AVS's configured downloader."""
        return await tool_result(tools.add_download, result_id, download_link, title, media_type, path_rule_id)

    @server.tool(
        name="avs_list_downloads",
        description=specs["avs_list_downloads"].description,
        annotations=_sdk_annotations(specs["avs_list_downloads"]),
    )
    async def avs_list_downloads() -> dict[str, Any]:
        return await tool_result(tools.list_downloads)

    @server.tool(
        name="avs_list_naming_jobs",
        description=specs["avs_list_naming_jobs"].description,
        annotations=_sdk_annotations(specs["avs_list_naming_jobs"]),
    )
    async def avs_list_naming_jobs(page: int = 1, per_page: int = 20, status: str | None = None) -> dict[str, Any]:
        return await tool_result(tools.list_naming_jobs, page, per_page, status)

    @server.tool(
        name="avs_retry_naming_job",
        description=specs["avs_retry_naming_job"].description,
        annotations=_sdk_annotations(specs["avs_retry_naming_job"]),
    )
    async def avs_retry_naming_job(job_id: str) -> dict[str, Any]:
        return await tool_result(tools.retry_naming_job, job_id)

    @server.tool(
        name="avs_list_hardlinks",
        description=specs["avs_list_hardlinks"].description,
        annotations=_sdk_annotations(specs["avs_list_hardlinks"]),
    )
    async def avs_list_hardlinks(status: str = "all", page: int = 1, per_page: int = 20) -> dict[str, Any]:
        return await tool_result(tools.list_hardlinks, status, page, per_page)

    @server.tool(
        name="avs_list_watchlist",
        description=specs["avs_list_watchlist"].description,
        annotations=_sdk_annotations(specs["avs_list_watchlist"]),
    )
    async def avs_list_watchlist() -> dict[str, Any]:
        return await tool_result(tools.list_watchlist)

    @server.tool(
        name="avs_add_watchlist",
        description=specs["avs_add_watchlist"].description,
        annotations=_sdk_annotations(specs["avs_add_watchlist"]),
    )
    async def avs_add_watchlist(
        keyword: str, media_type: MediaType = "auto", path_rule_id: str | None = None
    ) -> dict[str, Any]:
        return await tool_result(tools.add_watchlist, keyword, media_type, path_rule_id)

    @server.tool(
        name="avs_check_watchlist",
        description=specs["avs_check_watchlist"].description,
        annotations=_sdk_annotations(specs["avs_check_watchlist"]),
    )
    async def avs_check_watchlist(item_id: str | None = None) -> dict[str, Any]:
        return await tool_result(tools.check_watchlist, item_id)

    return server


def main() -> None:
    """Run only the MCP SDK's default stdio transport; no HTTP listener is created."""
    server = create_server()
    result = server.run()
    if asyncio.iscoroutine(result):
        asyncio.run(result)


if __name__ == "__main__":  # pragma: no cover
    main()
