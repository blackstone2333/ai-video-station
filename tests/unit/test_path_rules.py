from pathlib import Path

import pytest

from ainas.config import Settings
from ainas.errors import ValidationAppError
from ainas.path_rules import PathRuleRepository


def rule_settings(tmp_path: Path) -> Settings:
    base = tmp_path / "nas"
    return Settings(
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        medialib_base_path=base,
        medialib_mount_path=tmp_path / "mount",
        downloads_base_path=base / "Downloads",
        download_movie_path=base / "Downloads" / "Movie",
        download_tv_path=base / "Downloads" / "TV",
        download_anime_path=base / "Downloads" / "Anime",
        download_custom_path=base / "Downloads" / "Custom",
        medialib_movie_path=base / "Library" / "Movies",
        medialib_tv_path=base / "Library" / "TV",
        medialib_anime_path=base / "Library" / "Anime",
        medialib_custom_path=base / "Library" / "Custom",
    )


def test_path_rules_support_multiple_mappings_defaults_and_longest_match(tmp_path):
    settings = rule_settings(tmp_path)
    repository = PathRuleRepository(settings)
    assert {item["media_type"] for item in repository.list()} == {"movie", "tv", "anime", "custom"}
    assert repository.get("default-tv")["downloader_path"] == str(settings.download_tv_path)

    base = settings.medialib_base_path
    domestic = repository.add(
        {
            "media_type": "movie",
            "name": "国内电影",
            "source_path": base / "Downloads" / "Movie" / "CN",
            "downloader_path": "/Downloads/Movie/CN",
            "target_path": base / "Library" / "Movies" / "CN",
            "enabled": True,
            "rename_enabled": True,
            "default_download": True,
        }
    )
    movie_rules = repository.list("movie")
    assert len(movie_rules) == 2
    assert sum(item["default_download"] for item in movie_rules) == 1
    assert settings.download_movie_path == Path(domestic["source_path"])
    assert domestic["downloader_path"] == "/Downloads/Movie/CN"

    matched = repository.match("movie", base / "Downloads" / "Movie" / "CN" / "Film" / "movie.mkv")
    assert matched["id"] == domestic["id"]
    changed = repository.update(domestic["id"], {"rename_enabled": False, "enabled": False})
    assert changed["rename_enabled"] is False
    assert repository.match("movie", base / "somewhere-else")["id"] == "default-movie"

    repository.delete(domestic["id"])
    assert len(repository.list("movie")) == 1
    with pytest.raises(Exception):
        repository.get(domestic["id"])


def test_path_rules_reject_relative_and_outside_paths(tmp_path):
    repository = PathRuleRepository(rule_settings(tmp_path))
    common = {
        "media_type": "tv",
        "name": "剧集二库",
        "target_path": repository.settings.medialib_base_path / "Library" / "TV2",
        "enabled": True,
        "rename_enabled": True,
        "default_download": False,
    }
    with pytest.raises(ValidationAppError, match="绝对路径"):
        repository.add({**common, "source_path": "Downloads/TV2"})
    with pytest.raises(ValidationAppError, match="Docker 可访问范围"):
        repository.add({**common, "source_path": "/another-volume/TV2"})
