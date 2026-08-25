from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ainas.config import Settings
from ainas.errors import AppError, ConflictError, UpstreamError
from ainas.medialib import MediaLibraryService
from ainas.naming import EmbyNamingPlanner, NamingJobRepository, NamingPlan, NamingService, safe_name
from ainas.path_rules import PathRuleRepository
from ainas.quality import build_release


HASH = "a" * 40
MAGNET = "magnet:?xt=urn:btih:" + HASH + "&dn=DHF.2018.1080p.mp4"


def movie_release():
    return build_release(
        "大黄蜂",
        "DHF.2018.1080p.mp4",
        "https://sixv.test/dy/1.html",
        MAGNET,
        "movie",
        2018,
    )


def tv_release():
    return build_release(
        "漫长的季节[第二季]",
        "02.mp4",
        "https://sixv.test/dlz/1.html",
        "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=02.mp4",
        "tv",
        2026,
    )


def anime_release():
    return build_release(
        "Rick and Morty[第七季]",
        "01.mkv",
        "https://sixv.test/dm/1.html",
        "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=01.mkv",
        "anime",
        2026,
    )


def custom_release():
    return build_release(
        "Python 入门课程",
        "Python-Course-Pack",
        "https://resources.test/learning/1.html",
        "magnet:?xt=urn:btih:" + "d" * 40 + "&dn=Python-Course-Pack",
        "custom",
    )


def test_movie_plan_corrects_obfuscated_link_name_and_subtitle():
    plan = EmbyNamingPlanner.from_release(movie_release())
    assert plan.root_name == "大黄蜂 (2018)"
    assert plan.link_name == "DHF.2018.1080p.mp4"
    preview = EmbyNamingPlanner().plan_files(
        [
            {"name": "DHF.2018.1080p.mp4", "size": 1000},
            {"name": "DHF.2018.1080p.chs.srt", "size": 10},
            {"name": "sample.mp4", "size": 5},
        ],
        plan,
    )
    renamed = {item["old_path"]: item["new_path"] for item in preview["operations"]}
    assert renamed["DHF.2018.1080p.mp4"] == "大黄蜂 (2018) - 1080p.mp4"
    assert renamed["DHF.2018.1080p.chs.srt"] == "大黄蜂 (2018) - 1080p.zh-CN.srt"
    assert renamed["sample.mp4"] == "大黄蜂-Sample (2018) - 1080p.mp4"


def test_tv_plan_turns_bare_numbers_into_emby_episode_names():
    plan = EmbyNamingPlanner.from_release(tv_release())
    assert plan.media_name == "漫长的季节"
    assert plan.root_name == "漫长的季节 (2026)"
    assert plan.season == 2
    preview = EmbyNamingPlanner().plan_files(
        [
            {"name": "第2季/01.mp4", "size": 100},
            {"name": "第2季/02.mkv", "size": 200},
            {"name": "第2季/02.chs.ass", "size": 2},
        ],
        plan,
    )
    renamed = {item["old_path"]: item["new_path"] for item in preview["operations"]}
    assert renamed["第2季/01.mp4"] == "第2季/漫长的季节 - S02E01.mp4"
    assert renamed["第2季/02.mkv"] == "第2季/漫长的季节 - S02E02.mkv"
    assert renamed["第2季/02.chs.ass"] == "第2季/漫长的季节 - S02E02.zh-CN.ass"
    assert preview["folder_operations"] == [{"kind": "folder", "old_path": "第2季", "new_path": "Season 02"}]


def test_tv_plan_normalizes_real_sixv_numeric_episode_pack():
    release = build_release(
        "重器[全集]",
        "重器.2160p",
        "https://sixv.test/dlz/1.html",
        "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=重器.2160p",
        "tv",
        2026,
    )
    plan = replace(EmbyNamingPlanner.from_release(release), video_format="2160p.HD")
    preview = EmbyNamingPlanner().plan_files(
        [
            {
                "name": "重器.2160p/01.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
                "size": 100,
            },
            {
                "name": "重器.2160p/02.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
                "size": 100,
            },
        ],
        plan,
    )

    assert plan.media_name == "重器"
    assert plan.root_name == "重器 (2026)"
    assert preview["operations"] == [
        {
            "kind": "file",
            "old_path": "重器.2160p/01.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
            "new_path": "重器.2160p/重器 - S01E01 - 2160p.HD.mkv",
        },
        {
            "kind": "file",
            "old_path": "重器.2160p/02.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
            "new_path": "重器.2160p/重器 - S01E02 - 2160p.HD.mkv",
        },
    ]
    assert preview["folder_operations"] == [
        {"kind": "folder", "old_path": "重器.2160p", "new_path": "Season 01"}
    ]
    assert preview["unresolved_episodes"] == []


def test_anime_uses_tv_style_emby_naming():
    plan = EmbyNamingPlanner.from_release(anime_release())
    preview = EmbyNamingPlanner().plan_files([{"name": "01.mkv", "size": 100}], plan)
    assert plan.media_type == "anime"
    assert plan.root_name == "Rick and Morty (2026)"
    assert preview["operations"][0]["new_path"] == "Rick and Morty - S07E01.mkv"


def test_optional_template_fields_render_without_empty_separators():
    movie_plan = replace(
        EmbyNamingPlanner.from_release(movie_release()),
        original_title="Bumblebee",
        part="Part1",
        edition="IMAX",
        video_format="1080p.BluRay.x265",
    )
    movie = EmbyNamingPlanner().plan_files([{"name": "DHF.2160p.mkv", "size": 100}], movie_plan)
    assert movie["operations"][0]["new_path"] == "大黄蜂.Bumblebee-Part1 (2018) - IMAX - 2160p.BluRay.x265.mkv"

    episode_plan = replace(
        EmbyNamingPlanner.from_release(tv_release()),
        original_title="The Long Season",
        episode_title="重逢",
        video_format="1080p.WEB-DL",
    )
    episode = EmbyNamingPlanner().plan_files([{"name": "02.mkv", "size": 100}], episode_plan)
    assert episode["operations"][0]["new_path"] == "漫长的季节.The Long Season - S02E02 - 重逢 - 1080p.WEB-DL.mkv"


def test_custom_plan_does_not_rename_files_or_folders():
    plan = EmbyNamingPlanner.from_release(custom_release())
    preview = EmbyNamingPlanner().plan_files(
        [{"name": "Python-Course-Pack/第一章/01.mp4", "size": 100}, {"name": "Python-Course-Pack/讲义.pdf", "size": 10}],
        plan,
    )
    assert plan.media_type == "custom"
    assert preview["file_count"] == 2
    assert preview["operations"] == []
    assert preview["folder_operations"] == []


def test_movie_multiversion_and_safe_name_rules():
    plan = EmbyNamingPlanner.from_release(movie_release())
    preview = EmbyNamingPlanner().plan_files(
        [
            {"name": "DHF.2160p.mkv", "size": 200},
            {"name": "DHF.1080p.mkv", "size": 100},
            {"name": "trailer.mkv", "size": 5},
        ],
        plan,
    )
    targets = [item["new_path"] for item in preview["operations"]]
    assert "大黄蜂-Part1 (2018) - 2160p.mkv" in targets
    assert "大黄蜂-Part2 (2018) - 1080p.mkv" in targets
    assert "大黄蜂-Trailer (2018) - 1080p.mkv" in targets
    assert safe_name('A/B:C*D?"E<>|') == "A B C D E"


def test_repository_is_persistent_and_idempotent(tmp_path):
    repository = NamingJobRepository(tmp_path / "naming.json")
    plan = EmbyNamingPlanner.from_release(movie_release())
    first = repository.upsert(HASH, plan, "sixv-movie")
    second = repository.upsert(HASH, plan, "sixv-movie")
    assert first["id"] == second["id"]
    assert repository.get(first["id"])["status"] == "pending"
    assert repository.update(first["id"], {"status": "completed"})["status"] == "completed"
    with pytest.raises(Exception):
        repository.get("missing-job")


def test_discard_record_abandons_safe_jobs_without_touching_downloader(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    plan = EmbyNamingPlanner.from_release(movie_release())

    pending = repository.upsert("1" * 40, plan, settings.qb_movie_category)
    assert service.discard_record(pending["id"])["id"] == pending["id"]
    assert qb.calls == []

    waiting = repository.upsert("2" * 40, plan, settings.qb_movie_category)
    repository.update(
        waiting["id"],
        {
            "status": "completed",
            "hardlink_status": "waiting_download",
            "rename_checkpoint": {"files": 1, "folders": 0, "torrent": True},
        },
    )
    assert service.discard_record(waiting["id"])["id"] == waiting["id"]
    assert qb.calls == []


def test_discard_record_rejects_active_job_after_rename_started(tmp_path):
    settings = naming_settings(tmp_path)
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, FakeQB())
    item = repository.upsert("3" * 40, EmbyNamingPlanner.from_release(movie_release()), settings.qb_movie_category)
    repository.update(
        item["id"],
        {"status": "retrying", "rename_checkpoint": {"files": 1, "folders": 0, "torrent": False}},
    )

    with pytest.raises(ConflictError, match="rename operations"):
        service.discard_record(item["id"])


def test_missing_downloader_task_eventually_fails_waiting_hardlink(tmp_path):
    settings = naming_settings(tmp_path, naming_max_attempts=2)
    qb = FakeQB(torrent=False)
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=FakeHardlinker())
    item = repository.upsert("4" * 40, EmbyNamingPlanner.from_release(movie_release()), settings.qb_movie_category)
    repository.update(item["id"], {"status": "completed", "hardlink_status": "waiting_download"})

    service.check(item["id"])
    first = repository.get(item["id"])
    assert first["hardlink_status"] == "waiting_download"
    assert first["hardlink_attempts"] == 1
    assert first["hardlink_error"] == "torrent is absent from downloader"

    service.check(item["id"])
    failed = repository.get(item["id"])
    assert failed["hardlink_status"] == "failed"
    assert failed["hardlink_attempts"] == 2


class FakeQB:
    def __init__(self, files=None, torrent=True, fail_rename=False):
        self.file_values = files if files is not None else [
            {"name": "DHF.mp4", "size": 1000, "progress": 1.0, "priority": 1}
        ]
        self.torrent_value = {"hash": HASH, "progress": 1.0} if torrent else None
        self.fail_rename = fail_rename
        self.calls = []
        self.save_paths = []

    def add_download(self, link, category, rename=None, save_path=None, paused=False):
        self.calls.append(("add", category, rename, paused) if paused else ("add", category, rename))
        self.save_paths.append(save_path)
        return {"qb_task_id": HASH, "category": category}

    def add_torrent_file(self, content, filename, category, rename=None, save_path=None, paused=False):
        self.calls.append(("add_torrent", category, rename, paused) if paused else ("add_torrent", category, rename))
        self.save_paths.append(save_path)
        return {"qb_task_id": HASH, "category": category, "torrent_name": filename}

    def torrent_info(self, hash_value):
        self.calls.append(("info", hash_value))
        return self.torrent_value

    def files(self, hash_value):
        self.calls.append(("files", hash_value))
        return self.file_values

    def rename_file(self, hash_value, old_path, new_path):
        self.calls.append(("rename_file", old_path, new_path))
        if self.fail_rename:
            raise UpstreamError("qBittorrent", "rename failed")

    def rename_folder(self, hash_value, old_path, new_path):
        self.calls.append(("rename_folder", old_path, new_path))

    def rename_torrent(self, hash_value, name):
        self.calls.append(("rename_torrent", name))

    def set_category(self, hash_value, category):
        self.calls.append(("category", category))

    def resume(self, hash_value):
        self.calls.append(("resume", hash_value))

    def set_file_priorities(self, hash_value, selected_indices, skipped_indices):
        self.calls.append(("priorities", list(selected_indices), list(skipped_indices)))


class FakeHardlinker:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    @property
    def enabled(self):
        return True

    def link_completed(self, torrent, files, plan, naming_result=None):
        self.calls.append((torrent, files, plan, naming_result))
        if self.fail:
            raise UpstreamError("media library", "link failed")
        return {"status": "done", "linked": 1, "skipped": 0, "files": []}


class BatchHardlinker(FakeHardlinker):
    def link_completed(self, torrent, files, plan, naming_result=None):
        values = [dict(item) for item in files]
        self.calls.append((torrent, values, plan, naming_result))
        results = [
            {
                "status": "linked",
                "source": f"/downloads/{item['index']}",
                "target": f"/library/{item['index']}",
            }
            for item in values
        ]
        return {
            "status": "done",
            "linked": len(results),
            "skipped": 0,
            "already_linked": 0,
            "conflicts": 0,
            "target": "/library",
            "files": results,
        }


class FakeVerifyingHardlinker(FakeHardlinker):
    def __init__(self):
        super().__init__()
        self.verify_calls = 0

    def verify_named_sources(self, torrent, files, naming_result):
        self.verify_calls += 1
        if self.verify_calls == 1:
            raise UpstreamError("media library", "planned path is not ready")
        return {"status": "ready", "count": len(files)}


class FilesystemQB(FakeQB):
    def __init__(self, host_root: Path, mount_root: Path):
        super().__init__(files=[])
        self.host_root = host_root
        self.mount_root = mount_root
        self.progress = 0.5
        self.save_path = None

    def _mounted(self, host_path: Path) -> Path:
        return self.mount_root / host_path.relative_to(self.host_root)

    def add_download(self, link, category, rename=None, save_path=None):
        result = super().add_download(link, category, rename, save_path)
        self.save_path = Path(save_path)
        source = self._mounted(self.save_path) / "Season 02" / "02.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"episode-two")
        self.file_values = [
            {
                "name": "Season 02/02.mp4",
                "size": source.stat().st_size,
                "progress": self.progress,
                "priority": 1,
            }
        ]
        return result

    def torrent_info(self, hash_value):
        self.calls.append(("info", hash_value))
        return {"hash": hash_value, "progress": self.progress, "save_path": str(self.save_path)}

    def files(self, hash_value):
        self.calls.append(("files", hash_value))
        self.file_values[0]["progress"] = self.progress
        return [dict(item) for item in self.file_values]

    def rename_file(self, hash_value, old_path, new_path):
        super().rename_file(hash_value, old_path, new_path)
        source = self._mounted(self.save_path) / Path(old_path)
        destination = self._mounted(self.save_path) / Path(new_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        self.file_values[0]["name"] = new_path


def naming_settings(tmp_path: Path, **overrides):
    values = {"data_dir": tmp_path, "scheduler_enabled": False, "naming_max_attempts": 2}
    values.update(overrides)
    return Settings(**values)


def test_naming_service_stages_renames_and_releases_torrent(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    result = service.add_download(movie_release(), "sixv-movie")
    assert result["current_category"] == settings.qb_naming_category
    assert result["planned_name"] == "大黄蜂 (2018)"
    assert result["naming_status"] == "pending"
    assert qb.save_paths == [settings.download_movie_path / "大黄蜂 (2018)"]
    assert not any(call[0].startswith("rename") for call in qb.calls)

    report = service.check(result["naming_job_id"])
    assert report["completed"] == 1
    assert ("category", "sixv-movie") in qb.calls
    assert any(call[0] == "rename_file" for call in qb.calls)


def test_bundle_is_paused_then_only_missing_episode_files_are_selected(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(
        files=[
            {"index": 0, "name": "Season 02/01.mkv", "size": 100, "progress": 0, "priority": 1},
            {"index": 1, "name": "Season 02/02.mkv", "size": 100, "progress": 0, "priority": 1},
            {"index": 2, "name": "Season 02/03.mkv", "size": 100, "progress": 0, "priority": 1},
        ]
    )
    qb.torrent_value = {"hash": HASH, "progress": 0}
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)

    result = service.add_download(tv_release(), settings.qb_tv_category, wanted_episodes=["S02E02"])
    first = service.check(result["naming_job_id"])
    job = repository.get(result["naming_job_id"])

    assert ("add", settings.qb_naming_category, "漫长的季节 (2026)", True) in qb.calls
    assert ("priorities", [1], [0, 2]) in qb.calls
    assert ("resume", HASH) in qb.calls
    assert first["completed"] == 0
    assert job["status"] == "waiting_download"
    assert job["selection"]["matched_episodes"] == ["S02E02"]


def test_bundle_is_not_left_paused_when_no_avs_postprocessing_job_exists(tmp_path):
    settings = naming_settings(
        tmp_path,
        naming_enabled=False,
        medialib_hardlink_enabled=False,
    )
    qb = FakeQB()
    service = NamingService(settings, NamingJobRepository(settings.naming_jobs_path), qb)

    result = service.add_download(tv_release(), settings.qb_tv_category, wanted_episodes=["S02E02"])

    assert result["naming_job_id"] is None
    assert ("add", settings.qb_tv_category, None) in qb.calls
    assert not any(len(call) == 4 and call[0] == "add" and call[3] is True for call in qb.calls)


def test_unseparable_bundle_stays_paused_for_confirmation(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(files=[{"index": 0, "name": "全集.mkv", "size": 100, "progress": 0, "priority": 1}])
    qb.torrent_value = {"hash": HASH, "progress": 0}
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)

    result = service.add_download(tv_release(), settings.qb_tv_category, wanted_episodes=["S02E02"])
    service.check(result["naming_job_id"])
    job = repository.get(result["naming_job_id"])

    assert job["status"] == "waiting_selection"
    assert job["selection"]["reason"] == "episode-files-not-separable"
    assert not any(call[0] in {"priorities", "resume"} for call in qb.calls)


def test_naming_uses_downloader_container_path_from_rule(tmp_path):
    settings = naming_settings(tmp_path)
    rules = PathRuleRepository(settings)
    rules.update("default-movie", {"downloader_path": "/Downloads/Movie"})
    qb = FakeQB()
    service = NamingService(settings, NamingJobRepository(settings.naming_jobs_path), qb, path_rules=rules)

    service.add_download(movie_release(), settings.qb_movie_category)

    assert qb.save_paths == [Path("/Downloads/Movie/大黄蜂 (2018)")]
    assert settings.download_movie_path == Path("/volume1/video/Downloads/Movie")


def test_naming_verifies_realized_paths_before_marking_job_complete(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB()
    hardlinker = FakeVerifyingHardlinker()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)

    result = service.add_download(movie_release(), settings.qb_movie_category)
    report = service.check(result["naming_job_id"])

    assert report["completed"] == 1
    assert hardlinker.verify_calls == 2
    assert repository.get(result["naming_job_id"])["status"] == "completed"


def test_download_to_named_file_to_hardlink_chain_uses_only_avs_state(tmp_path):
    host = tmp_path / "nas-video"
    mount = tmp_path / "medialib"
    mount.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        naming_max_attempts=2,
        medialib_base_path=host,
        medialib_mount_path=mount,
        downloads_base_path=host / "Downloads",
        download_movie_path=host / "Downloads" / "Movie",
        download_tv_path=host / "Downloads" / "TV",
        download_anime_path=host / "Downloads" / "Anime",
        download_custom_path=host / "Downloads" / "Custom",
        medialib_movie_path=host / "video" / "movies",
        medialib_tv_path=host / "video" / "tv",
        medialib_anime_path=host / "video" / "anime",
        medialib_custom_path=host / "video" / "custom",
    )
    qb = FilesystemQB(host, mount)
    repository = NamingJobRepository(settings.naming_jobs_path)
    hardlinker = MediaLibraryService(settings)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)

    result = service.add_download(tv_release(), settings.qb_tv_category)
    first = service.check(result["naming_job_id"])
    assert first["completed"] == 0
    assert repository.get(result["naming_job_id"])["status"] == "waiting_download"

    qb.progress = 1.0
    second = service.check(result["naming_job_id"])
    updated = repository.get(result["naming_job_id"])
    source = (
        mount
        / "Downloads"
        / "TV"
        / "漫长的季节 (2026)"
        / "Season 02"
        / "漫长的季节 - S02E02.mp4"
    )
    target = mount / "video" / "tv" / "漫长的季节 (2026)" / "Season 02" / source.name

    assert second["completed"] == 1
    assert updated["status"] == "completed"
    assert updated["hardlink_status"] == "done"
    assert source.exists() and target.exists()
    assert source.stat().st_ino == target.stat().st_ino


def test_torrent_upload_keeps_staging_label_but_uses_final_download_path(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    result = service.add_torrent_file(movie_release(), settings.qb_movie_category, b"torrent", "movie.torrent", HASH)
    assert result["current_category"] == settings.qb_naming_category
    assert result["naming_status"] == "pending"
    assert qb.save_paths == [settings.download_movie_path / "大黄蜂 (2018)"]


def test_naming_service_waits_for_metadata_then_completes(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(torrent=False)
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    result = service.add_download(movie_release(), "sixv-movie")
    assert result["naming_status"] == "pending"
    first = service.check(result["naming_job_id"])
    assert first["completed"] == 0
    assert repository.get(result["naming_job_id"])["status"] == "waiting_metadata"
    qb.torrent_value = {"hash": HASH, "progress": 1.0}
    report = service.check(result["naming_job_id"])
    assert report["completed"] == 1


def test_naming_service_retries_then_releases_failed_job(tmp_path):
    settings = naming_settings(tmp_path, naming_max_attempts=1)
    qb = FakeQB(fail_rename=True)
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    result = service.add_download(movie_release(), "sixv-movie")
    assert result["naming_status"] == "pending"
    service.check(result["naming_job_id"])
    assert repository.get(result["naming_job_id"])["status"] == "failed"
    assert ("category", "sixv-movie") in qb.calls
    assert any(call[0] == "resume" for call in qb.calls)


def test_naming_disabled_or_non_magnet_does_not_stage(tmp_path):
    settings = naming_settings(tmp_path, naming_enabled=False)
    qb = FakeQB()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    disabled = service.add_download(movie_release(), "sixv-movie")
    assert disabled["naming_status"] == "disabled"
    assert disabled["current_category"] == "sixv-movie"


def test_naming_waits_when_no_selected_file_is_complete_then_hardlinks(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB()
    qb.torrent_value = {"hash": HASH, "progress": 0.5}
    qb.file_values[0]["progress"] = 0.5
    hardlinker = FakeHardlinker()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)
    result = service.add_download(movie_release(), "sixv-movie")
    job = repository.get(result["naming_job_id"])
    assert job["status"] == "pending"
    report = service.check(job["id"])
    assert report["completed"] == 0
    job = repository.get(job["id"])
    assert job["status"] == "waiting_download"
    assert job["hardlink_status"] is None
    assert not any(call[0].startswith("rename") for call in qb.calls)
    assert not hardlinker.calls

    qb.torrent_value["progress"] = 1.0
    qb.file_values[0]["progress"] = 1.0
    report = service.check(job["id"])
    assert report["completed"] == 1
    updated = repository.get(job["id"])
    assert updated["hardlink_status"] == "done"
    assert len(hardlinker.calls) == 1
    assert hardlinker.calls[0][3] == updated["result"]


def test_completed_episodes_are_named_and_hardlinked_before_the_torrent_finishes(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(
        files=[
            {"index": 0, "name": "重器.2160p/01.2160p.mkv", "size": 100, "progress": 1.0, "priority": 1},
            {"index": 1, "name": "重器.2160p/02.2160p.mkv", "size": 100, "progress": 1.0, "priority": 1},
            {"index": 2, "name": "重器.2160p/03.2160p.mkv", "size": 100, "progress": 0.5, "priority": 1},
        ]
    )
    qb.torrent_value = {"hash": HASH, "progress": 0.8}
    hardlinker = BatchHardlinker()
    repository = NamingJobRepository(settings.naming_jobs_path)
    plan = NamingPlan(
        media_type="tv",
        media_name="重器[全集]",
        root_name="重器[全集] (2026)",
        year=2026,
        season=None,
        episode=None,
        link_name="重器.2160p",
        video_format="2160p.HD",
    )
    job = repository.upsert(HASH, plan, settings.qb_tv_category)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)

    first = service.check(job["id"])
    partial = repository.get(job["id"])

    assert first["completed"] == 0
    assert partial["status"] == "waiting_download"
    assert partial["hardlink_status"] == "waiting_download"
    assert partial["processed_file_indices"] == [0, 1]
    assert partial["plan"]["root_name"] == "重器 (2026)"
    assert [item[1] for item in qb.calls if item[0] == "rename_file"] == [
        "重器.2160p/01.2160p.mkv",
        "重器.2160p/02.2160p.mkv",
    ]
    assert [item["index"] for item in hardlinker.calls[0][1]] == [0, 1]
    assert partial["hardlink_result"]["linked"] == 2
    assert not any(call[0] in {"rename_folder", "rename_torrent", "category"} for call in qb.calls)
    with pytest.raises(AppError, match="processing has started"):
        service.preview_corrected_plan(job["id"], {"media_name": "错误标题"})

    qb.file_values[2]["progress"] = 1.0
    qb.torrent_value["progress"] = 1.0
    second = service.check(job["id"])
    completed = repository.get(job["id"])

    assert second["completed"] == 1
    assert completed["status"] == "completed"
    assert completed["hardlink_status"] == "done"
    assert completed["processed_file_indices"] == [0, 1, 2]
    assert [item[1] for item in qb.calls if item[0] == "rename_file"] == [
        "重器.2160p/01.2160p.mkv",
        "重器.2160p/02.2160p.mkv",
        "重器.2160p/03.2160p.mkv",
    ]
    assert [item["index"] for item in hardlinker.calls[1][1]] == [2]
    assert completed["hardlink_result"]["linked"] == 3
    assert sum(call[0] == "rename_folder" for call in qb.calls) == 1
    assert sum(call[0] == "rename_torrent" for call in qb.calls) == 1
    assert sum(call[0] == "category" for call in qb.calls) == 1


def test_unresolved_episode_never_reaches_hardlink(tmp_path):
    settings = naming_settings(tmp_path, naming_max_attempts=1)
    qb = FakeQB(
        files=[
            {
                "name": "重器.2160p/幕后花絮.2160p.HD.mkv",
                "size": 100,
                "progress": 1.0,
                "priority": 1,
            }
        ]
    )
    hardlinker = FakeHardlinker()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)
    result = service.add_download(tv_release(), settings.qb_tv_category)

    service.check(result["naming_job_id"])
    job = repository.get(result["naming_job_id"])

    assert job["status"] == "failed"
    assert job["hardlink_status"] is None
    assert job["result"]["unresolved_episodes"] == ["重器.2160p/幕后花絮.2160p.HD.mkv"]
    assert "无法识别集号" in job["last_error"]
    assert hardlinker.calls == []


def test_pending_legacy_bundle_plan_is_canonicalized_before_rename(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(
        files=[
            {
                "name": "重器.2160p/27.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
                "size": 100,
                "progress": 1.0,
                "priority": 1,
            }
        ]
    )
    repository = NamingJobRepository(settings.naming_jobs_path)
    plan = NamingPlan(
        media_type="tv",
        media_name="重器[全集]",
        root_name="重器[全集] (2026)",
        year=2026,
        season=None,
        episode=None,
        link_name="重器.2160p",
        video_format="2160p.HD",
    )
    job = repository.upsert(HASH, plan, settings.qb_tv_category)
    service = NamingService(settings, repository, qb)

    service.check(job["id"])
    updated = repository.get(job["id"])

    assert updated["status"] == "completed"
    assert updated["plan"]["media_name"] == "重器"
    assert updated["plan"]["root_name"] == "重器 (2026)"
    assert ("rename_file", "重器.2160p/27.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv", "重器.2160p/重器 - S01E27 - 2160p.HD.mkv") in qb.calls
    assert ("rename_folder", "重器.2160p", "Season 01") in qb.calls


def test_custom_download_uses_final_category_and_hardlinks_without_rename(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(files=[{"name": "Python-Course-Pack/讲义.pdf", "size": 10}])
    qb.torrent_value = {"hash": "d" * 40, "progress": 1.0}
    hardlinker = FakeHardlinker()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)

    result = service.add_download(custom_release(), settings.qb_custom_category)

    assert result["current_category"] == settings.qb_custom_category
    assert result["naming_status"] == "pending"
    service.check(result["naming_job_id"])
    updated = repository.get(result["naming_job_id"])
    assert updated["status"] == "completed"
    assert updated["hardlink_status"] == "done"
    assert ("add", settings.qb_custom_category, None) in qb.calls
    assert qb.save_paths == [settings.download_custom_path / "Python-Course-Pack"]
    assert not any(call[0].startswith("rename") for call in qb.calls)
    assert hardlinker.calls[0][2].media_type == "custom"


def test_hardlink_failure_retries_and_can_be_manually_retried(tmp_path):
    settings = naming_settings(tmp_path, naming_max_attempts=1)
    qb = FakeQB()
    qb.torrent_value = {"hash": HASH, "progress": 1.0}
    hardlinker = FakeHardlinker(fail=True)
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)
    result = service.add_download(movie_release(), "sixv-movie")
    service.check(result["naming_job_id"])
    job = repository.get(result["naming_job_id"])
    assert job["hardlink_status"] == "failed"
    assert len(hardlinker.calls) == 1
    assert "link failed" in job["hardlink_error"]

    hardlinker.fail = False
    report = service.check(job["id"])
    assert report["completed"] == 1
    assert repository.get(job["id"])["hardlink_status"] == "done"
