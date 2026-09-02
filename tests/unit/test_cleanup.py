from __future__ import annotations

import os
from pathlib import Path

import pytest

from ainas.cleanup import CleanupPlanRepository, CleanupService
from ainas.errors import ConflictError, ValidationAppError
from ainas.state import StateStore


class Rules:
    def __init__(self, values):
        self.values = values

    def list(self):
        return self.values


class Jobs:
    def __init__(self, values=()):
        self.values = list(values)

    def list(self):
        return self.values


class Downloader:
    def __init__(self):
        self.calls = []
        self.tasks = {}
        self.file_values = {}
        self.fail_delete = False

    def torrent_info(self, value):
        return self.tasks.get(value)

    def files(self, value):
        return self.file_values.get(value, [])

    def pause(self, value):
        self.calls.append(("pause", value))

    def resume(self, value):
        self.calls.append(("resume", value))

    def delete(self, value, delete_files=True):
        self.calls.append(("delete", value, delete_files))
        if self.fail_delete:
            raise RuntimeError("downloader delete failed")
        self.tasks.pop(value, None)


@pytest.fixture
def cleanup_env(settings, tmp_path):
    host = tmp_path / "host"
    mount = tmp_path / "mount"
    source = mount / "downloads"
    target = mount / "library"
    source.mkdir(parents=True)
    target.mkdir()
    settings.medialib_base_path = host
    settings.medialib_mount_path = mount
    rules = Rules(
        [
            {
                "id": "rule-movie",
                "enabled": True,
                "media_type": "movie",
                "source_path": str(host / "downloads"),
                "target_path": str(host / "library"),
            }
        ]
    )
    jobs = Jobs()
    downloader = Downloader()
    service = CleanupService(
        settings,
        CleanupPlanRepository(StateStore(tmp_path / "state.db")),
        rules,
        jobs,
        downloader,
    )
    return service, rules, jobs, downloader, source, target


def add_version(source, target, jobs, name, payload, *, media_type="movie", episode=None, torrent_hash=None, plan_episode=None):
    source_path = source / name
    target_path = target / name
    source_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(payload)
    os.link(source_path, target_path)
    plan = {"media_type": media_type, "media_name": "重器" if media_type != "movie" else "示例电影", "year": 2026}
    if plan_episode is not None:
        plan["episode"] = plan_episode
    elif episode is not None:
        plan["episode"] = episode
    jobs.values.append(
        {
            "torrent_hash": torrent_hash,
            "plan": plan,
            "hardlink_result": {"files": [{"source": str(source_path), "target": str(target_path)}]},
        }
    )
    return source_path, target_path


def group_versions(plan):
    assert len(plan["groups"]) == 1
    group = plan["groups"][0]
    return group, {Path((item["source_paths"] or item["target_paths"])[0]).name: item for item in group["versions"]}


def selection(group, *versions):
    return [{"group_id": group["id"], "delete_version_ids": [item["id"] for item in versions]}]


def test_same_inode_is_not_a_duplicate(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    movie = source / "Movie.1080p.mkv"
    movie.write_bytes(b"x")
    target_link = target / movie.name
    os.link(movie, target_link)
    jobs.values.append(
        {
            "plan": {"media_name": "Movie", "year": 2026},
            "hardlink_result": {"files": [{"source": str(movie), "target": str(target_link)}]},
        }
    )

    assert service.scan()["groups"] == []


def test_quality_and_space_policies_recommend_different_versions(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"4" * 100)
    add_version(source, target, jobs, "示例电影.720p.WEB-DL.mkv", b"7" * 20)

    quality_group, quality_versions = group_versions(service.scan("quality_first"))
    space_group, space_versions = group_versions(service.scan("space_first"))

    assert quality_group["recommended_keep_id"] == quality_versions["示例电影.2160p.BluRay.mkv"]["id"]
    assert space_group["recommended_keep_id"] == space_versions["示例电影.720p.WEB-DL.mkv"]["id"]


def test_user_can_delete_recommended_high_quality_version_and_keep_low_version(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    high_source, high_target = add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"4" * 100)
    low_source, low_target = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"1" * 40)
    plan = service.scan("quality_first")
    group, versions = group_versions(plan)
    high = versions[high_source.name]

    updated, results = service.execute(
        plan["id"], selection(group, high), False, "DELETE_SELECTED_DUPLICATES"
    )

    assert updated["status"] == "completed"
    assert results[0]["status"] == "completed"
    assert high_source.exists() and not high_target.exists()
    assert low_source.exists() and low_target.exists()


def test_every_selection_is_preflighted_before_any_target_is_removed(cleanup_env):
    service, rules, jobs, _, source, target = cleanup_env
    rules.values[0]["media_type"] = "tv"
    first_low, first_target = add_version(source, target, jobs, "重器 - S01E01 - 1080p.mkv", b"a", media_type="tv", episode="S01E01")
    add_version(source, target, jobs, "重器 - S01E01 - 2160p.mkv", b"b", media_type="tv", episode="S01E01")
    second_low, second_target = add_version(source, target, jobs, "重器 - S01E02 - 1080p.mkv", b"c", media_type="tv", episode="S01E02")
    add_version(source, target, jobs, "重器 - S01E02 - 2160p.mkv", b"d", media_type="tv", episode="S01E02")
    plan = service.scan()
    by_identity = {group["identity"]: group for group in plan["groups"]}
    first_group = next(group for identity, group in by_identity.items() if "S01E01" in identity)
    second_group = next(group for identity, group in by_identity.items() if "S01E02" in identity)
    first_version = next(item for item in first_group["versions"] if first_low.name in item["source_paths"][0])
    second_version = next(item for item in second_group["versions"] if second_low.name in item["source_paths"][0])
    second_target.unlink()
    second_target.write_bytes(b"stale")

    with pytest.raises(ConflictError, match="stale"):
        service.execute(
            plan["id"],
            [
                {"group_id": first_group["id"], "delete_version_ids": [first_version["id"]]},
                {"group_id": second_group["id"], "delete_version_ids": [second_version["id"]]},
            ],
            False,
            "DELETE_SELECTED_DUPLICATES",
        )

    assert first_target.exists()


def test_pack_level_episode_range_never_groups_different_episodes(cleanup_env):
    service, rules, jobs, _, source, target = cleanup_env
    rules.values[0]["media_type"] = "tv"
    add_version(source, target, jobs, "重器 - S01E01 - 1080p.mkv", b"one", media_type="tv", plan_episode="S01E01-E33")
    add_version(source, target, jobs, "重器 - S01E02 - 1080p.mkv", b"two", media_type="tv", plan_episode="S01E01-E33")

    assert service.scan()["groups"] == []


def test_source_delete_uses_downloader_for_a_complete_whole_torrent_and_allows_subtitles(cleanup_env):
    service, _, jobs, downloader, source, target = cleanup_env
    low_source, low_target = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"low", torrent_hash="low-hash")
    add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"high", torrent_hash="high-hash")
    downloader.tasks["low-hash"] = {"state": "uploading", "progress": 1.0}
    downloader.file_values["low-hash"] = [{"name": low_source.name}, {"name": "示例电影.zh.srt"}]
    plan = service.scan()
    group, versions = group_versions(plan)

    updated, _ = service.execute(
        plan["id"], selection(group, versions[low_source.name]), True, "DELETE_SELECTED_DUPLICATES"
    )

    assert updated["status"] == "completed"
    assert ("pause", "low-hash") in downloader.calls
    assert ("delete", "low-hash", True) in downloader.calls
    assert not low_target.exists()


def test_partial_multi_file_torrent_is_rejected_before_changes(cleanup_env):
    service, _, jobs, downloader, source, target = cleanup_env
    low_source, low_target = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"low", torrent_hash="pack")
    add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"high", torrent_hash="other")
    downloader.tasks["pack"] = {"state": "uploading", "progress": 1.0}
    downloader.file_values["pack"] = [{"name": low_source.name}, {"name": "Another.Movie.1080p.mkv"}]
    plan = service.scan()
    group, versions = group_versions(plan)

    with pytest.raises(ConflictError, match="partial multi-file"):
        service.execute(
            plan["id"], selection(group, versions[low_source.name]), True, "DELETE_SELECTED_DUPLICATES"
        )

    assert low_source.exists() and low_target.exists()
    assert downloader.calls == []


def test_missing_downloader_task_uses_contained_direct_source_delete(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    low_source, low_target = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"low", torrent_hash="gone")
    add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"high", torrent_hash="keep")
    plan = service.scan()
    group, versions = group_versions(plan)

    updated, results = service.execute(
        plan["id"], selection(group, versions[low_source.name]), True, "DELETE_SELECTED_DUPLICATES"
    )

    assert updated["status"] == "completed"
    assert results[0]["source_deleted_by"] == "avs"
    assert not low_source.exists() and not low_target.exists()


def test_downloader_failure_restores_links_and_retry_replaces_failure_result(cleanup_env):
    service, _, jobs, downloader, source, target = cleanup_env
    low_source, low_target = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"low", torrent_hash="low")
    add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"high", torrent_hash="high")
    downloader.tasks["low"] = {"state": "uploading", "progress": 1.0}
    downloader.file_values["low"] = [{"name": low_source.name}]
    downloader.fail_delete = True
    plan = service.scan()
    group, versions = group_versions(plan)
    failed, _ = service.execute(
        plan["id"], selection(group, versions[low_source.name]), True, "DELETE_SELECTED_DUPLICATES"
    )

    assert failed["status"] == "partial"
    assert low_source.exists() and low_target.exists()
    assert ("resume", "low") in downloader.calls

    downloader.fail_delete = False
    completed, results = service.retry(plan["id"])
    assert completed["status"] == "completed"
    assert results[0]["status"] == "completed"
    assert len(completed["results"]) == 1
    assert completed["results"][0]["status"] == "completed"


def test_retained_version_must_be_readable_before_selected_links_change(cleanup_env, monkeypatch):
    service, _, jobs, _, source, target = cleanup_env
    high_source, high_target = add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"high")
    low_source, _ = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"low")
    plan = service.scan()
    group, versions = group_versions(plan)
    original_open = Path.open

    def fail_retained_open(path, *args, **kwargs):
        if path == low_source:
            raise PermissionError("not readable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_retained_open)
    with pytest.raises(ConflictError, match="not readable"):
        service.execute(
            plan["id"], selection(group, versions[high_source.name]), False, "DELETE_SELECTED_DUPLICATES"
        )
    assert high_target.exists()


def test_symlink_replacement_is_rejected_and_confirmation_is_required(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    low_source, low_target = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"low")
    add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"high")
    plan = service.scan()
    group, versions = group_versions(plan)
    with pytest.raises(ValidationAppError, match="confirmation"):
        service.execute(plan["id"], selection(group, versions[low_source.name]))
    low_target.unlink()
    low_target.symlink_to(low_source)
    with pytest.raises(ConflictError, match="symlink"):
        service.execute(
            plan["id"], selection(group, versions[low_source.name]), False, "DELETE_SELECTED_DUPLICATES"
        )


def test_reclaimable_summary_only_counts_links_known_to_the_plan(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    low_source, _ = add_version(source, target, jobs, "示例电影.1080p.WEB-DL.mkv", b"low")
    add_version(source, target, jobs, "示例电影.2160p.BluRay.mkv", b"high-quality")
    plan = service.scan("quality_first")
    assert plan["summary"]["reclaimable_if_source_deleted"] == low_source.stat().st_size

    extra = source.parent / "outside-known-link.mkv"
    os.link(low_source, extra)
    plan = service.scan("quality_first")
    assert plan["summary"]["reclaimable_if_source_deleted"] == 0


def test_automatic_execution_skips_groups_with_an_unverified_retained_version(cleanup_env):
    service, _, _, _, _, _ = cleanup_env
    plan = {
        "groups": [
            {
                "id": "unsafe-retained",
                "recommended_keep_id": "unmanaged-high",
                "versions": [
                    {"id": "unmanaged-high", "managed": False, "safe": False},
                    {"id": "managed-low", "managed": True, "safe": True},
                ],
            },
            {
                "id": "verified-retained",
                "recommended_keep_id": "managed-high",
                "versions": [
                    {"id": "managed-high", "managed": True, "safe": True},
                    {"id": "managed-low-2", "managed": True, "safe": True},
                    {"id": "unmanaged-low", "managed": False, "safe": False},
                ],
            },
        ]
    }

    assert service.automatic_selections(plan) == [
        {"group_id": "verified-retained", "delete_version_ids": ["managed-low-2"]}
    ]


def test_user_can_delete_all_versions_in_a_group(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    v1_source, v1_target = add_version(source, target, jobs, "电影A.1080p.mkv", b"v1")
    v2_source, v2_target = add_version(source, target, jobs, "电影A.720p.mkv", b"v2")
    plan = service.scan("quality_first")
    group, versions = group_versions(plan)
    v1 = versions[v1_source.name]
    v2 = versions[v2_source.name]

    # Delete both versions (delete all in group)
    updated, results = service.execute(
        plan["id"],
        [{"group_id": group["id"], "delete_version_ids": [v1["id"], v2["id"]]}],
        False,
        "DELETE_SELECTED_DUPLICATES",
    )

    assert updated["status"] == "completed"
    assert len(results) == 2
    assert not v1_target.exists()
    assert not v2_target.exists()
    assert v1_source.exists()
    assert v2_source.exists()


def test_empty_delete_version_ids_is_skipped(cleanup_env):
    service, _, jobs, _, source, target = cleanup_env
    v1_source, v1_target = add_version(source, target, jobs, "电影B.1080p.mkv", b"v1")
    v2_source, v2_target = add_version(source, target, jobs, "电影B.720p.mkv", b"v2")
    plan = service.scan("quality_first")
    group, versions = group_versions(plan)
    v1 = versions[v1_source.name]

    # One group with empty delete_version_ids, one group with 1 deletion
    updated, results = service.execute(
        plan["id"],
        [
            {"group_id": group["id"], "delete_version_ids": [v1["id"]]},
        ],
        False,
        "DELETE_SELECTED_DUPLICATES",
    )

    assert updated["status"] == "completed"
    assert not v1_target.exists()
    assert v2_target.exists()
