from __future__ import annotations

import json

import pytest

from ainas.state import StateStore, StateStoreError


def test_records_import_legacy_once_and_remain_ordered(tmp_path):
    legacy = tmp_path / "jobs.json"
    legacy.write_text(json.dumps({"items": [{"id": "a", "value": 1}, {"id": "b", "value": 2}]}))
    store = StateStore(tmp_path / "state.db")

    store.ensure_records("jobs", legacy_path=legacy)
    assert [item["id"] for item in store.list_records("jobs")] == ["a", "b"]

    legacy.write_text(json.dumps({"items": [{"id": "changed"}]}))
    store.ensure_records("jobs", legacy_path=legacy)
    assert [item["id"] for item in store.list_records("jobs")] == ["a", "b"]


def test_record_replacement_is_atomic_on_duplicate_id(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_records("jobs", default=[{"id": "safe"}])

    with pytest.raises(StateStoreError, match="duplicate record id"):
        store.replace_records("jobs", [{"id": "same"}, {"id": "same"}])

    assert store.list_records("jobs") == [{"id": "safe"}]


def test_document_round_trip_and_integrity(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_document("settings", default={"enabled": True})
    store.write_document("settings", {"enabled": False, "hours": 6})

    assert store.read_document("settings") == {"enabled": False, "hours": 6}
    assert store.integrity_check() == {
        "status": "ok",
        "detail": "ok",
        "schema_version": 1,
        "namespaces": 1,
    }


def test_invalid_legacy_state_fails_without_replacing_it(tmp_path):
    legacy = tmp_path / "broken.json"
    legacy.write_text("not-json")
    store = StateStore(tmp_path / "state.db")

    with pytest.raises(StateStoreError, match="legacy state cannot be imported"):
        store.ensure_records("broken", legacy_path=legacy)

    assert legacy.read_text() == "not-json"


def test_newer_namespace_schema_is_rejected(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.ensure_records("future_records", schema_version=2)
    store.ensure_document("future_document", schema_version=2)

    with pytest.raises(StateStoreError, match="newer than supported"):
        store.ensure_records("future_records", schema_version=1)
    with pytest.raises(StateStoreError, match="newer than supported"):
        store.ensure_document("future_document", schema_version=1)
