from pathlib import Path

import pytest

from ainas.errors import ValidationAppError
from ainas.models import DownloadRequest, NamingPlanPreviewRequest, PaginationParams
from ainas.path_rules import PathRuleRepository
from ainas.quality import build_release
from ainas.services import DownloadService, ResultCache, SearchOutcome, WatchlistService
from ainas.watchlist import WatchlistRepository


class QB:
    def __init__(self):
        self.calls = []

    def add_download(self, link, category, **kwargs):
        self.calls.append((link, category, kwargs))
        return {"qb_task_id": "a" * 40, "category": category}


class Search:
    def __init__(self, release):
        self.release = release

    def search(self, keyword, media_type):
        return SearchOutcome(keyword, media_type, [self.release])


def release(media_type="movie"):
    return build_release("测试电影", "1080p", "https://example.test/a", "magnet:?xt=urn:btih:" + "a" * 40, media_type)


def test_explicit_route_is_used_and_invalid_routes_do_not_submit(settings):
    rules = PathRuleRepository(settings)
    qb = QB()
    cache = ResultCache()
    item = release()
    cache.put_all([item])
    service = DownloadService(settings, qb, cache, path_rules=rules)
    result = service.download(item.id, item.download_link, item.title, "movie", "default-movie")
    assert result["path_rule_id"] == "default-movie"
    assert qb.calls[0][2]["save_path"] == str(settings.download_movie_path)
    with pytest.raises(ValidationAppError):
        service.download(item.id, item.download_link, item.title, "movie", "default-tv")
    rules.update("default-movie", {"enabled": False})
    with pytest.raises(ValidationAppError):
        service.download(item.id, item.download_link, item.title, "movie", "default-movie")
    assert len(qb.calls) == 1


def test_subscription_pins_and_reuses_route(settings):
    rules = PathRuleRepository(settings)
    alternate = rules.add({
        "media_type": "movie", "name": "alternate", "source_path": settings.medialib_base_path / "Downloads" / "Alt",
        "downloader_path": "/downloads/alt", "target_path": settings.medialib_base_path / "Library" / "Alt",
        "enabled": True, "rename_enabled": True, "default_download": False,
    })
    repo = WatchlistRepository(settings.watchlist_path)
    item = repo.add("测试电影", "movie", alternate["id"], alternate)
    qb = QB()
    WatchlistService(settings, repo, Search(release()), qb, path_rules=rules).check(item["id"])
    assert qb.calls[0][2]["save_path"] == "/downloads/alt"
    assert repo.get(item["id"])["path_rule_id"] == alternate["id"]


def test_preflight_does_not_create_directories_and_delete_requires_safe_opt_out(settings):
    rules = PathRuleRepository(settings)
    before = Path(settings.download_movie_path)
    assert not before.exists()
    report = rules.preflight("default-movie")
    assert report["status"] == "error"
    assert not before.exists()
    with pytest.raises(ValidationAppError):
        rules.delete("default-movie")
    impact = rules.delete("default-movie", disable_media_path=True)
    assert impact["default_change"]["replacement_default_id"] is None


def test_route_and_planning_models_validate():
    assert DownloadRequest.model_validate({"result_id": "abcdefgh", "download_link": "magnet:?x=1", "title": "x", "path_rule_id": "abcdefgh"}).path_rule_id == "abcdefgh"
    assert PaginationParams().page_size == 50
    assert NamingPlanPreviewRequest.model_validate({"media_type": "movie", "source_name": "Film.mkv"}).source_name == "Film.mkv"
