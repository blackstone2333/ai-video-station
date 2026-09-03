from __future__ import annotations

import os

import time
from pathlib import Path

from ainas.config import Settings
from ainas.directory_sync import (
    DirectorySyncEventHandler,
    DirectorySyncRepository,
    DirectorySyncService,
    DirectorySyncWatcher,
)
from ainas.state import StateStore


class Rules:
    def __init__(self, rule):
        self.rules = [dict(rule)] if isinstance(rule, dict) else list(rule)

    def list(self, media_type=None):
        if media_type:
            return [dict(r) for r in self.rules if r.get("media_type") == media_type]
        return [dict(r) for r in self.rules]

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


def sync_env(tmp_path, *, jobs=None, downloader=None, settle_seconds=0, enabled=True):
    host = tmp_path / "nas"
    mount = tmp_path / "medialib"
    source_host = host / "Downloads" / "Anime"
    target_host = host / "Library" / "Anime"
    source = mount / "Downloads" / "Anime"
    target = mount / "Library" / "Anime"
    source.mkdir(parents=True, exist_ok=True)
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
        directory_sync_enabled=enabled,
        directory_sync_settle_seconds=settle_seconds,
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
    repo = DirectorySyncRepository(state)
    rules = Rules(rule)
    service = DirectorySyncService(
        settings,
        repo,
        rules,
        jobs or Jobs(),
        downloader or Downloader(configured=False),
    )
    return service, rule, source, target, source_host, settings, rules, repo


def test_external_stable_anime_file_is_hardlinked_and_idempotent(tmp_path):
    service, _, source, target, _, _, _, _ = sync_env(tmp_path)
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
    service, _, source, target, _, _, _, _ = sync_env(tmp_path, downloader=downloader)
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
    service, _, source, target, _, _, _, _ = sync_env(tmp_path, jobs=jobs)
    episode = source / "AVS Show" / "01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"avs-active")

    report = service.scan()

    assert report["waiting"] == 1
    assert not (target / "AVS Show" / "01.mkv").exists()


def test_same_name_conflict_never_overwrites_target(tmp_path):
    service, _, source, target, _, _, _, _ = sync_env(tmp_path)
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
    service, _, source, target, _, _, _, _ = sync_env(tmp_path)
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


def test_targeted_scan_changed_only_processes_specified_paths(tmp_path):
    service, rule, source, target, _, _, _, repo = sync_env(tmp_path)
    file1 = source / "Show A" / "01.mp4"
    file2 = source / "Show B" / "01.mp4"
    file1.parent.mkdir(parents=True)
    file2.parent.mkdir(parents=True)
    file1.write_bytes(b"video1")
    file2.write_bytes(b"video2")

    # Target only file1
    report = service.scan_changed(rule["id"], [file1], is_auto=True)
    assert report["linked"] == 1
    assert (target / "Show A" / "01.mp4").exists()
    assert not (target / "Show B" / "01.mp4").exists()
    assert len(repo.list()) == 1

    # Auto scan on already linked file does not persist redundant run
    report_dup = service.scan_changed(rule["id"], [file1], is_auto=True)
    assert report_dup["linked"] == 0
    assert len(repo.list()) == 1

    # Target file2
    report2 = service.scan_changed(rule["id"], [file2], is_auto=True)
    assert report2["linked"] == 1
    assert (target / "Show B" / "01.mp4").exists()
    assert len(repo.list()) == 2


def test_auto_scan_skips_persisting_empty_runs(tmp_path):
    service, rule, source, target, _, _, _, repo = sync_env(tmp_path)
    empty_file = source / "readme.txt"
    empty_file.write_text("not a video")

    report = service.scan_changed(rule["id"], [empty_file], is_auto=True)
    assert report["linked"] == 0
    assert len(repo.list()) == 0

    # Manual scan on same directory still persists run record
    manual_report = service.scan(rule["id"])
    assert manual_report["linked"] == 0
    assert len(repo.list()) == 1


def test_directory_sync_watcher_events_and_debouncing(tmp_path):
    service, rule, source, target, _, settings, rules, repo = sync_env(tmp_path, settle_seconds=0)
    watcher = DirectorySyncWatcher(service, rules, settings)
    watcher.start()
    assert watcher.running is True

    try:
        # Create a video file
        ep = source / "AniShow" / "S01E01.mkv"
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_bytes(b"episode-content")

        # Wait briefly for debounce timer to fire
        deadline = time.time() + 3.0
        linked_target = target / "AniShow" / "S01E01.mkv"
        while time.time() < deadline and not linked_target.exists():
            time.sleep(0.05)

        assert linked_target.exists()
        assert ep.samefile(linked_target)
        assert len(repo.list()) == 1

        # Non-video file should not cause new hardlinks
        txt = source / "AniShow" / "info.nfo"
        txt.write_text("info")
        time.sleep(0.2)
        assert not (target / "AniShow" / "info.nfo").exists()
        assert len(repo.list()) == 1

        # Test reload when directory_sync_enabled is disabled
        settings.directory_sync_enabled = False
        watcher.reload()
        assert len(watcher._watches) == 0

        # Create another video while disabled -> no sync
        ep2 = source / "AniShow" / "S01E02.mkv"
        ep2.write_bytes(b"episode-2")
        time.sleep(0.2)
        assert not (target / "AniShow" / "S01E02.mkv").exists()

        # Re-enable and reload -> watch resumes
        settings.directory_sync_enabled = True
        watcher.reload()
        assert len(watcher._watches) == 1

    finally:
        watcher.stop()
        assert watcher.running is False


def test_directory_sync_repository_get_and_not_found(tmp_path):
    service, _, _, _, _, settings, _, repo = sync_env(tmp_path)
    run = repo.save({"id": "test-run-1", "source_kind": "directory_sync", "status": "done"})
    assert repo.get("test-run-1")["id"] == "test-run-1"
    import pytest
    from ainas.errors import NotFoundError
    with pytest.raises(NotFoundError):
        repo.get("non-existent-run")


def test_directory_sync_ignores_symlinks_and_nonexistent_source(tmp_path):
    service, rule, source, target, _, settings, rules, repo = sync_env(tmp_path)

    # Nonexistent source
    bad_rule = dict(rule)
    bad_rule["id"] = "bad-source"
    bad_rule["source_path"] = str(tmp_path / "nas" / "Downloads" / "DoesNotExist")
    report = service.scan_changed(bad_rule["id"], [tmp_path / "nas" / "Downloads" / "DoesNotExist" / "01.mp4"], is_auto=True)
    # When rule is not in rules list, returns early
    assert report["linked"] == 0

    # Add bad rule to rules list and test _sync_rule error
    rules.rules.append(bad_rule)
    report_bad = service.scan("bad-source")
    assert "不存在" in (report_bad["runs"][0]["error"] or "")

    # Test symlink handling
    real_video = source / "real.mp4"
    real_video.write_bytes(b"video")
    link_video = source / "link.mp4"
    try:
        link_video.symlink_to(real_video)
        rep = service.scan(rule["id"])
        # Symlink itself is not linked as a separate video
        assert not (target / "link.mp4").is_symlink()
    except OSError:
        pass


def test_directory_sync_watcher_edge_cases(tmp_path):
    service, rule, source, target, _, settings, rules, repo = sync_env(tmp_path)

    # Watcher without observer class
    no_obs = DirectorySyncWatcher(service, rules, settings, observer_cls=False)
    no_obs.start()
    assert no_obs.running is False
    no_obs.stop()

    # Event handler filtering
    watcher = DirectorySyncWatcher(service, rules, settings)
    handler = DirectorySyncEventHandler(watcher, rule["id"], source)

    class DummyEvent:
        def __init__(self, event_type, src_path, dest_path=None, is_directory=False):
            self.event_type = event_type
            self.src_path = src_path
            self.dest_path = dest_path
            self.is_directory = is_directory

    # Deleted / opened / dir modified events should be ignored
    handler.on_any_event(DummyEvent("deleted", str(source / "del.mp4")))
    handler.on_any_event(DummyEvent("opened", str(source / "open.mp4")))
    handler.on_any_event(DummyEvent("modified", str(source), is_directory=True))
    handler.on_any_event(DummyEvent("created", str(source / ".hidden.mp4")))
    handler.on_any_event(DummyEvent("created", str(source / "sample.mp4")))
    handler.on_any_event(DummyEvent("created", str(tmp_path / "outside.mp4")))
    assert len(watcher._pending_paths) == 0

    # Queue event while not running does nothing
    watcher.queue_event(rule["id"], source / "test.mp4")
    assert len(watcher._pending_paths) == 0


def test_directory_sync_skips_when_inode_already_in_target_library(tmp_path):
    service, rule, source, target, _, settings, rules, repo = sync_env(tmp_path)
    source_file = source / "MyShow.mp4"
    source_file.write_bytes(b"content")

    # Suppose AVS naming or user already linked it into a structured subfolder
    structured = target / "MyShow (2024)" / "MyShow.S01E01.mp4"
    structured.parent.mkdir(parents=True, exist_ok=True)
    os.link(source_file, structured)

    # Now directory sync runs on source_file
    report = service.scan(rule["id"])

    assert report["linked"] == 0
    assert report["runs"][0]["already_linked"] == 1
    # Flat duplicate was NOT created in target root
    assert not (target / "MyShow.mp4").exists()
    assert structured.exists()


def test_deduplicate_library_links_removes_shallow_duplicate_retaining_structured(tmp_path):
    service, rule, source, target, _, settings, rules, repo = sync_env(tmp_path)
    source_file = source / "MyMovie.mp4"
    source_file.write_bytes(b"movie-bytes")

    # Structured link in movie subfolder
    structured = target / "MyMovie (2024)" / "MyMovie.2160p.mp4"
    structured.parent.mkdir(parents=True, exist_ok=True)
    os.link(source_file, structured)

    # Accidental duplicate flat link in target root
    flat = target / "MyMovie.mp4"
    os.link(source_file, flat)

    # Dry run
    dry_res = service.deduplicate_library_links(dry_run=True)
    assert dry_res["found"] == 1
    assert dry_res["unlinked"] == 0
    assert structured.exists()
    assert flat.exists()

    # Execute
    exec_res = service.deduplicate_library_links(dry_run=False)
    assert exec_res["found"] == 1
    assert exec_res["unlinked"] == 1
    assert structured.exists()
    assert not flat.exists()
    assert source_file.exists()
