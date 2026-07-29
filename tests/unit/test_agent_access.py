import json

from ainas.agent_access import AgentAccessRepository
from ainas.config import Settings


def test_agent_tokens_are_one_time_hashed_revocable_and_track_presence(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False, agent_offline_minutes=10)
    repository = AgentAccessRepository(settings)
    created = repository.create("家庭 Agent")
    token = created["token"]
    assert token.startswith("avs_agent_")
    stored = json.loads(settings.agents_path.read_text())["items"][0]
    assert token not in settings.agents_path.read_text()
    assert len(stored["token_hash"]) == 64
    assert repository.authenticate("avs_agent_wrong") is None

    authenticated = repository.authenticate(token, touch=False)
    assert authenticated["id"] == created["id"]
    connected = repository.connect(created["id"], "Codex", ["search", "download", "search"])
    assert connected["online"] is True
    assert connected["capabilities"] == ["search", "download"]
    listed = repository.list()[0]
    assert "token" not in listed and "token_hash" not in listed

    repository.revoke(created["id"])
    assert repository.authenticate(token) is None
    assert repository.list()[0]["revoked"] is True
