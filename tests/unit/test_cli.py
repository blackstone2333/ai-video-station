from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from ainas.cli import CliError, _request, main


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.value).encode()


def test_request_uses_agent_bearer_token():
    seen = {}

    def opener(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return FakeResponse({"success": True})

    result = _request(
        "http://nas.test:16666",
        "avs_agent_secret",
        "GET",
        "/api/downloader/status",
        opener=opener,
    )

    assert result == {"success": True}
    assert seen["request"].get_header("Authorization") == "Bearer avs_agent_secret"
    assert seen["request"].full_url == "http://nas.test:16666/api/downloader/status"


def test_main_can_call_generic_api_with_admin_key():
    output = []

    def opener(request, timeout):
        assert request.method == "POST"
        assert request.get_header("X-api-key") == "admin-secret"
        assert json.loads(request.data) == {"job_id": "job-1"}
        return FakeResponse({"checked": 1})

    exit_code = main(
        ["--url", "http://nas.test:16666", "request", "POST", "/api/naming/jobs/check", "--data", '{"job_id":"job-1"}'],
        environment={"AVS_TOKEN": "admin-secret"},
        opener=opener,
        output=output.append,
    )

    assert exit_code == 0
    assert json.loads(output[0]) == {"checked": 1}


def test_main_requires_token_for_api(capsys):
    assert main(["status"], environment={}) == 1
    assert "AVS_TOKEN" in capsys.readouterr().err


def test_main_search_defaults_to_read_only_payload():
    output = []

    def opener(request, timeout):
        assert request.method == "POST"
        assert request.full_url == "http://nas.test:16666/api/search"
        assert json.loads(request.data) == {"keyword": "沙丘", "type": "movie", "add_to_watchlist": False}
        return FakeResponse({"success": True})

    assert main(
        ["--url", "http://nas.test:16666", "search", "沙丘", "--type", "movie"],
        environment={"AVS_TOKEN": "avs_agent_secret"},
        opener=opener,
        output=output.append,
    ) == 0
    assert json.loads(output[0]) == {"success": True}


def test_main_high_frequency_commands_construct_typed_payloads():
    calls = []

    def opener(request, timeout):
        calls.append((request.method, request.full_url, json.loads(request.data) if request.data else None))
        return FakeResponse({"success": True})

    environment = {"AVS_TOKEN": "agent-token"}
    url = ["--url", "http://nas.test:16666"]
    assert main(url + ["download", "result-123", "magnet:?xt=urn:btih:abc", "示例", "--path-rule-id", "rule-123"], environment=environment, opener=opener) == 0
    assert main(url + ["manual-download", "magnet:?xt=urn:btih:def", "--original-title", "Example"], environment=environment, opener=opener) == 0
    assert main(url + ["watchlist-add", "星际迷航", "--type", "tv", "--path-rule-id", "rule-123"], environment=environment, opener=opener) == 0
    assert main(url + ["watchlist-update", "item-123", "--viewing-mode", "collection"], environment=environment, opener=opener) == 0
    assert main(url + ["watchlist-check", "--item-id", "item-123"], environment=environment, opener=opener) == 0
    assert main(url + ["naming-retry", "job-123"], environment=environment, opener=opener) == 0
    assert main(url + ["naming-check", "--job-id", "job-123"], environment=environment, opener=opener) == 0

    assert calls == [
        ("POST", "http://nas.test:16666/api/download", {"result_id": "result-123", "download_link": "magnet:?xt=urn:btih:abc", "title": "示例", "type": "auto", "path_rule_id": "rule-123"}),
        ("POST", "http://nas.test:16666/api/download/manual", {"download_link": "magnet:?xt=urn:btih:def", "type": "auto", "original_title": "Example"}),
        ("POST", "http://nas.test:16666/api/watchlist/add", {"keyword": "星际迷航", "type": "tv", "viewing_mode": "daily", "path_rule_id": "rule-123"}),
        ("PATCH", "http://nas.test:16666/api/watchlist/item-123", {"viewing_mode": "collection"}),
        ("POST", "http://nas.test:16666/api/watchlist/check", {"item_id": "item-123"}),
        ("POST", "http://nas.test:16666/api/naming/jobs/job-123/retry", None),
        ("POST", "http://nas.test:16666/api/naming/jobs/check", {"job_id": "job-123"}),
    ]


def test_problem_error_is_structured_and_never_echoes_agent_token(capsys):
    secret = "avs_agent_do-not-leak"

    def opener(request, timeout):
        body = json.dumps(
            {
                "type": "https://ai-video-station.local/errors/validation",
                "title": "Validation Error",
                "status": 422,
                "detail": "Request validation failed",
                "request_id": "req-123",
                "errors": [{"field": "keyword", "message": "required", "code": "missing"}],
            }
        ).encode()
        raise HTTPError(request.full_url, 422, "unprocessable", None, BytesIO(body))

    assert main(["search", "x"], environment={"AVS_TOKEN": secret}, opener=opener) == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "detail": "Request validation failed",
        "status": 422,
        "title": "Validation Error",
        "type": "https://ai-video-station.local/errors/validation",
        "errors": [{"field": "keyword", "message": "required", "code": "missing"}],
        "request_id": "req-123",
    }
    assert secret not in json.dumps(error)


def test_generic_request_rejects_cross_origin_url_before_adding_credentials(capsys):
    called = False

    def opener(request, timeout):
        nonlocal called
        called = True
        raise AssertionError("cross-origin request must not be opened")

    secret = "avs_agent_do-not-forward"
    result = main(
        ["request", "GET", "https://attacker.invalid/collect"],
        environment={"AVS_TOKEN": secret},
        opener=opener,
    )

    assert result == 1
    assert called is False
    error = json.loads(capsys.readouterr().err)
    assert "相对路径" in error["detail"]
    assert secret not in json.dumps(error)


@pytest.mark.parametrize(
    ("token", "header"),
    [
        ("avs_agent_do-not-forward", "Authorization"),
        ("admin-do-not-forward", "X-api-key"),
    ],
)
def test_default_opener_rejects_cross_origin_redirect_with_credentials(monkeypatch, token, header):
    seen = {}

    class RedirectingOpener:
        def __init__(self, handler):
            self.handler = handler

        def open(self, request, timeout):
            seen["request"] = request
            return self.handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.invalid/collect",
            )

    def fake_build_opener(handler):
        seen["handler"] = handler
        return RedirectingOpener(handler)

    monkeypatch.setattr("ainas.cli.build_opener", fake_build_opener)

    with pytest.raises(CliError, match="随重定向"):
        _request(
            "http://nas.test:16666",
            token,
            "GET",
            "/api/downloader/status",
        )

    assert seen["request"].get_header(header)


def test_same_origin_redirect_remains_supported():
    from ainas.cli import _SameOriginRedirectHandler

    request = Request(
        "http://nas.test:16666/api/downloader/status",
        headers={"Authorization": "Bearer avs_agent_secret"},
    )
    redirected = _SameOriginRedirectHandler(request.full_url).redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "/api/downloader/status/",
    )

    assert redirected is not None
    assert redirected.full_url == "http://nas.test:16666/api/downloader/status/"
    assert redirected.get_header("Authorization") == "Bearer avs_agent_secret"
