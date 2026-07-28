from __future__ import annotations

import responses

from ainas.config import Settings
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
    assert repository.update("generic", {"enabled": False})["enabled"] is False
    repository.delete("generic")
    assert [item["id"] for item in repository.list()] == ["sixv"]


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
