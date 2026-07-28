from __future__ import annotations

from pathlib import Path

from sixv.app import create_app
from sixv.config import Settings
from sixv.naming import NamingJobRepository, NamingPlan
from sixv.quality import build_release
from sixv.services import AppServices, ResultCache, SearchOutcome
from sixv.watchlist import WatchlistRepository


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

    def status(self):
        return {"configured": True, "connected": True, "version": "4.6.7", "error": None}

    def tasks(self):
        return [{"hash": "abc", "name": "Movie", "progress": 0.5}]

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

    def download(self, result_id, link, title, media_type):
        self.qb.add_download(link, "sixv-movie")
        return {"qb_task_id": "a" * 40, "category": "sixv-movie", "title": title, "type": "movie"}


class StubWatchlistService:
    def check(self, item_id=None):
        return {"running": False, "checked": 1, "downloaded": 0, "results": []}


class StubNamingService:
    def check(self, job_id=None):
        return {"running": False, "checked": 1, "completed": 1, "results": []}


def build_test_app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        scheduler_enabled=False,
        api_key="test-key",
        rate_limit_per_minute=100,
        cors_origins="https://qclaw.test",
    )
    cache = ResultCache()
    qb = StubQB()
    watchlist = WatchlistRepository(settings.watchlist_path)
    search = StubSearch(cache)
    naming_jobs = NamingJobRepository(settings.naming_jobs_path)
    services = AppServices(
        crawler=StubCrawler(),
        qb=qb,
        watchlist=watchlist,
        cache=cache,
        search=search,
        download=StubDownload(qb),
        watchlist_service=StubWatchlistService(),
        naming_jobs=naming_jobs,
        naming=StubNamingService(),
    )
    return create_app(settings, services, start_scheduler=False), services


def test_health_auth_search_download_and_qb(tmp_path):
    app, services = build_test_app(tmp_path)
    client = app.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/").location == "/admin"
    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "6V 媒体调度台" in admin.get_data(as_text=True)
    assert "frame-ancestors 'none'" in admin.headers["Content-Security-Policy"]
    assert client.get("/static/admin.css").status_code == 200
    assert client.get("/static/admin.js").status_code == 200
    assert client.get("/ready").json["status"] == "ok"
    assert client.post("/api/search", json={"keyword": "奥本海默", "type": "movie"}).status_code == 401

    headers = {"X-Api-Key": "test-key", "X-Request-Id": "request-1"}
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
    assert client.get("/api/qb/tasks", headers=headers).json["count"] == 1


def test_validation_auto_watch_cors_and_watchlist_crud(tmp_path):
    app, _ = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"Authorization": "Bearer test-key", "Origin": "https://qclaw.test"}
    bad = client.post("/api/search", json={"keyword": "  ", "type": "wrong"}, headers=headers)
    assert bad.status_code == 422
    assert bad.content_type == "application/problem+json"
    assert len(bad.json["errors"]) == 2

    missing = client.post("/api/search", json={"keyword": "不存在", "type": "movie"}, headers=headers)
    assert missing.json["watchlisted"] is True
    assert missing.headers["Access-Control-Allow-Origin"] == "https://qclaw.test"

    added = client.post("/api/watchlist/add", json={"keyword": "龙之家族", "type": "tv"}, headers=headers)
    assert added.status_code == 201
    item_id = added.json["item"]["id"]
    listing = client.get("/api/watchlist", headers=headers)
    assert listing.json["count"] == 2
    checked = client.post("/api/watchlist/check", json={"item_id": item_id}, headers=headers)
    assert checked.json["checked"] == 1
    assert client.delete(f"/api/watchlist/{item_id}", headers=headers).status_code == 204


def test_malformed_requests_and_not_found_use_problem_details(tmp_path):
    app, _ = build_test_app(tmp_path)
    client = app.test_client()
    headers = {"X-Api-Key": "test-key"}
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
    headers = {"X-Api-Key": "test-key"}
    plan = NamingPlan("movie", "大黄蜂", "大黄蜂 (2018)", 2018, None, None, "DHF.mp4")
    item = services.naming_jobs.upsert("a" * 40, plan, "sixv-movie")

    listing = client.get("/api/naming/jobs?page=1&per_page=10", headers=headers)
    assert listing.status_code == 200
    assert listing.json["pagination"]["total"] == 1
    assert client.get(f"/api/naming/jobs/{item['id']}", headers=headers).json["item"]["plan"]["root_name"] == "大黄蜂 (2018)"
    checked = client.post("/api/naming/jobs/check", json={"job_id": item["id"]}, headers=headers)
    assert checked.json["completed"] == 1
    assert client.get("/api/naming/jobs?page=bad", headers=headers).status_code == 422
