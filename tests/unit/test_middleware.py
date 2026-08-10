import logging

from flask import Flask, jsonify

from ainas.agent_access import AgentAccessRepository
from ainas.config import Settings
from ainas.errors import AppError
from ainas.middleware import SimpleRateLimiter, install_middleware
from ainas.permissions import is_authorized, require_scope


def _app(settings, agents=None):
    app = Flask(__name__)
    app.testing = True
    install_middleware(app, settings, agents)

    @app.errorhandler(AppError)
    def app_error(error):
        return jsonify({"error": error.code}), error.status_code

    @app.get("/api/read")
    def read():
        require_scope("read")
        return jsonify({"ok": True})

    @app.post("/api/write")
    def write():
        require_scope("write")
        return jsonify({"ok": True})

    return app


def test_api_without_key_fails_closed_unless_insecure_lan_is_explicit(tmp_path):
    closed = _app(Settings(data_dir=tmp_path, scheduler_enabled=False))
    assert closed.test_client().get("/api/read").status_code == 401

    opted_in = _app(Settings(data_dir=tmp_path / "insecure", scheduler_enabled=False, allow_insecure_lan=True))
    assert opted_in.test_client().get("/api/read").status_code == 200


def test_placeholder_key_is_rejected(tmp_path):
    app = _app(Settings(data_dir=tmp_path, scheduler_enabled=False, api_key="change-me"))
    assert app.test_client().get("/api/read", headers={"X-Api-Key": "change-me"}).status_code == 401

    weak = _app(Settings(data_dir=tmp_path / "weak", scheduler_enabled=False, api_key="short-key"))
    assert weak.test_client().get("/api/read", headers={"X-Api-Key": "short-key"}).status_code == 401

    documented_placeholder = "replace-with-a-long-random-string"
    example = _app(
        Settings(data_dir=tmp_path / "example", scheduler_enabled=False, api_key=documented_placeholder)
    )
    assert example.test_client().get(
        "/api/read", headers={"X-Api-Key": documented_placeholder}
    ).status_code == 401


def test_admin_bypasses_scopes_and_agents_are_scoped(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False, api_key="unit-secure-api-key")
    agents = AgentAccessRepository(settings)
    created = agents.create("read-only", scopes=["read"])
    app = _app(settings, agents)
    client = app.test_client()

    assert client.post("/api/write", headers={"X-Api-Key": "unit-secure-api-key"}).status_code == 200
    assert client.get("/api/read", headers={"Authorization": f"Bearer {created['token']}"}).status_code == 200
    assert client.post("/api/write", headers={"Authorization": f"Bearer {created['token']}"}).status_code == 403
    assert is_authorized("admin", [], "anything") is True


def test_rate_limiter_uses_bounded_source_buckets_not_fake_keys():
    limiter = SimpleRateLimiter(limit=10, max_keys=2)
    limiter.consume("192.0.2.1")
    limiter.consume("192.0.2.2")
    limiter.consume("192.0.2.3")
    assert len(limiter._requests) <= 3  # two sources plus the shared overflow bucket


def test_fake_api_keys_cannot_bypass_source_rate_limit(tmp_path):
    app = _app(
        Settings(
            data_dir=tmp_path,
            scheduler_enabled=False,
            api_key="unit-secure-api-key",
            rate_limit_per_minute=2,
        )
    )
    client = app.test_client()
    assert client.get("/api/read", headers={"X-Api-Key": "fake-one"}).status_code == 401
    assert client.get("/api/read", headers={"X-Api-Key": "fake-two"}).status_code == 401
    assert client.get("/api/read", headers={"X-Api-Key": "fake-three"}).status_code == 429


def test_mutation_audit_has_request_and_actor_fields_without_body(tmp_path, caplog):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False, api_key="unit-secure-api-key")
    app = _app(settings)
    caplog.set_level(logging.INFO, logger="ainas.middleware")

    response = app.test_client().post(
        "/api/write",
        json={"not": "recorded"},
        headers={"X-Api-Key": "unit-secure-api-key", "X-Request-Id": "audit-123"},
    )
    assert response.status_code == 200
    record = next(record for record in caplog.records if record.message == "api_mutation")
    assert record.request_id == "audit-123"
    assert record.actor_kind == "admin"
    assert record.actor_id == "admin"
    assert not hasattr(record, "body")
