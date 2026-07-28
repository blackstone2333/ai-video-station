import pytest

from ainas.errors import ValidationAppError
from ainas.quality import build_release
from ainas.services import DownloadService, ResultCache, SearchService


RELEASE = build_release(
    "测试剧",
    "S01E01 1080p WEB-DL",
    "https://sixv.test/mj/2026-01-01/1.html",
    "magnet:?xt=urn:btih:" + "e" * 40,
    "tv",
)


class StubCrawler:
    def search(self, keyword, media_type):
        return [RELEASE]


class StubQB:
    def __init__(self):
        self.values = []

    def add_download(self, link, category):
        self.values.append((link, category))
        return {"qb_task_id": "e" * 40, "category": category}


def test_search_service_resolves_auto_type_and_cache_expires():
    cache = ResultCache(ttl_seconds=60)
    service = SearchService(StubCrawler(), cache)
    outcome = service.search("测试剧", "auto")
    assert outcome.media_type == "tv"
    assert cache.get(RELEASE.id) == RELEASE
    cache.ttl_seconds = -1
    cache.put_all([RELEASE])
    assert cache.get(RELEASE.id) is None


def test_download_service_uses_cached_type_and_rejects_mismatch(settings):
    cache = ResultCache()
    cache.put_all([RELEASE])
    qb = StubQB()
    service = DownloadService(settings, qb, cache)
    result = service.download(RELEASE.id, RELEASE.download_link, RELEASE.title, "auto")
    assert result["type"] == "tv"
    assert qb.values[0][1] == settings.qb_tv_category
    with pytest.raises(ValidationAppError):
        service.download(RELEASE.id, "magnet:?xt=urn:btih:" + "f" * 40, RELEASE.title, "tv")


def test_download_service_routes_anime_and_requires_type_when_unknown(settings):
    anime = build_release(
        "Rick and Morty",
        "S01E01 1080p",
        "https://sixv.test/dm/1.html",
        "magnet:?xt=urn:btih:" + "c" * 40,
        "anime",
    )
    cache = ResultCache()
    cache.put_all([anime])
    qb = StubQB()
    service = DownloadService(settings, qb, cache)
    assert service.download(anime.id, anime.download_link, anime.title, "auto")["type"] == "anime"
    assert qb.values[0][1] == settings.qb_anime_category

    with pytest.raises(ValidationAppError, match="media type could not be detected"):
        service.download("unknown12", "magnet:?xt=urn:btih:" + "d" * 40, "Unknown title", "auto")


def test_download_service_routes_custom_content(settings):
    qb = StubQB()
    service = DownloadService(settings, qb, ResultCache())
    result = service.download(
        "custom001",
        "magnet:?xt=urn:btih:" + "f" * 40,
        "Python 学习资料",
        "custom",
    )
    assert result["type"] == "custom"
    assert qb.values[0][1] == settings.qb_custom_category
