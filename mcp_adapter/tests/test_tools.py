from __future__ import annotations

import json

import httpx
import pytest

from avs_mcp_adapter.client import AVSClient, AVSSettings
from avs_mcp_adapter.tools import MCPToolBindings, TOOL_SPECS, tool_result


def make_client(handler):
    return AVSClient(AVSSettings("http://avs.test", "avs_agent_test"), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_search_forces_read_only_watchlist_flag() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/search"
        assert json.loads(request.content) == {"keyword": "The Bear", "type": "tv", "add_to_watchlist": False}
        return httpx.Response(200, json={"results": []})

    client = make_client(handler)
    assert await MCPToolBindings(client).search_media("The Bear", "tv") == {"results": []}
    await client.aclose()


@pytest.mark.asyncio
async def test_list_tools_translate_pagination_and_filters() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"items": []})

    client = make_client(handler)
    bindings = MCPToolBindings(client)
    await bindings.list_naming_jobs(page=2, per_page=50, status="failed")
    await bindings.list_hardlinks(status="done", page=3, per_page=10)
    await bindings.list_cleanup_plans(page=4, per_page=25, status="ready")
    assert calls == [
        "http://avs.test/api/naming/jobs?page=2&per_page=50&status=failed",
        "http://avs.test/api/hardlinks?status=done&page=3&per_page=10",
        "http://avs.test/api/cleanup/plans?page=4&per_page=25&status=ready",
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_mutation_payloads_do_not_include_unset_path_rules() -> None:
    received: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"success": True})

    client = make_client(handler)
    bindings = MCPToolBindings(client)
    await bindings.add_download("result1234", "magnet:?xt=urn:btih:test", "Example")
    await bindings.add_watchlist("Example", "movie")
    await bindings.update_watchlist("item-123", "collection")
    await bindings.preview_manual_download("magnet:?xt=urn:btih:preview", "Example", "movie")
    await bindings.check_watchlist()
    assert received == [
        ("/api/download", {"result_id": "result1234", "download_link": "magnet:?xt=urn:btih:test", "title": "Example", "type": "auto"}),
        ("/api/watchlist/add", {"keyword": "Example", "type": "movie", "viewing_mode": "daily"}),
        ("/api/watchlist/item-123", {"viewing_mode": "collection"}),
        ("/api/download/manual/preview", {"download_link": "magnet:?xt=urn:btih:preview", "type": "movie", "title": "Example"}),
        ("/api/watchlist/check", {}),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_tool_result_keeps_structured_api_errors() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"title": "Forbidden", "detail": "missing scope", "request_id": "r-1", "errors": []})

    client = make_client(handler)
    response = await tool_result(MCPToolBindings(client).list_watchlist)
    assert response == {"ok": False, "error": {"status": 403, "title": "Forbidden", "detail": "missing scope", "request_id": "r-1", "errors": []}}
    await client.aclose()


@pytest.mark.asyncio
async def test_cleanup_tools_keep_scan_safe_and_execution_explicit() -> None:
    received: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append((request.method, request.url.path, json.loads(request.content) if request.content else {}))
        return httpx.Response(200, json={"success": True})

    client = make_client(handler)
    bindings = MCPToolBindings(client)
    await bindings.scan_duplicates("space_first", "tv")
    await bindings.get_cleanup_plan("plan-1")
    await bindings.execute_cleanup_plan(
        "plan-1",
        [{"group_id": "group-1", "delete_version_ids": ["version-1"]}],
        "DELETE_SELECTED_DUPLICATES",
        True,
    )
    await bindings.retry_cleanup_plan("plan-1")
    assert received == [
        ("POST", "/api/cleanup/scan", {"policy": "space_first", "media_type": "tv"}),
        ("GET", "/api/cleanup/plans/plan-1", {}),
        ("POST", "/api/cleanup/plans/plan-1/execute", {"selections": [{"group_id": "group-1", "delete_version_ids": ["version-1"]}], "delete_source": True, "confirmation": "DELETE_SELECTED_DUPLICATES"}),
        ("POST", "/api/cleanup/plans/plan-1/retry", {}),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_cleanup_execution_rejects_ambiguous_selection_before_rest_call() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid cleanup input must not reach AVS")

    client = make_client(handler)
    response = await tool_result(
        MCPToolBindings(client).execute_cleanup_plan,
        "plan-1",
        [{"group_id": "group-1", "delete_version_ids": []}],
        "DELETE_SELECTED_DUPLICATES",
    )
    assert response["ok"] is False
    assert response["error"]["status"] == 400
    await client.aclose()


def test_allow_list_and_annotations_cover_the_declared_safe_surface() -> None:
    names = {item.name for item in TOOL_SPECS}
    assert names == {
        "avs_search_media", "avs_add_download", "avs_list_downloads", "avs_list_naming_jobs",
        "avs_retry_naming_job", "avs_list_hardlinks", "avs_list_watchlist", "avs_add_watchlist", "avs_check_watchlist",
        "avs_preview_manual_download", "avs_add_manual_download", "avs_update_watchlist",
        "avs_scan_duplicates", "avs_list_cleanup_plans", "avs_get_cleanup_plan",
        "avs_execute_cleanup_plan", "avs_retry_cleanup_plan",
    }
    annotations = {item.name: item.annotations for item in TOOL_SPECS}
    assert annotations["avs_search_media"].read_only is True
    assert annotations["avs_list_hardlinks"].idempotent is True
    assert annotations["avs_add_watchlist"].idempotent is True
    assert annotations["avs_add_download"].destructive is False
    assert annotations["avs_check_watchlist"].open_world is True
    assert annotations["avs_execute_cleanup_plan"].destructive is True
    assert annotations["avs_scan_duplicates"].destructive is False
