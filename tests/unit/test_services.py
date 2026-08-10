import pytest
from types import SimpleNamespace

from ainas.errors import ValidationAppError
from ainas.quality import build_release, link_display_name
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

    def add_torrent_file(self, content, filename, category):
        self.values.append((filename, category))
        return {"qb_task_id": "e" * 40, "category": category}


class CapturingNaming:
    def __init__(self):
        self.release = None

    def add_torrent_file(self, release, category, content, filename, info_hash, path_rule_id=None):
        self.release = release
        return {"qb_task_id": info_hash, "category": category}


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


def test_manual_magnet_requires_a_real_link_name_or_explicit_title(settings):
    qb = StubQB()
    service = DownloadService(settings, qb, ResultCache())
    nameless = "magnet:?xt=urn:btih:" + "a" * 40

    with pytest.raises(ValidationAppError, match="不包含可识别的资源名称") as error:
        service.manual_link(nameless, None, "auto")
    assert error.value.errors[0]["code"] == "TITLE_REQUIRED_FOR_NAMELESS_LINK"
    assert qb.values == []

    with pytest.raises(ValidationAppError):
        service.manual_link(nameless, "手动下载", "movie")
    assert qb.values == []


def test_manual_magnet_uses_dn_and_explicit_title_without_placeholder(settings):
    qb = StubQB()
    service = DownloadService(settings, qb, ResultCache())
    named = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=Show.S01E02.1080p.mkv"

    detected = service.manual_link(named, None, "auto")
    overridden = service.manual_link(
        "magnet:?xt=urn:btih:" + "c" * 40,
        "我的电影",
        "movie",
    )

    assert detected["source_name"] == "Show.S01E02.1080p.mkv"
    assert detected["type"] == "tv"
    assert overridden["title"] == "我的电影"
    assert overridden["source_name"] == "我的电影"


def test_manual_torrent_preserves_special_characters_in_metadata_name(settings, monkeypatch):
    metadata = SimpleNamespace(name="Movie & More 中文.mkv", info_hash="d" * 40)
    monkeypatch.setattr("ainas.services.parse_torrent_metadata", lambda _: metadata)
    naming = CapturingNaming()
    service = DownloadService(settings, StubQB(), ResultCache(), naming=naming)

    result = service.manual_torrent(b"torrent", "movie.torrent", None, "movie")

    assert result["source_name"] == metadata.name
    assert naming.release.link_name == metadata.name
    assert naming.release.download_link == (
        "magnet:?xt=urn:btih:" + "d" * 40 + "&dn=Movie%20%26%20More%20%E4%B8%AD%E6%96%87.mkv"
    )
    assert link_display_name(naming.release.download_link, "") == metadata.name
