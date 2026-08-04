from __future__ import annotations

import json

from ainas.cli import _request, main


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
