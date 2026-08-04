from __future__ import annotations

from dataclasses import replace

import pytest

from ainas.errors import NotFoundError, ServiceUnavailableError
from ainas.quality import build_release
from ainas.services import ResultCache, SearchOutcome, WatchlistService
from ainas.watchlist import WatchlistRepository


class StubSearch:
    def __init__(self, releases):
        self.releases = releases

    def search(self, keyword, media_type):
        return SearchOutcome(keyword, media_type, self.releases)


class StubQB:
    def __init__(self, fail=False, fail_after=None):
        self.added = []
        self.fail = fail
        self.fail_after = fail_after

    def add_download(self, link, category):
        if self.fail or (self.fail_after is not None and len(self.added) >= self.fail_after):
            raise ServiceUnavailableError("qb unavailable")
        self.added.append((link, category))
        return {"qb_task_id": "x", "category": category}


def make_release(label, hash_char="a", media_type="tv"):
    return build_release(
        "测试剧" if media_type == "tv" else "测试电影",
        label,
        "https://sixv.test/mj/2026-01-01/1.html",
        "magnet:?xt=urn:btih:" + hash_char * 40,
        media_type,
    )


def test_repository_crud_is_idempotent(tmp_path):
    repository = WatchlistRepository(tmp_path / "watchlist.json")
    first = repository.add("奥本海默", "movie")
    second = repository.add("奥本海默", "movie")
    assert first["id"] == second["id"]
    assert repository.find(" 奥本海默 ", "movie")["id"] == first["id"]
    assert repository.find("奥本海默", "auto")["id"] == first["id"]
    assert repository.find("不存在", "movie") is None
    assert len(repository.list()) == 1
    assert repository.update(first["id"], {"status": "found"})["status"] == "found"
    repository.delete(first["id"])
    with pytest.raises(NotFoundError):
        repository.get(first["id"])


def test_tv_check_downloads_one_best_version_per_episode(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("测试剧", "tv")
    releases = [
        make_release("S01E01 1080p BluRay", "a"),
        make_release("S01E01 720p WEB-DL", "b"),
        make_release("S01E02 1080p WEB-DL", "c"),
    ]
    qb = StubQB()
    service = WatchlistService(settings, repository, StubSearch(releases), qb)
    first = service.check(item["id"])
    assert first["downloaded"] == 2
    assert len(qb.added) == 2
    second = service.check(item["id"])
    assert second["downloaded"] == 0
    assert repository.get(item["id"])["status"] == "monitoring"


def test_movie_failure_is_recorded_for_retry(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("测试电影", "movie")
    media = make_release("1080p BluRay", "d", "movie")
    service = WatchlistService(settings, repository, StubSearch([media]), StubQB(fail=True))
    report = service.check(item["id"])
    assert report["downloaded"] == 0
    assert "qb unavailable" in repository.get(item["id"])["last_error"]


def test_partial_episode_batch_checkpoints_success_before_a_later_qb_failure(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("测试剧", "tv")
    releases = [
        make_release("S01E01 1080p WEB-DL", "a"),
        make_release("S01E02 1080p WEB-DL", "b"),
    ]
    qb = StubQB(fail_after=1)
    service = WatchlistService(settings, repository, StubSearch(releases), qb)

    report = service.check(item["id"])
    saved = repository.get(item["id"])

    assert report["downloaded"] == 1
    assert saved["downloaded_episodes"] == ["S01E01"]
    assert len(saved["downloaded_links"]) == 1
    assert "qb unavailable" in saved["last_error"]
