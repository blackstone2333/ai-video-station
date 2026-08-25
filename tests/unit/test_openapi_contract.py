from __future__ import annotations

from pathlib import Path

import pytest


yaml = pytest.importorskip("yaml")
DOCUMENT = Path(__file__).parents[2] / "static" / "openapi.yaml"


def test_openapi_covers_actual_agent_routes_and_scopes():
    document = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))

    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "2.1.2"
    expected = {
        "/api/search": {"post"},
        "/api/download": {"post"},
        "/api/download/manual": {"post"},
        "/api/download/manual/preview": {"post"},
        "/api/downloader/status": {"get"},
        "/api/qb/status": {"get"},
        "/api/downloader/tasks": {"get"},
        "/api/qb/tasks": {"get"},
        "/api/downloader/tasks/{task_hash}/dismiss": {"post", "delete"},
        "/api/downloader/tasks/{task_hash}/recover": {"post"},
        "/api/downloader/tasks/{task_hash}/relocate": {"post"},
        "/api/watchlist": {"get"},
        "/api/watchlist/add": {"post"},
        "/api/watchlist/{item_id}": {"patch", "delete"},
        "/api/watchlist/check": {"post"},
        "/api/naming/jobs": {"get"},
        "/api/naming/jobs/{job_id}": {"get", "delete"},
        "/api/naming/jobs/{job_id}/preview": {"post"},
        "/api/naming/jobs/{job_id}/plan": {"patch"},
        "/api/naming/jobs/{job_id}/retry": {"post"},
        "/api/naming/jobs/check": {"post"},
        "/api/hardlinks": {"get"},
        "/api/logs": {"get"},
        "/api/settings/sites": {"get", "post"},
        "/api/settings/sites/preview": {"post"},
        "/api/settings/sites/{site_id}": {"patch", "delete"},
        "/api/settings/paths": {"get", "patch"},
        "/api/settings/path-rules": {"get", "post"},
        "/api/settings/path-rules/{rule_id}": {"patch", "delete"},
        "/api/settings/path-rules/{rule_id}/check": {"post"},
        "/api/settings/downloader": {"get", "patch"},
        "/api/settings/downloader/test": {"post"},
        "/api/settings/system": {"get", "patch"},
        "/api/agents": {"get"},
        "/api/agents/bootstrap": {"post"},
        "/api/agents/{agent_id}/scopes": {"patch"},
        "/api/agents/connect": {"post"},
        "/api/agents/heartbeat": {"post"},
        "/api/agents/{agent_id}": {"delete"},
    }
    paths = document["paths"]
    assert {path for path in paths if path.startswith("/api/")} == set(expected)
    for path, methods in expected.items():
        assert {method for method in paths[path] if method in {"get", "post", "patch", "delete"}} == methods
        for method in methods:
            operation = paths[path][method]
            assert operation["x-agent-scope"]
            assert "default" in operation["responses"]


def test_openapi_models_the_agent_contract_gaps_that_regressed_before():
    document = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    assert schemas["ManualTorrentRequest"]["required"] == ["torrent"]
    assert schemas["WatchlistAddRequest"]["required"] == ["keyword"]
    assert "path_rule_id" in schemas["WatchlistAddRequest"]["properties"]
    assert {"address_page", "allow_private_hosts"} <= set(schemas["Site"]["properties"])
    assert {"scopes", "token"} <= set(schemas["Agent"]["properties"])
    assert {"missing_in_downloader", "waiting_selection"} <= set(schemas["NamingStatus"]["enum"])
    assert {"request_id", "errors"} <= set(schemas["Problem"]["properties"])
    assert "Pagination" in schemas

    watchlist_schema = document["paths"]["/api/watchlist/add"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert watchlist_schema["$ref"].endswith("/WatchlistAddRequest")
    assert document["paths"]["/api/search"]["post"]["x-agent-scope"].startswith("search")


def test_openapi_strictly_models_patchable_path_and_downloader_settings():
    document = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    path_schema = schemas["PathSettingsPatchRequest"]
    assert path_schema["additionalProperties"] is False
    assert path_schema["minProperties"] == 1
    assert set(path_schema["properties"]) == {
        "qb_movie_category",
        "qb_tv_category",
        "qb_anime_category",
        "qb_custom_category",
        "downloads_base_path",
        "download_movie_path",
        "download_tv_path",
        "download_anime_path",
        "download_custom_path",
        "medialib_movie_path",
        "medialib_tv_path",
        "medialib_anime_path",
        "medialib_custom_path",
        "medialib_hardlink_enabled",
    }

    downloader_schema = schemas["DownloaderSettingsPatchRequest"]
    assert downloader_schema["additionalProperties"] is False
    assert downloader_schema["minProperties"] == 1
    assert set(downloader_schema["properties"]) == {
        "downloader_type",
        "qb_host",
        "qb_port",
        "qb_username",
        "qb_password",
        "qb_use_https",
        "qb_verify_ssl",
        "transmission_host",
        "transmission_port",
        "transmission_username",
        "transmission_password",
        "transmission_use_https",
        "transmission_verify_ssl",
        "transmission_rpc_path",
    }

    paths_ref = document["paths"]["/api/settings/paths"]["patch"]["requestBody"]["content"]["application/json"]["schema"]
    downloader_ref = document["paths"]["/api/settings/downloader"]["patch"]["requestBody"]["content"]["application/json"]["schema"]
    assert paths_ref == {"$ref": "#/components/schemas/PathSettingsPatchRequest"}
    assert downloader_ref == {"$ref": "#/components/schemas/DownloaderSettingsPatchRequest"}
