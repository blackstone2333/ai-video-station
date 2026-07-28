from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from sixv.config import Settings
from sixv.medialib import HardlinkError, MediaLibraryService


def media_settings(tmp_path: Path, enabled: bool = True) -> tuple[Settings, Path, Path]:
    host = tmp_path / "nas-video"
    mount = tmp_path / "medialib"
    mount.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        medialib_hardlink_enabled=enabled,
        medialib_base_path=host,
        medialib_movie_path=host / "video" / "movies",
        medialib_tv_path=host / "video" / "tv",
        medialib_mount_path=mount,
    )
    return settings, host, mount


def test_hardlink_disabled_is_a_noop(tmp_path):
    settings, _, _ = media_settings(tmp_path, enabled=False)
    result = MediaLibraryService(settings).link_completed({}, [], {})
    assert result == {
        "status": "disabled",
        "linked": 0,
        "skipped": 0,
        "already_linked": 0,
        "files": [],
    }


def test_movie_links_all_files_and_preserves_nested_paths(tmp_path):
    settings, host, mount = media_settings(tmp_path)
    source_root = mount / "Downloads" / "sixv-movie" / "大黄蜂 (2018)"
    (source_root / "Extras").mkdir(parents=True)
    video = source_root / "大黄蜂 (2018).mkv"
    nfo = source_root / "movie.nfo"
    poster = source_root / "Extras" / "poster.jpg"
    padding = source_root / ".pad" / "0"
    padding.parent.mkdir()
    video.write_bytes(b"video")
    nfo.write_text("metadata", encoding="utf-8")
    poster.write_bytes(b"poster")
    padding.write_bytes(b"padding")

    result = MediaLibraryService(settings).link_completed(
        {
            "save_path": str(host / "Downloads" / "sixv-movie"),
            "content_path": str(host / "Downloads" / "sixv-movie" / "大黄蜂 (2018)"),
        },
        [
            {"name": "大黄蜂 (2018)/大黄蜂 (2018).mkv", "priority": 1},
            {"name": "大黄蜂 (2018)/movie.nfo", "priority": 1},
            {"name": "大黄蜂 (2018)/Extras/poster.jpg", "priority": 1},
            {"name": "大黄蜂 (2018)/.pad/0", "priority": 1},
        ],
        {
            "media_type": "movie",
            "media_name": "大黄蜂",
            "root_name": "大黄蜂 (2018)",
            "year": 2018,
        },
    )

    target = mount / "video" / "movies" / "大黄蜂 (2018)"
    assert result["linked"] == 3
    assert not (target / ".pad" / "0").exists()
    assert (target / "大黄蜂 (2018).mkv").stat().st_ino == video.stat().st_ino
    assert (target / "movie.nfo").stat().st_ino == nfo.stat().st_ino
    assert (target / "Extras" / "poster.jpg").stat().st_ino == poster.stat().st_ino


def test_movie_strips_obfuscated_torrent_root_directory(tmp_path):
    settings, host, mount = media_settings(tmp_path)
    source_root = mount / "Downloads" / "sixv-movie" / "DHF.2018.1080p"
    source_root.mkdir(parents=True)
    source = source_root / "大黄蜂 (2018).mkv"
    source.write_bytes(b"video")

    MediaLibraryService(settings).link_completed(
        {
            "save_path": str(host / "Downloads" / "sixv-movie"),
            "content_path": str(host / "Downloads" / "sixv-movie" / "DHF.2018.1080p"),
        },
        [{"name": "DHF.2018.1080p/大黄蜂 (2018).mkv", "priority": 1}],
        {"media_type": "movie", "media_name": "大黄蜂", "root_name": "大黄蜂 (2018)"},
    )

    target = mount / "video" / "movies" / "大黄蜂 (2018)"
    assert (target / "大黄蜂 (2018).mkv").stat().st_ino == source.stat().st_ino
    assert not (target / "DHF.2018.1080p").exists()


def test_tv_keeps_season_and_adds_it_for_root_episodes(tmp_path):
    settings, host, mount = media_settings(tmp_path)
    source_root = mount / "Downloads" / "sixv-tv"
    season_episode = source_root / "Season 01" / "漫长的季节 - S01E01.mkv"
    root_episode = source_root / "漫长的季节 - S02E03.mkv"
    season_episode.parent.mkdir(parents=True)
    season_episode.write_bytes(b"episode-1")
    root_episode.write_bytes(b"episode-3")

    result = MediaLibraryService(settings).link_completed(
        {"save_path": str(host / "Downloads" / "sixv-tv")},
        [
            {"name": "Season 01/漫长的季节 - S01E01.mkv", "priority": 1},
            {"name": "漫长的季节 - S02E03.mkv", "priority": 1},
        ],
        {"media_type": "tv", "media_name": "漫长的季节", "root_name": "漫长的季节"},
    )

    target = mount / "video" / "tv" / "漫长的季节"
    first = target / "Season 01" / season_episode.name
    third = target / "Season 02" / root_episode.name
    assert result["linked"] == 2
    assert first.stat().st_ino == season_episode.stat().st_ino
    assert third.stat().st_ino == root_episode.stat().st_ino


def test_existing_files_are_skipped_and_same_inode_is_reported(tmp_path):
    settings, host, mount = media_settings(tmp_path)
    source = mount / "Downloads" / "sixv-movie" / "电影 (2024).mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    target = mount / "video" / "movies" / "电影 (2024)" / source.name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep-me")
    service = MediaLibraryService(settings)
    torrent = {"save_path": str(host / "Downloads" / "sixv-movie")}
    files = [{"name": source.name, "priority": 1}]
    plan = {"media_type": "movie", "media_name": "电影", "root_name": "电影 (2024)"}

    first = service.link_completed(torrent, files, plan)
    assert first["linked"] == 0
    assert first["skipped"] == 1
    assert target.read_bytes() == b"keep-me"

    target.unlink()
    os.link(source, target)
    second = service.link_completed(torrent, files, plan)
    assert second["already_linked"] == 1
    assert second["files"][0]["status"] == "already-linked"


@pytest.mark.parametrize("name", ["../secret.mkv", "/etc/passwd", "folder\\..\\secret.mkv"])
def test_rejects_unsafe_torrent_paths(tmp_path, name):
    settings, host, _ = media_settings(tmp_path)
    with pytest.raises(HardlinkError, match="unsafe torrent file path"):
        MediaLibraryService(settings).link_completed(
            {"save_path": str(host / "Downloads" / "sixv-movie")},
            [{"name": name, "priority": 1}],
            {"media_type": "movie", "media_name": "电影", "root_name": "电影 (2024)"},
        )


def test_rejects_qbittorrent_path_outside_mounted_base(tmp_path):
    settings, _, _ = media_settings(tmp_path)
    with pytest.raises(HardlinkError, match="outside MEDIALIB_BASE_PATH"):
        MediaLibraryService(settings).link_completed(
            {"save_path": "/somewhere/else"},
            [{"name": "movie.mkv", "priority": 1}],
            {"media_type": "movie", "media_name": "电影", "root_name": "电影 (2024)"},
        )


def test_settings_reject_media_target_outside_base(tmp_path):
    with pytest.raises(ValidationError, match="MEDIALIB_MOVIE_PATH must be inside MEDIALIB_BASE_PATH"):
        Settings(
            scheduler_enabled=False,
            medialib_base_path=tmp_path / "video",
            medialib_movie_path=tmp_path / "other" / "movies",
            medialib_tv_path=tmp_path / "video" / "tv",
            medialib_mount_path=tmp_path / "mount",
        )
