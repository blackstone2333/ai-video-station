from __future__ import annotations

import threading
import time

import responses
import pytest

from ainas.config import Settings
from ainas.errors import UpstreamError, ValidationAppError
from ainas.quality import build_release
from ainas.sites import GenericHTMLProvider, ProviderRegistry, SiteRepository


def generic_site(**overrides):
    value = {
        "id": "generic",
        "name": "Generic Media",
        "adapter": "generic",
        "enabled": True,
        "base_urls": ["https://media.test"],
        "search_url": "{base_url}/search?q={keyword}",
        "result_selector": ".item",
        "title_selector": ".title",
        "link_selector": ".title",
        "download_selector": "a.download",
        "default_type": "auto",
        "anime_path_patterns": ["/anime/"],
    }
    value.update(overrides)
    return value


def test_site_repository_crud(tmp_path):
    default = {
        "id": "sixv",
        "name": "6v",
        "adapter": "sixv",
        "enabled": True,
        "base_urls": ["https://sixv.test"],
    }
    repository = SiteRepository(tmp_path / "sites.json", default)
    assert repository.list()[0]["id"] == "sixv"
    added = repository.add(generic_site())
    assert added["adapter"] == "generic"
    assert repository.add(generic_site(id="learning", default_type="custom"))["default_type"] == "custom"
    assert repository.update("generic", {"enabled": False})["enabled"] is False
    repository.delete("generic")
    assert [item["id"] for item in repository.list()] == ["sixv", "learning"]


def test_site_repository_reserves_sixv_adapter_for_builtin_provider(tmp_path):
    repository = SiteRepository(
        tmp_path / "sites.json",
        {
            "id": "sixv",
            "name": "6v",
            "adapter": "sixv",
            "enabled": True,
            "base_urls": ["https://sixv.test"],
        },
    )

    with pytest.raises(ValidationAppError, match="reserved for the built-in provider"):
        repository.add(
            {
                "id": "untrusted-sixv",
                "name": "Untrusted",
                "adapter": "sixv",
                "base_urls": ["http://127.0.0.1"],
            }
        )


@responses.activate
def test_generic_provider_searches_and_classifies_anime(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    responses.get(
        "https://media.test/search?q=Rick+and+Morty",
        body='<article class="item"><a class="title" href="/anime/rick.html">Rick and Morty</a></article>',
    )
    responses.get(
        "https://media.test/anime/rick.html",
        body='<a class="download" href="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&dn=S01E01.mkv">S01E01 1080p</a>',
    )
    values = GenericHTMLProvider(settings, generic_site()).search("Rick and Morty")
    assert len(values) == 1
    assert values[0].media_type == "anime"
    assert values[0].episode == "S01E01"
    assert values[0].provider == "generic"


@responses.activate
def test_provider_registry_uses_enabled_site_and_probe(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    repository = SiteRepository(tmp_path / "sites.json", generic_site())
    responses.get("https://media.test/", body="ok")
    assert ProviderRegistry(settings, repository).probe() == "https://media.test"

    responses.get(
        "https://media.test/search?q=Rick+and+Morty",
        body='<article class="item"><a class="title" href="/anime/rick.html">Rick and Morty</a></article>',
    )
    responses.get(
        "https://media.test/anime/rick.html",
        body='<a class="download" href="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&dn=S02E01.mkv">S02E01</a>',
    )
    values = ProviderRegistry(settings, repository).search("Rick and Morty", "anime")
    assert len(values) == 1
    assert values[0].provider == "generic"


def test_generic_provider_blocks_private_hosts_and_redirects(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    private = GenericHTMLProvider(settings, generic_site(base_urls=["http://127.0.0.1"]))
    with pytest.raises(UpstreamError, match="blocked private"):
        private.probe()

    provider = GenericHTMLProvider(settings, generic_site())
    with responses.RequestsMock() as mocked:
        mocked.get("https://media.test/", status=302, headers={"Location": "http://127.0.0.1/"})
        with pytest.raises(UpstreamError, match="host is not configured"):
            provider.probe()


@responses.activate
def test_generic_provider_rejects_oversized_response(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    responses.get(
        "https://media.test/search?q=test",
        body="x" * (2 * 1024 * 1024 + 1),
        headers={"Content-Type": "text/html"},
    )
    with pytest.raises(UpstreamError, match="size limit"):
        GenericHTMLProvider(settings, generic_site()).search("test")


def test_provider_registry_is_bounded_partial_and_deterministic(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    repository = SiteRepository(tmp_path / "sites.json", generic_site())
    registry = ProviderRegistry(settings, repository, max_workers=2)
    active = 0
    peak = 0
    lock = threading.Lock()

    class Provider:
        def __init__(self, name, fail=False): self.name, self.fail = name, fail
        def probe(self): return self.name
        def search(self, keyword, media_type):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            if self.fail:
                raise UpstreamError(self.name, "unavailable")
            return [build_release("Same", "same", "https://example.test", "magnet:?xt=urn:btih:" + "a" * 40, "movie")]

    providers = [
        ({"id": "first", "name": "first"}, Provider("first")),
        ({"id": "broken", "name": "broken"}, Provider("broken", fail=True)),
        ({"id": "last", "name": "last"}, Provider("last")),
    ]
    monkeypatch.setattr(registry, "_providers", lambda: providers)
    values = registry.search("same", "movie")
    assert peak <= 2
    assert len(values) == 1
    # Site order, rather than future completion order, selects the duplicate.
    assert values[0].provider == "last"
