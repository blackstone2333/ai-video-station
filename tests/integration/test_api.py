from __future__ import annotations

import io
import logging
from pathlib import Path

from ainas.app import create_app
from ainas.config import Settings
from ainas.download_records import DismissedDownloadRepository
from ainas.errors import ConflictError
from ainas.naming import NamingJobRepository, NamingPlan
from ainas.quality import build_release
from ainas.services import AppServices, ResultCache, SearchOutcome
from ainas.sites import SiteRepository
from ainas.state import StateStoreError
from ainas.watchlist import WatchlistRepository
from tests.unit.test_torrent_meta import TORRENT


MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
RELEASE = build_release(
    "奥本海默",
    "12.5GB 国英双语 1080p BluRay x264",
    "https://sixv.test/jddy/2023-11-09/46705.html",
    MAGNET,
    "movie",
)


class StubCrawler:
    def probe(self):
        return "https://sixv.test"


class StubQB:
    configured = True

    def __init__(self):
        self.added = []
        self.operations = []

    def status(self):
        return {"configured": True, "connected": True, "version": "4.6.7", "error": None}

    def tasks(self):
        return [{"hash": "abc", "name": "Movie", "progress": 0.5}]

    def torrent_info(self, hash_value):
        return None if hash_value == "missing" else {"hash": hash_value, "name": "Movie", "progress": 0.5}

    def recheck(self, hash_value):
        self.operations.append(("recheck", hash_value))

    def resume(self, hash_value):
        self.operations.append(("resume", hash_value))

    def set_location(self, hash_value, location):
        self.operations.append(("set_location", hash_value, str(location)))

    def add_download(self, link, category):
        self.added.append((link, category))
        return {"qb_task_id": "a" * 40, "category": category}


class StubSearch:
    def __init__(self, cache):
        self.cache = cache

    def search(self, keyword, media_type):
        releases = [] if keyword == "不存在" else [RELEASE]
        self.cache.put_all(releases)
        return SearchOutcome(keyword, "movie" if releases else media_type, releases)


class StubDownload:
    def __init__(self, qb):
        self.qb = qb
        self.manual_calls = []

    def download(self, result_id, link, title, media_type, path_rule_id=None):
        self.qb.add_download(link, "sixv-movie")
        return {"qb_task_id": "a" * 40, "category": "sixv-movie", "title": title, "type": "movie"}

    def preview_link(self, download_link, title, media_type, original_title=None, edition=None, episode_title=None, path_rule_id=None):
        self.manual_calls.append(("preview-link", download_link, title, media_type))
        return {"ready": True, "title": title or "Manual", "type": "tv", "naming": {"plan": {"root_name": "Manual"}}}

    def preview_torrent(self, content, filename, title, media_type, original_title=None, edition=None, episode_title=None, path_rule_id=None):
        self.manual_calls.append(("preview-torrent", filename, title, media_type))
        return {"ready": True, "title": title or "Movie.mkv", "type": "movie", "naming": {"plan": {"root_name": "Movie"}}}

    def manual_link(self, download_link, title, media_type, original_title=None, edition=None, episode_title=None, path_rule_id=None):
        self.manual_calls.append(("link", download_link, title, media_type))
        return {"qb_task_id": "b" * 40, "category": "Movie", "title": title or "Manual", "type": media_type}

    def manual_torrent(self, content, filename, title, media_type, original_title=None, edition=None, episode_title=None, path_rule_id=None):
        self.manual_calls.append(("torrent", filename, title, media_type))
        return {"qb_task_id": "c" * 40, "category": "Movie", "title": title or "Movie.mkv", "type": media_type}


class StubWatchlistService:
    def __init__(self, repository):
        self.repository = repository

    def add(self, keyword, media_type, path_rule_id=None, viewing_mode="daily", resource_preferences=None):
        return self.repository.add(
            keyword, media_type, path_rule_id, viewing_mode=viewing_mode,
            resource_preferences=resource_preferences,
        )

    def update(self, item_id, viewing_mode=None, resource_preferences=None):
        item = self.repository.get(item_id)
        return self.repository.update(
            item_id,
            {
                "viewing_mode": viewing_mode or item.get("viewing_mode", "daily"),
                "resource_preferences": resource_preferences or item.get("resource_preferences"),
            },
        )

    def check(self, item_id=None):
        return {"running": False, "checked": 1, "downloaded": 0, "results": []}


class StubNamingService:
    def __init__(self, repository):
        self.repository = repository

    def check(self, job_id=None):
        return {"running": False, "checked": 1, "completed": 1, "results": []}

    def discard_record(self, job_id):
        item = self.repository.get(job_id)
        naming_failure = item.get("status") in {"failed", "missing_in_downloader"}
        hardlink_unfinished = item.get("status") == "completed" and item.get("hardlink_status") in {
            "pending", "waiting_download", "retrying", "failed", "partial", "conflict"
        }
        if not naming_failure and not hardlink_unfinished:
            raise ConflictError("record cannot be discarded")
        return self.repository.delete(job_id)

    def preview_corrected_plan(self, job_id, overrides):
        return {
            "job_id": job_id,
            "plan": {"media_type": "movie", "media_name": overrides.get("media_name", "测试电影")},
            "preview": {"operations": []},
            "torrent_found": False,
        }

    def apply_corrected_plan(self, job_id, plan):
        return {"id": job_id, "status": "retrying", "plan": plan}


def build_test_app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        scheduler_enabled=False,
        api_key="integration-api-key-1234",
        rate_limit_per_minute=100,
        cors_origins="https://qclaw.test",
    )
    cache = ResultCache()
    qb = StubQB()
    watchlist = WatchlistRepository(settings.watchlist_path)
    search = StubSearch(cache)
    naming_jobs = NamingJobRepository(settings.naming_jobs_path)
    dismissed_downloads = DismissedDownloadRepository(settings.dismissed_downloads_path)
    sites = SiteRepository(
        settings.sites_path,
        {"id": "sixv", "name": "6v", "adapter": "sixv", "enabled": True, "base_urls": ["https://sixv.test"]},
    )
    services = AppServices(
        crawler=StubCrawler(),
        qb=qb,
        watchlist=watchlist,
        cache=cache,
        search=search,
        download=StubDownload(qb),
        watchlist_service=StubWatchlistService(watchlist),
        naming_jobs=naming_jobs,
        naming=StubNamingService(naming_jobs),
        sites=sites,
        dismissed_downloads=dismissed_downloads,
    )
    return create_app(settings, services, start_scheduler=False), services


def test_health_auth_search_download_and_qb(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/").location == "/admin"
    admin = client.get("/admin")
    assert admin.status_code == 200
    page = admin.get_data(as_text=True)
    assert 'id="header-theme-mode"' in page
    assert 'data-panel="logs"' in page
    assert 'id="refresh-downloads"' in page
    assert 'id="show-hidden-downloads"' in page
    assert 'id="hidden-downloads-modal"' in page
    assert 'id="path-rule-downloader"' in page
    assert 'id="job-detail-modal"' in page
    assert "每 20 秒" not in page
    assert "AI Video Station 媒体调度台" in admin.get_data(as_text=True)
    assert "路径设置" in admin.get_data(as_text=True)
    assert "frame-ancestors 'none'" in admin.headers["Content-Security-Policy"]
    assert client.get("/static/admin.css").status_code == 200
    assert client.get("/static/admin.js").status_code == 200
    assert client.get("/ready").json["status"] == "ok"
    assert client.post("/api/search", json={"keyword": "奥本海默", "type": "movie"}).status_code == 401

    headers = {"X-Api-Key": "integration-api-key-1234", "X-Request-Id": "request-1"}
    response = client.post("/api/search", json={"keyword": "《奥本海默》", "type": "movie"}, headers=headers)
    assert response.status_code == 200
    assert response.json["count"] == 1
    assert response.headers["X-Request-Id"] == "request-1"

    selected = response.json["results"][0]
    response = client.post(
        "/api/download",
        json={
            "result_id": selected["id"],
            "download_link": selected["download_link"],
            "title": selected["title"],
            "type": "movie",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json["qb_task_id"] == "a" * 40
    assert client.get("/api/qb/status", headers=headers).json["connected"] is True
    tasks = client.get("/api/qb/tasks", headers=headers).json
    assert tasks["count"] == 1
    assert tasks["synced_at"]


def test_ready_reports_persistent_state_failure_without_internal_details(tmp_path):
    app, services = build_test_app(tmp_path)

    class BrokenState:
        def integrity_check(self):
            raise StateStoreError("sensitive database path")

    services.state_store = BrokenState()
    response = app.test_client().get("/ready")

    assert response.status_code == 503
    assert response.json["checks"]["state"] == {
        "status": "error",
        "detail": "persistent state is unavailable",
    }


def test_download_records_can_be_hidden_restored_and_recovered(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}

    hidden = client.post("/api/downloader/tasks/abc/dismiss", headers=headers)
    assert hidden.status_code == 201
    tasks = client.get("/api/downloader/tasks", headers=headers).json
    assert tasks["count"] == 0
    assert tasks["hidden_count"] == 1
    assert tasks["hidden_tasks"][0]["name"] == "Movie"

    restored = client.delete("/api/downloader/tasks/abc/dismiss", headers=headers)
    assert restored.status_code == 204
    assert client.get("/api/downloader/tasks", headers=headers).json["count"] == 1

    recovered = client.post("/api/downloader/tasks/abc/recover", headers=headers)
    assert recovered.status_code == 202
    assert services.qb.operations == [("recheck", "abc"), ("resume", "abc")]
    assert client.post("/api/downloader/tasks/missing/dismiss", headers=headers).status_code == 404
    assert client.delete("/api/downloader/tasks/missing/dismiss", headers=headers).status_code == 404
    assert client.post("/api/downloader/tasks/missing/recover", headers=headers).status_code == 404
    assert services.qb.operations == [("recheck", "abc"), ("resume", "abc")]

    relocated = client.post(
        "/api/downloader/tasks/abc/relocate",
        json={"location": "/Downloads/TV"},
        headers=headers,
    )
    assert relocated.status_code == 202
    assert services.qb.operations[-1] == ("set_location", "abc", "/Downloads/TV")


def test_validation_search_is_read_only_and_watchlist_crud(tmp_path):
    app, _ = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"Authorization": "Bearer integration-api-key-1234", "Origin": "https://qclaw.test"}
    bad = client.post("/api/search", json={"keyword": "  ", "type": "wrong"}, headers=headers)
    assert bad.status_code == 422
    assert bad.content_type == "application/problem+json"
    assert len(bad.json["errors"]) == 2

    missing = client.post("/api/search", json={"keyword": "不存在", "type": "movie"}, headers=headers)
    assert missing.json["watchlist_exists"] is False
    assert missing.json["watchlist_added"] is False
    assert missing.json["watchlist_id"] is None
    assert client.get("/api/watchlist", headers=headers).json["count"] == 0
    assert missing.headers["Access-Control-Allow-Origin"] == "https://qclaw.test"

    opted_in = client.post(
        "/api/search",
        json={"keyword": "不存在", "type": "movie", "addto_watchlist": True},
        headers=headers,
    )
    assert opted_in.json["watchlist_exists"] is True
    assert opted_in.json["watchlist_added"] is True
    repeated = client.post(
        "/api/search",
        json={"keyword": "不存在", "type": "movie", "add_to_watchlist": True},
        headers=headers,
    )
    assert repeated.json["watchlist_added"] is False
    assert repeated.json["watchlist_id"] == opted_in.json["watchlist_id"]
    read_only_again = client.post(
        "/api/search",
        json={"keyword": "不存在", "type": "movie"},
        headers=headers,
    )
    assert read_only_again.json["watchlist_exists"] is True
    assert read_only_again.json["watchlist_added"] is False

    added = client.post("/api/watchlist/add", json={"keyword": "龙之家族", "type": "tv"}, headers=headers)
    assert added.status_code == 201
    item_id = added.json["item"]["id"]
    listing = client.get("/api/watchlist", headers=headers)
    assert listing.json["count"] == 2
    checked = client.post("/api/watchlist/check", json={"item_id": item_id}, headers=headers)
    assert checked.json["checked"] == 1
    changed = client.patch(
        f"/api/watchlist/{item_id}", json={"viewing_mode": "collection"}, headers=headers
    )
    assert changed.json["item"]["viewing_mode"] == "collection"
    assert client.delete(f"/api/watchlist/{item_id}", headers=headers).status_code == 204


def test_malformed_requests_and_not_found_use_problem_details(tmp_path):
    app, _ = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}
    wrong_type = client.post("/api/search", data="x", content_type="text/plain", headers=headers)
    assert wrong_type.status_code == 422
    malformed = client.post("/api/search", data="{", content_type="application/json", headers=headers)
    assert malformed.status_code == 422
    not_found = client.delete("/api/watchlist/not-found-id", headers=headers)
    assert not_found.status_code == 404
    assert not_found.json["title"] == "Not Found"


def test_naming_job_api_lists_gets_and_checks_jobs(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}
    plan = NamingPlan("movie", "大黄蜂", "大黄蜂 (2018)", 2018, None, None, "DHF.mp4")
    item = services.naming_jobs.upsert("a" * 40, plan, "sixv-movie")

    listing = client.get("/api/naming/jobs?page=1&per_page=10", headers=headers)
    assert listing.status_code == 200
    assert listing.json["pagination"]["total"] == 1
    assert client.get(f"/api/naming/jobs/{item['id']}", headers=headers).json["item"]["plan"]["root_name"] == "大黄蜂 (2018)"
    checked = client.post("/api/naming/jobs/check", json={"job_id": item["id"]}, headers=headers)
    assert checked.json["completed"] == 1
    assert client.get("/api/naming/jobs?page=bad", headers=headers).status_code == 422

    services.naming_jobs.update(
        item["id"],
        {"status": "failed", "attempts": 3, "last_error": "rename failed"},
    )
    retried = client.post(f"/api/naming/jobs/{item['id']}/retry", headers=headers)
    assert retried.status_code == 200
    assert retried.json["checked"] == 1

    deleted = client.delete(f"/api/naming/jobs/{item['id']}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/naming/jobs/{item['id']}", headers=headers).status_code == 404


def test_naming_job_delete_rejects_non_failed_records(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}
    plan = NamingPlan("movie", "大黄蜂", "大黄蜂 (2018)", 2018, None, None, "DHF.mp4")
    item = services.naming_jobs.upsert("a" * 40, plan, "sixv-movie")

    response = client.delete(f"/api/naming/jobs/{item['id']}", headers=headers)

    assert response.status_code == 409
    assert services.naming_jobs.get(item["id"])["status"] == "pending"


def test_naming_job_delete_accepts_missing_partial_and_conflict_records(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}
    plan = NamingPlan("movie", "大黄蜂", "大黄蜂 (2018)", 2018, None, None, "DHF.mp4")

    terminal_states = (
        {"status": "missing_in_downloader"},
        {"status": "completed", "hardlink_status": "partial"},
        {"status": "completed", "hardlink_status": "conflict"},
    )
    for index, changes in enumerate(terminal_states):
        item = services.naming_jobs.upsert(str(index + 1) * 40, plan, "sixv-movie")
        services.naming_jobs.update(item["id"], changes)

        response = client.delete(f"/api/naming/jobs/{item['id']}", headers=headers)

        assert response.status_code == 204
        assert client.get(f"/api/naming/jobs/{item['id']}", headers=headers).status_code == 404


def test_naming_job_delete_can_abandon_waiting_hardlink_record(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}
    plan = NamingPlan("movie", "手动下载", "手动下载", None, None, None, None)
    item = services.naming_jobs.upsert("c" * 40, plan, "Movie")
    services.naming_jobs.update(item["id"], {"status": "completed", "hardlink_status": "waiting_download"})

    response = client.delete(f"/api/naming/jobs/{item['id']}", headers=headers)

    assert response.status_code == 204
    assert client.get(f"/api/naming/jobs/{item['id']}", headers=headers).status_code == 404


def test_hardlink_history_and_site_settings_api(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}
    plan = NamingPlan("anime", "Rick and Morty", "Rick and Morty", None, 1, "S01E01", "01.mkv")
    job = services.naming_jobs.upsert("b" * 40, plan, "Anime")
    services.naming_jobs.update(
        job["id"],
        {
            "status": "completed",
            "hardlink_status": "done",
            "hardlink_result": {"target": "/medialib/video/anime/Rick and Morty", "linked": 1, "skipped": 0, "files": []},
        },
    )
    history = client.get("/api/hardlinks", headers=headers)
    assert history.status_code == 200
    assert history.json["items"][0]["type"] == "anime"
    assert history.json["items"][0]["linked"] == 1

    created = client.post(
        "/api/settings/sites",
        json={
            "name": "Generic",
            "adapter": "generic",
            "base_urls": ["https://media.test"],
            "search_url": "{base_url}/search?q={keyword}",
        },
        headers=headers,
    )
    assert created.status_code == 201
    site_id = created.json["item"]["id"]
    updated_site = client.patch(
        f"/api/settings/sites/{site_id}",
        json={"enabled": False, "allow_private_hosts": True},
        headers=headers,
    ).json["item"]
    assert updated_site["enabled"] is False
    assert updated_site["allow_private_hosts"] is True
    assert client.get("/api/settings/sites", headers=headers).json["count"] == 2
    assert client.delete(f"/api/settings/sites/{site_id}", headers=headers).status_code == 204


def test_logs_api_filters_and_redacts_tokens(tmp_path):
    app, _ = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}
    logging.getLogger("tests.diagnostics").error("hardlink failed for avs_agent_super-secret")

    response = client.get("/api/logs?level=error&query=hardlink&limit=10", headers=headers)

    assert response.status_code == 200
    item = next(value for value in response.json["items"] if value["logger"] == "tests.diagnostics")
    assert item["message"] == "hardlink failed for [REDACTED_AGENT_TOKEN]"
    assert client.get("/api/logs?level=nope", headers=headers).status_code == 422


def test_path_settings_api_get_patch_auth_and_validation(tmp_path):
    app, _ = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}

    assert client.get("/api/settings/paths").status_code == 401
    current = client.get("/api/settings/paths", headers=headers)
    assert current.status_code == 200
    assert current.json["settings"]["medialib_base_path"] == "/volume1/video"
    assert current.json["settings"]["medialib_mount_path"] == "/medialib"
    assert current.json["settings"]["qb_custom_category"] == "Custom"

    changed = client.patch(
        "/api/settings/paths",
        json={
            "downloads_base_path": "/volume1/video/Incoming",
            "download_movie_path": "/volume1/video/Incoming/Films",
            "download_tv_path": "/volume1/video/Incoming/Series",
            "download_anime_path": "/volume1/video/Incoming/Anime",
            "download_custom_path": "/volume1/video/Incoming/Learning",
            "medialib_movie_path": "/volume1/video/Library/Films",
            "medialib_tv_path": "/volume1/video/Library/Series",
            "medialib_anime_path": "/volume1/video/Library/Anime",
            "medialib_custom_path": "/volume1/video/Library/Learning",
            "qb_movie_category": "国外电影",
            "qb_tv_category": "电视剧",
            "qb_anime_category": "动漫",
            "qb_custom_category": "学习资料",
            "medialib_hardlink_enabled": False,
        },
        headers=headers,
    )
    assert changed.status_code == 200
    assert changed.json["settings"]["download_movie_path"] == "/volume1/video/Incoming/Films"
    assert changed.json["settings"]["medialib_hardlink_enabled"] is False
    assert changed.json["settings"]["download_custom_path"] == "/volume1/video/Incoming/Learning"
    assert changed.json["settings"]["qb_custom_category"] == "学习资料"
    assert (tmp_path / "path_settings.json").exists()

    relative = client.patch(
        "/api/settings/paths",
        json={"download_movie_path": "Incoming/Films"},
        headers=headers,
    )
    assert relative.status_code == 422
    assert relative.json["errors"][0]["field"] == "download_movie_path"
    outside = client.patch(
        "/api/settings/paths",
        json={"medialib_tv_path": "/another-volume/tv"},
        headers=headers,
    )
    assert outside.status_code == 422
    assert client.patch("/api/settings/paths", json={}, headers=headers).status_code == 422
    assert client.patch("/api/settings/paths", json={"medialib_base_path": "/tmp"}, headers=headers).status_code == 422
    duplicate = client.patch(
        "/api/settings/paths",
        json={"qb_custom_category": "国外电影"},
        headers=headers,
    )
    assert duplicate.status_code == 422


def test_manual_download_path_rules_and_runtime_settings_api(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "integration-api-key-1234"}

    magnet = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=Show.S01E01.mkv"
    preview = client.post(
        "/api/download/manual/preview",
        json={"download_link": magnet, "title": "剧集", "type": "auto"},
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.json["ready"] is True
    manual = client.post(
        "/api/download/manual",
        json={
            "download_link": magnet,
            "title": "剧集",
            "type": "tv",
            "episode_title": "第一集",
            "subscribe": True,
            "viewing_mode": "daily",
        },
        headers=headers,
    )
    assert manual.status_code == 200
    assert manual.json["qb_task_id"] == "b" * 40
    uploaded = client.post(
        "/api/download/manual",
        data={"torrent": (io.BytesIO(TORRENT), "movie.torrent"), "type": "movie", "title": "电影"},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    assert uploaded.json["qb_task_id"] == "c" * 40
    assert manual.json["watchlist"]["keyword"] == "剧集"
    assert [call[0] for call in services.download.manual_calls] == ["preview-link", "link", "torrent"]

    rules = client.get("/api/settings/path-rules", headers=headers)
    assert rules.json["count"] == 4
    created = client.post(
        "/api/settings/path-rules",
        json={
            "media_type": "movie",
            "name": "国内电影",
            "source_path": "/volume1/video/Downloads/Movie/CN",
            "downloader_path": "/Downloads/Movie/CN",
            "target_path": "/volume1/video/video/movies/CN",
            "enabled": True,
            "rename_enabled": True,
            "default_download": False,
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json["item"]["downloader_path"] == "/Downloads/Movie/CN"
    rule_id = created.json["item"]["id"]
    changed = client.patch(
        f"/api/settings/path-rules/{rule_id}",
        json={"rename_enabled": False},
        headers=headers,
    )
    assert changed.json["item"]["rename_enabled"] is False
    deleted = client.delete(f"/api/settings/path-rules/{rule_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json["deleted_id"] == rule_id

    assert client.get("/api/settings/system", headers=headers).json["settings"]["watchlist_check_hours"] == 12
    period = client.patch(
        "/api/settings/system", json={"watchlist_check_hours": 72}, headers=headers
    )
    assert period.json["settings"]["watchlist_check_hours"] == 72
    downloader = client.patch(
        "/api/settings/downloader",
        json={"downloader_type": "qbittorrent", "qb_host": "qb.local", "qb_password": "secret"},
        headers=headers,
    )
    assert downloader.status_code == 200
    assert downloader.json["settings"]["qb_password_configured"] is True
    assert "qb_password" not in downloader.json["settings"]
    assert client.post("/api/settings/downloader/test", json={}, headers=headers).status_code == 200


def test_agent_bootstrap_connect_on_demand_permissions_and_revoke(tmp_path):
    app, _ = build_test_app(tmp_path)
    client = app.test_client()
    admin = {"X-Api-Key": "integration-api-key-1234"}
    created = client.post("/api/agents/bootstrap", json={"name": "Codex"}, headers=admin)
    assert created.status_code == 201
    agent = created.json["agent"]
    token = agent["token"]
    bearer = {"Authorization": f"Bearer {token}"}

    connected = client.post(
        "/api/agents/connect",
        json={"name": "Codex", "capabilities": ["search", "download", "hardlink"]},
        headers=bearer,
    )
    assert connected.status_code == 200
    assert connected.json["agent"]["online"] is True
    assert client.get("/api/downloader/status", headers=bearer).status_code == 200
    listing = client.get("/api/agents", headers=admin)
    assert listing.json["online"] == 1
    assert "token" not in listing.json["items"][0]

    forbidden = client.patch(
        "/api/settings/system", json={"watchlist_check_hours": 24}, headers=bearer
    )
    assert forbidden.status_code == 403
    assert client.delete(f"/api/agents/{agent['id']}", headers=admin).status_code == 204
    assert client.get("/api/downloader/status", headers=bearer).status_code == 401


def test_agent_scopes_allow_only_granted_actions_and_search_never_subscribes_implicitly(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    admin = {"X-Api-Key": "integration-api-key-1234"}
    created = client.post(
        "/api/agents/bootstrap",
        json={"name": "search-only", "scopes": ["search"]},
        headers=admin,
    )
    agent = created.json["agent"]
    bearer = {"Authorization": f"Bearer {agent['token']}"}

    searched = client.post("/api/search", json={"keyword": "奥本海默", "type": "movie"}, headers=bearer)
    assert searched.status_code == 200
    assert searched.json["watchlist_added"] is False
    assert services.watchlist.list() == []
    assert client.post(
        "/api/search",
        json={"keyword": "奥本海默", "type": "movie", "add_to_watchlist": True},
        headers=bearer,
    ).status_code == 403
    assert client.post(
        "/api/download",
        json={
            "result_id": searched.json["results"][0]["id"],
            "download_link": searched.json["results"][0]["download_link"],
            "title": "奥本海默",
            "type": "movie",
        },
        headers=bearer,
    ).status_code == 403
    assert client.get("/api/watchlist", headers=bearer).status_code == 403


def test_naming_correction_preview_and_apply_endpoints_require_naming_scope(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    admin = {"X-Api-Key": "integration-api-key-1234"}
    plan = NamingPlan("movie", "原名", "原名", 2024, None, None, "source.mkv")
    job = services.naming_jobs.upsert("f" * 40, plan, "sixv-movie")
    created = client.post(
        "/api/agents/bootstrap", json={"name": "namer", "scopes": ["naming"]}, headers=admin
    )
    bearer = {"Authorization": f"Bearer {created.json['agent']['token']}"}

    preview = client.post(
        f"/api/naming/jobs/{job['id']}/preview", json={"media_name": "修正名"}, headers=bearer
    )
    assert preview.status_code == 200
    assert preview.json["plan"]["media_name"] == "修正名"
    applied = client.patch(
        f"/api/naming/jobs/{job['id']}/plan", json={"media_name": "修正名"}, headers=bearer
    )
    assert applied.status_code == 200
    assert applied.json["item"]["status"] == "retrying"
    assert client.post(
        f"/api/naming/jobs/{job['id']}/preview", json={"media_name": "无权限"}, headers=admin
    ).status_code == 200
