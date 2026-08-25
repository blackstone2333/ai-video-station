from __future__ import annotations

import httpx
import pytest

from avs_mcp_adapter.client import AVSClient, AVSClientError, AVSSettings


def settings(token: str = "avs_agent_example") -> AVSSettings:
    return AVSSettings("http://avs.test", token, timeout_seconds=4)


@pytest.mark.asyncio
async def test_agent_token_and_success_request_id_are_forwarded() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer avs_agent_example"
        assert "X-Api-Key" not in request.headers
        assert request.url.path == "/api/search"
        return httpx.Response(200, json={"results": []}, headers={"X-Request-Id": "request-42"})

    client = AVSClient(settings(), transport=httpx.MockTransport(handler))
    assert await client.request("POST", "/api/search", json={"keyword": "Dune"}) == {"results": [], "request_id": "request-42"}
    await client.aclose()


@pytest.mark.asyncio
async def test_admin_key_uses_only_x_api_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "admin-key"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"tasks": []})

    client = AVSClient(settings("admin-key"), transport=httpx.MockTransport(handler))
    assert (await client.request("GET", "/api/downloader/tasks"))["tasks"] == []
    await client.aclose()


@pytest.mark.asyncio
async def test_problem_response_preserves_request_id_and_field_errors() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "title": "Validation failed",
                "detail": "keyword is required",
                "request_id": "body-id",
                "errors": [{"field": "keyword", "code": "REQUIRED"}],
            },
            headers={"X-Request-Id": "header-id"},
        )

    client = AVSClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(AVSClientError) as raised:
        await client.request("POST", "/api/search", json={})
    assert raised.value.to_dict() == {
        "status": 422,
        "title": "Validation failed",
        "detail": "keyword is required",
        "request_id": "body-id",
        "errors": [{"field": "keyword", "code": "REQUIRED"}],
    }
    await client.aclose()


def test_environment_requires_url_and_token_and_validates_timeout() -> None:
    with pytest.raises(ValueError, match="AVS_URL"):
        AVSSettings.from_env({"AVS_TOKEN": "x"})
    with pytest.raises(ValueError, match="AVS_TOKEN"):
        AVSSettings.from_env({"AVS_URL": "http://localhost:16666"})
    with pytest.raises(ValueError, match="at most 300"):
        AVSSettings.from_env({"AVS_URL": "http://localhost:16666", "AVS_TOKEN": "x", "AVS_TIMEOUT_SECONDS": "301"})
