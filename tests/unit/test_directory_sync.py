from __future__ import annotations

from pathlib import Path

from ainas.config import Settings
from ainas.directory_sync import DirectorySyncRepository, DirectorySyncService
from ainas.state import StateStore


class Rules:
    def __init__(self, rule):
        self.rule = rule

    def list(self):
        return [dict(self.rule)]

    def host_path_for_downloader(self, value):
        return Path(value)


class Jobs:
    def __init__(self, values=None):
        self.values = values or []

    def list(self):
        return list(self.values)


class Downloader:
    def __init__(self, tasks=None, info=None, configured=True):
        self._tasks = tasks or []
        self._info = info or {}
        self.configured = configured

    def tasks(self):
        return list(self._tasks)

    def torrent_info(self, hash_value):
        return self._info.get(hash_value)


def sync_env(tmp_path, *, jobs=None, downloader=None):
    host = tmp_path / "nas"
    mount = tmp_path / "medialib"
    source_host = host / "Downloads" / "Anime"
    target_host = host / "Library" / "Anime"
    source = mount / "Downloads" / "Anime"
    target = mount / "Library" / "Anime"
    source.mkdir(parents=True)
    mount.mkdir(exist_ok=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        medialib_base_path=host,
        medialib_mount_path=mount,
        downloads_base_path=host / "Downloads",
        download_movie_path=host / "Downloads" / "Movie",
        download_tv_path=host / "Downloads" / "TV",
        download_anime_path=source_host,
        download_custom_path=host / "Downloads" / "Custom",
        medialib_movie_path=host / "Library" / "Movies",
        medialib_tv_path=host / "Library" / "TV",
        medialib_anime_path=target_host,
        medialib_custom_path=host / "Library" / "Custom",
        medialib_hardlink_enabled=True,
        directory_sync_enabled=True,
        directory_sync_settle_seconds=0,
    )
    rule = {
        "id": "anime-rule-1234",
        "name": "Ani-RSS 动漫",
        "media_type": "anime",
        "source_path": str(source_host),
        "downloader_path": str(source_host),
        "target_path": str(target_host),
        "enabled": True,
    }
    state = StateStore(settings.state_db_path)
    service = DirectorySyncService(
        settings,
        DirectorySyncRepository(state),
        Rules(rule),
        jobs or Jobs(),
        downloader or Downloader(configured=False),
    )
    return service, rule, source, target, source_host


def test_external_stable_anime_file_is_hardlinked_and_idempotent(tmp_path):
    service, _, source, target, _ = sync_env(tmp_path)
    episode = source / "葬送的芙莉莲 (2023)" / "Season 01" / "葬送的芙莉莲.S01E01.1080p.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"anime-episode")

    first = service.scan()
    linked = target / episode.relative_to(source)

    assert first["linked"] == 1
    assert linked.exists()
    assert episode.samefile(linked)

    second = service.scan()
    assert second["linked"] == 0
    assert second["runs"][0]["already_linked"] == 1
    assert episode.samefile(linked)


def test_active_downloader_content_is_waited_for(tmp_path):
    host = tmp_path / "nas"
    task_hash = "a" * 40
    downloader = Downloader(
        tasks=[{"hash": task_hash, "progress": 0.5, "completed": False}],
        info={task_hash: {"content_path": str(host / "Downloads" / "Anime" / "New Show")}},
    )
    service, _, source, target, _ = sync_env(tmp_path, downloader=downloader)
    episode = source / "New Show" / "01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"still-downloading")

    report = service.scan()

    assert report["linked"] == 0
    assert report["waiting"] == 1
    assert not (target / "New Show" / "01.mkv").exists()


def test_active_avs_naming_root_is_waited_for(tmp_path):
    host = tmp_path / "nas"
    jobs = Jobs(
        [
            {
                "status": "waiting_download",
                "submission": {"save_path": str(host / "Downloads" / "Anime" / "AVS Show")},
            }
        ]
    )
    service, _, source, target, _ = sync_env(tmp_path, jobs=jobs)
    episode = source / "AVS Show" / "01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"avs-active")

    report = service.scan()

    assert report["waiting"] == 1
    assert not (target / "AVS Show" / "01.mkv").exists()


def test_same_name_conflict_never_overwrites_target(tmp_path):
    service, _, source, target, _ = sync_env(tmp_path)
    episode = source / "Show" / "01.mkv"
    existing = target / "Show" / "01.mkv"
    episode.parent.mkdir(parents=True)
    existing.parent.mkdir(parents=True)
    episode.write_bytes(b"source")
    existing.write_bytes(b"keep-me")

    report = service.scan()

    assert report["conflicts"] == 1
    assert existing.read_bytes() == b"keep-me"
    assert not episode.samefile(existing)


def test_link_failure_is_recorded_without_removing_source(tmp_path, monkeypatch):
    service, _, source, target, _ = sync_env(tmp_path)
    episode = source / "Show" / "01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"source")

    def fail_link(_source, _target):
        raise OSError("cross-device link")

    monkeypatch.setattr("ainas.directory_sync.os.link", fail_link)
    report = service.scan()

    run = report["runs"][0]
    assert run["status"] == "conflict"
    assert "cross-device link" in run["error"]
    assert episode.exists()
    assert not (target / "Show" / "01.mkv").exists()
