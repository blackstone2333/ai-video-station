from __future__ import annotations

from pathlib import Path

import pytest

from ainas.config import Settings
from ainas.errors import UpstreamError
from ainas.naming import EmbyNamingPlanner, NamingJobRepository, NamingPlan, NamingService, safe_name
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
    assert renamed["DHF.2018.1080p.mp4"] == "大黄蜂 (2018).mp4"
    assert renamed["DHF.2018.1080p.chs.srt"] == "大黄蜂 (2018).zh-CN.srt"
    assert renamed["sample.mp4"] == "大黄蜂 (2018) - sample.mp4"


def test_tv_plan_turns_bare_numbers_into_emby_episode_names():
    plan = EmbyNamingPlanner.from_release(tv_release())
    assert plan.media_name == "漫长的季节"
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


def test_anime_uses_tv_style_emby_naming():
    plan = EmbyNamingPlanner.from_release(anime_release())
    preview = EmbyNamingPlanner().plan_files([{"name": "01.mkv", "size": 100}], plan)
    assert plan.media_type == "anime"
    assert preview["operations"][0]["new_path"] == "Rick and Morty - S07E01.mkv"


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
    assert "大黄蜂 (2018) - 2160p.mkv" in targets
    assert "大黄蜂 (2018) - 1080p.mkv" in targets
    assert "大黄蜂 (2018) - trailer.mkv" in targets
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


class FakeQB:
    def __init__(self, files=None, torrent=True, fail_rename=False):
        self.file_values = files if files is not None else [{"name": "DHF.mp4", "size": 1000}]
        self.torrent_value = {"hash": HASH} if torrent else None
        self.fail_rename = fail_rename
        self.calls = []

    def add_download(self, link, category, rename=None):
        self.calls.append(("add", category, rename))
        return {"qb_task_id": HASH, "category": category}

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


class FakeHardlinker:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    @property
    def enabled(self):
        return True

    def link_completed(self, torrent, files, plan):
        self.calls.append((torrent, files, plan))
        if self.fail:
            raise UpstreamError("media library", "link failed")
        return {"status": "done", "linked": 1, "skipped": 0, "files": []}


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
    assert result["naming_status"] == "completed"
    assert ("category", "sixv-movie") in qb.calls
    assert any(call[0] == "rename_file" for call in qb.calls)


def test_naming_service_waits_for_metadata_then_completes(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(torrent=False)
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    result = service.add_download(movie_release(), "sixv-movie")
    assert result["naming_status"] == "waiting_metadata"
    qb.torrent_value = {"hash": HASH}
    report = service.check(result["naming_job_id"])
    assert report["completed"] == 1


def test_naming_service_retries_then_releases_failed_job(tmp_path):
    settings = naming_settings(tmp_path, naming_max_attempts=1)
    qb = FakeQB(fail_rename=True)
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb)
    result = service.add_download(movie_release(), "sixv-movie")
    assert result["naming_status"] == "failed"
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


def test_naming_waits_for_complete_download_then_hardlinks(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB()
    qb.torrent_value = {"hash": HASH, "progress": 0.5}
    hardlinker = FakeHardlinker()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)
    result = service.add_download(movie_release(), "sixv-movie")
    job = repository.get(result["naming_job_id"])
    assert job["status"] == "completed"
    assert job["hardlink_status"] == "waiting_download"
    assert not hardlinker.calls

    qb.torrent_value["progress"] = 1.0
    report = service.check(job["id"])
    assert report["completed"] == 1
    updated = repository.get(job["id"])
    assert updated["hardlink_status"] == "done"
    assert len(hardlinker.calls) == 1


def test_custom_download_uses_final_category_and_hardlinks_without_rename(tmp_path):
    settings = naming_settings(tmp_path)
    qb = FakeQB(files=[{"name": "Python-Course-Pack/讲义.pdf", "size": 10}])
    qb.torrent_value = {"hash": "d" * 40, "progress": 1.0}
    hardlinker = FakeHardlinker()
    repository = NamingJobRepository(settings.naming_jobs_path)
    service = NamingService(settings, repository, qb, hardlinker=hardlinker)

    result = service.add_download(custom_release(), settings.qb_custom_category)

    assert result["current_category"] == settings.qb_custom_category
    assert result["naming_status"] == "completed"
    assert result["hardlink_status"] == "done"
    assert ("add", settings.qb_custom_category, None) in qb.calls
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
    job = repository.get(result["naming_job_id"])
    assert job["hardlink_status"] == "failed"
    assert len(hardlinker.calls) == 1
    assert "link failed" in job["hardlink_error"]

    hardlinker.fail = False
    report = service.check(job["id"])
    assert report["completed"] == 1
    assert repository.get(job["id"])["hardlink_status"] == "done"
