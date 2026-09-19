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


class StubNaming:
    def __init__(self):
        self.added = []

    def add_download(self, release, category, path_rule_id=None, wanted_episodes=None):
        self.added.append((release, category, path_rule_id, wanted_episodes))
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
    assert first["resource_preferences"]["resolution_order"][0] == "2160p"
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


def test_new_subscription_defaults_to_daily_but_legacy_record_keeps_existing_quality_order(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    daily = repository.add("测试剧", "tv")
    assert daily["viewing_mode"] == "daily"

    releases = [
        make_release("S01E01 4K WEB-DL", "a"),
        make_release("S01E01 1080p BluRay", "b"),
    ]
    naming = StubNaming()
    WatchlistService(settings, repository, StubSearch(releases), StubQB(), naming=naming).check(daily["id"])
    assert naming.added[0][0].resolution == "1080p"

    legacy = repository.add("旧订阅", "tv")
    repository.update(legacy["id"], {"viewing_mode": None, "resource_preferences": None})
    naming = StubNaming()
    WatchlistService(settings, repository, StubSearch(releases), StubQB(), naming=naming).check(legacy["id"])
    assert naming.added[0][0].resolution == "2160p"


def test_changing_viewing_mode_replaces_the_previous_preset_snapshot(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("测试剧", "tv")
    service = WatchlistService(settings, repository, StubSearch([]), StubQB())

    updated = service.update(item["id"], viewing_mode="collection")

    assert updated["viewing_mode"] == "collection"
    assert updated["resource_preferences"]["mode"] == "collection"
    assert updated["resource_preferences"]["resolution_order"][:3] == ["4320p", "2160p", "1080p"]


def test_complete_season_bundle_requests_only_missing_episodes_and_records_provenance(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("重器", "tv")
    repository.update(
        item["id"],
        {"downloaded_episodes": [f"S01E{episode:02d}" for episode in range(1, 27)]},
    )
    bundle = build_release(
        "重器 第1季",
        "全33集 1080p BluRay 国语",
        "https://sixv.test/mj/2026-01-01/2.html",
        "magnet:?xt=urn:btih:" + "f" * 40,
        "tv",
    )
    assert bundle is not None
    naming = StubNaming()

    report = WatchlistService(settings, repository, StubSearch([bundle]), StubQB(), naming=naming).check(item["id"])

    wanted = [f"S01E{episode:02d}" for episode in range(27, 34)]
    assert report["downloaded"] == 1
    assert naming.added[0][3] == wanted
    saved = repository.get(item["id"])
    assert sorted(saved["episode_sources"])[-7:] == wanted
    assert {saved["episode_sources"][episode]["release_id"] for episode in wanted} == {bundle.id}


def test_anime_subscription_keeps_avs_type_when_sixv_url_looks_like_tv(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("Rick and Morty", "anime")
    release = make_release("S01E01 1080p WEB-DL", "e", "anime")
    naming = StubNaming()
    service = WatchlistService(settings, repository, StubSearch([release]), StubQB(), naming=naming)

    report = service.check(item["id"])

    assert report["downloaded"] == 1
    assert naming.added[0][0].media_type == "anime"
    assert naming.added[0][1] == settings.qb_anime_category
    assert repository.get(item["id"])["type"] == "anime"


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


def test_watchlist_handles_all_disjoint_joined_releases_without_repeat_downloads(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("测试剧", "tv")
    releases = [make_release(f"S01E{start:02d}-{start + 1:02d} 1080p", char)
                for start, char in [(3, "a"), (5, "b"), (7, "c")]]
    naming = StubNaming()
    service = WatchlistService(settings, repository, StubSearch(releases), StubQB(), naming=naming)
    assert service.check(item["id"])["downloaded"] == 3
    assert [entry[3] for entry in naming.added] == [[f"S01E{i:02d}" for i in (start, start + 1)] for start in (3, 5, 7)]
    assert service.check(item["id"])["downloaded"] == 0
    assert repository.get(item["id"])["downloaded_episodes"] == [f"S01E{i:02d}" for i in range(3, 9)]


def test_watchlist_normalizes_legacy_range_and_seasonless_episode_tokens():
    values = [make_release("S01E03", "a"), make_release("S01E04", "b"), make_release("EP05", "c")]
    assert WatchlistService._select_tv_releases(values, {"S01E03-E04", "E05"}) == []


def test_completed_job_credits_both_episodes_only_after_success(settings):
    repository = WatchlistRepository(settings.watchlist_path)
    item = repository.add("测试剧", "tv")
    repository.update(item["id"], {"downloaded_episodes": ["S01E03"], "episode_sources": {"S01E03": {"naming_job_id": "job", "release_id": "release"}}})
    job = {"id": "job", "status": "completed", "hardlink_status": "conflict", "completed_episodes": ["S01E03", "S01E04"]}
    repository.record_completed_job(job)
    assert repository.get(item["id"])["downloaded_episodes"] == ["S01E03"]
    job["hardlink_status"] = "done"
    repository.record_completed_job(job)
    repository.record_completed_job(job)
    saved = repository.get(item["id"])
    assert saved["downloaded_episodes"] == saved["completed_episodes"] == ["S01E03", "S01E04"]
    assert saved["episode_sources"]["S01E04"]["status"] == "completed"


def test_completed_manual_job_matches_only_same_title_and_type(settings):
    repo = WatchlistRepository(settings.watchlist_path)
    matching = repo.add("柯蒂斯总统", "anime")
    wrong_type = repo.add("柯蒂斯总统", "tv")
    wrong_title = repo.add("其他节目", "anime")
    repo.record_completed_job({
        "id": "manual", "torrent_hash": "hash", "status": "completed", "hardlink_status": "done",
        "plan": {"media_name": "柯蒂斯总统", "media_type": "anime"},
        "completed_episodes": ["S01E07", "S01E08"],
    })
    assert repo.get(matching["id"])["completed_episodes"] == ["S01E07", "S01E08"]
    assert repo.get(wrong_type["id"])["downloaded_episodes"] == []
    assert repo.get(wrong_title["id"])["downloaded_episodes"] == []
