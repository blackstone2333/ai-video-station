from __future__ import annotations

import json
from pathlib import Path

import pytest

from ainas.config import Settings
from ainas.errors import ValidationAppError
from ainas.medialib import MediaLibraryService
from ainas.path_settings import PathSettingsRepository


def path_settings(tmp_path: Path) -> Settings:
    host = tmp_path / "volume"
    mount = tmp_path / "medialib"
    host.mkdir(exist_ok=True)
    mount.mkdir(exist_ok=True)
    return Settings(
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        medialib_base_path=host,
        medialib_mount_path=mount,
        downloads_base_path=host / "Downloads",
        download_movie_path=host / "Downloads" / "Movie",
        download_tv_path=host / "Downloads" / "TV",
        download_anime_path=host / "Downloads" / "Anime",
        medialib_movie_path=host / "video" / "movies",
        medialib_tv_path=host / "video" / "tv",
        medialib_anime_path=host / "video" / "anime",
    )


def test_path_settings_persist_and_restore_shared_settings(tmp_path):
    settings = path_settings(tmp_path)
    repository = PathSettingsRepository(settings)
    host = settings.medialib_base_path
    updated = repository.update(
        {
            "downloads_base_path": host / "Incoming",
            "download_movie_path": host / "Incoming" / "Films",
            "download_tv_path": host / "Incoming" / "Series",
            "download_anime_path": host / "Incoming" / "Animation",
            "medialib_movie_path": host / "Library" / "Films",
            "medialib_tv_path": host / "Library" / "Series",
            "medialib_anime_path": host / "Library" / "Animation",
            "medialib_hardlink_enabled": False,
        }
    )

    assert settings.download_movie_path == host / "Incoming" / "Films"
    assert settings.medialib_anime_path == host / "Library" / "Animation"
    assert settings.medialib_hardlink_enabled is False
    assert updated["download_tv_path"].endswith("/Incoming/Series")
    assert json.loads(settings.path_settings_path.read_text(encoding="utf-8"))["medialib_hardlink_enabled"] is False

    restarted = path_settings(tmp_path)
    restored = PathSettingsRepository(restarted).get()
    assert restarted.download_movie_path == host / "Incoming" / "Films"
    assert restored["medialib_tv_path"].endswith("/Library/Series")
    assert restored["medialib_hardlink_enabled"] is False


def test_path_settings_reject_relative_outside_unknown_and_invalid_file(tmp_path):
    settings = path_settings(tmp_path)
    repository = PathSettingsRepository(settings)

    with pytest.raises(ValidationAppError, match="绝对路径"):
        repository.update({"download_movie_path": Path("Downloads/Movie")})
    with pytest.raises(ValidationAppError, match="Docker"):
        repository.update({"download_movie_path": tmp_path / "outside"})
    with pytest.raises(ValidationAppError, match="不支持"):
        repository.update({"medialib_base_path": tmp_path})

    settings.path_settings_path.write_text("{", encoding="utf-8")
    with pytest.raises(ValidationAppError, match="无法读取"):
        PathSettingsRepository(settings)


def test_updated_paths_are_used_by_download_and_hardlink_services(tmp_path):
    settings = path_settings(tmp_path)
    repository = PathSettingsRepository(settings)
    host = settings.medialib_base_path
    new_movie_download = host / "Incoming" / "Movies"
    new_movie_library = host / "Library" / "Movies"
    repository.update(
        {
            "download_movie_path": new_movie_download,
            "medialib_movie_path": new_movie_library,
        }
    )

    assert settings.download_path_for_category(settings.qb_movie_category) == new_movie_download
    hardlinker = MediaLibraryService(settings)
    assert hardlinker._target_root("movie") == settings.medialib_mount_path / "Library" / "Movies"
