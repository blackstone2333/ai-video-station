"""Conservative, persistent duplicate scanning and cleanup plans."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .errors import ConflictError, NotFoundError, ValidationAppError
from .qbittorrent import normalized_task_progress
from .quality import build_release, canonical_media_name, detect_episode, is_cam_release
from .resource_preferences import profile_for, release_score
from .state import StateStore
from .watchlist import utc_now_iso


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".wmv"}
SINGLE_EPISODE = re.compile(r"^S\d{1,3}E\d{1,4}$", re.IGNORECASE)


class CleanupPlanRepository:
    """Store cleanup plans in the shared SQLite state database."""

    def __init__(self, state: StateStore) -> None:
        self.state = state
        self._lock = threading.RLock()
        state.ensure_records("cleanup_plans")

    def list(self) -> List[Dict[str, Any]]:
        return self.state.list_records("cleanup_plans")

    def get(self, plan_id: str) -> Dict[str, Any]:
        for item in self.list():
            if item["id"] == plan_id:
                return item
        raise NotFoundError("cleanup plan", plan_id)

    def save(self, item: Mapping[str, Any]) -> Dict[str, Any]:
        value = deepcopy(dict(item))
        with self._lock:
            items = self.list()
            index = next((position for position, current in enumerate(items) if current["id"] == value["id"]), None)
            if index is None:
                items.append(value)
            else:
                items[index] = value
            self.state.replace_records("cleanup_plans", items)
        return deepcopy(value)


class CleanupService:
    """Find physical duplicate versions and execute explicitly confirmed plans."""

    def __init__(self, settings: Any, repository: CleanupPlanRepository, path_rules: Any, naming_jobs: Any, downloader: Any, operation_lock: threading.RLock | None = None) -> None:
        self.settings = settings
        self.repository = repository
        self.path_rules = path_rules
        self.naming_jobs = naming_jobs
        self.downloader = downloader
        # Share this lock with directory synchronization in the application so
        # cleanup cannot race a concurrent hardlink mutation. Unit callers may
        # omit it and receive an isolated lock.
        self._lock = operation_lock or threading.RLock()

    def _container_path(self, value: str | Path) -> Path:
        path = Path(value)
        mount = Path(self.settings.medialib_mount_path)
        if path == mount or mount in path.parents:
            return path
        try:
            return mount / path.relative_to(self.settings.medialib_base_path)
        except ValueError as exc:
            raise ValidationAppError("cleanup path is outside MEDIALIB_MOUNT_PATH") from exc

    def _roots(self, rules: Sequence[Mapping[str, Any]] | None = None) -> List[Path]:
        values: List[Path] = []
        candidates = list(rules) if rules is not None else [item for item in self.path_rules.list() if item.get("enabled")]
        for rule in candidates:
            for field in ("source_path", "target_path"):
                path = self._container_path(rule[field])
                if path not in values:
                    values.append(path)
        return values

    def _safe_path(self, value: str | Path, roots: Sequence[Path]) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ConflictError("cleanup path must be absolute")
        matching = [root for root in roots if path == root or root in path.parents]
        if not matching:
            raise ConflictError("cleanup path escapes enabled roots")
        root = max(matching, key=lambda item: len(item.parts))
        current = path
        while True:
            if current.is_symlink():
                raise ConflictError("cleanup path contains symlink")
            if current == root:
                break
            current = current.parent
        try:
            if not path.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
                raise ConflictError("cleanup path escapes enabled roots")
        except OSError as exc:
            raise ConflictError("cleanup path cannot be inspected") from exc
        return path

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _job_for_path(self, path: Path) -> Dict[str, Any] | None:
        expected = path.resolve(strict=False)
        for job in self.naming_jobs.list() if self.naming_jobs else []:
            result = job.get("hardlink_result") or {}
            candidates: List[Any] = [result.get("source"), result.get("target")]
            for item in result.get("files") or []:
                if isinstance(item, Mapping):
                    candidates.extend((item.get("source"), item.get("target")))
            for candidate in candidates:
                if not candidate:
                    continue
                try:
                    if self._container_path(candidate).resolve(strict=False) == expected:
                        return job
                except (OSError, ValidationAppError):
                    continue
        return None

    @staticmethod
    def _single_plan_episode(plan: Mapping[str, Any]) -> str | None:
        value = str(plan.get("episode") or "").strip().upper()
        return value if SINGLE_EPISODE.fullmatch(value) else None

    def _identity(self, media_type: str, path: Path, job: Mapping[str, Any] | None) -> tuple[str, bool]:
        plan = (job or {}).get("plan") or {}
        name = str(plan.get("media_name") or plan.get("root_name") or canonical_media_name(path.stem)).casefold()
        if media_type in {"tv", "anime"}:
            # A pack-level plan may say S01E01-E33. Each physical file must use its
            # own normalized filename first or unrelated episodes become "versions".
            episode = detect_episode(path.name, allow_numeric_prefix=True) or self._single_plan_episode(plan)
            return f"{name}:{episode or 'unknown'}", bool(episode)
        return f"{name}:{plan.get('year') or ''}", True

    @staticmethod
    def _quality_score(version: Mapping[str, Any], policy: str) -> tuple[Any, ...]:
        release = version["_release"]
        if is_cam_release(Path(version["_path"]).name):
            return (-999, -999, -999, -999)
        profile = profile_for("compact" if policy == "space_first" else "collection")
        return release_score(release, profile)

    @staticmethod
    def _known_paths(version: Mapping[str, Any]) -> set[str]:
        return {str(value) for value in [*(version.get("source_paths") or []), *(version.get("target_paths") or [])]}

    @classmethod
    def _reclaimable(cls, version: Mapping[str, Any]) -> int:
        known_links = len(cls._known_paths(version))
        if not version.get("source_paths") or int(version.get("link_count") or 0) > known_links:
            return 0
        return int(version.get("size") or 0)

    @staticmethod
    def automatic_selections(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Select only groups whose retained recommendation is also AVS-verified."""
        selections: List[Dict[str, Any]] = []
        for group in plan.get("groups") or []:
            versions = list(group.get("versions") or [])
            keep_id = group.get("recommended_keep_id")
            retained = next((item for item in versions if item.get("id") == keep_id), None)
            if not retained or not retained.get("managed") or not retained.get("safe"):
                continue
            delete_ids = [
                str(item["id"])
                for item in versions
                if item.get("id") != keep_id and item.get("managed") and item.get("safe")
            ]
            if delete_ids:
                selections.append({"group_id": str(group["id"]), "delete_version_ids": delete_ids})
        return selections

    def scan(self, policy: str = "quality_first", media_type: str | None = None) -> Dict[str, Any]:
        with self._lock:
            return self._scan(policy, media_type)

    def _scan(self, policy: str, media_type: str | None) -> Dict[str, Any]:
        if policy not in {"quality_first", "space_first"}:
            raise ValidationAppError("unsupported cleanup policy")
        rules = [
            rule
            for rule in self.path_rules.list()
            if rule.get("enabled") and (not media_type or rule["media_type"] == media_type)
        ]
        physical: Dict[tuple[str, int, int], Dict[str, Any]] = {}
        for rule in rules:
            for role in ("source_path", "target_path"):
                root = self._container_path(rule[role])
                if not root.exists() or root.is_symlink():
                    continue
                for path in root.rglob("*"):
                    if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
                        continue
                    stat = path.stat()
                    key = (rule["media_type"], stat.st_dev, stat.st_ino)
                    item = physical.setdefault(
                        key,
                        {"path": path, "stat": stat, "rule": rule, "source_paths": set(), "target_paths": set()},
                    )
                    item[f"{role}s"].add(path)

        versions: List[Dict[str, Any]] = []
        for (kind, device, inode), item in physical.items():
            path = item["path"]
            stat = item["stat"]
            rule = item["rule"]
            job = self._job_for_path(path)
            release = build_release(path.stem, path.name, "", "", kind)
            identity, reliable = self._identity(kind, path, job)
            version = {
                "id": uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}:{device}:{inode}").hex,
                "media_type": kind,
                "path_rule_id": rule["id"],
                "source_paths": sorted(str(value) for value in item["source_paths"]),
                "target_paths": sorted(str(value) for value in item["target_paths"]),
                "size": stat.st_size,
                "device": device,
                "inode": inode,
                "link_count": stat.st_nlink,
                "mtime_ns": stat.st_mtime_ns,
                "resolution": getattr(release, "resolution", None),
                "source": getattr(release, "source", None),
                "hdr": getattr(release, "hdr", None),
                "audio": getattr(release, "language", None),
                "codec": getattr(release, "encoding", None),
                "torrent_hash": (job or {}).get("torrent_hash"),
                # A file is managed only when AVS can tie it to a naming job.
                # External downloads (Ani-RSS/manual tools) remain visible for
                # duplicate inspection but are never eligible for source
                # deletion without an explicit ownership record.
                "managed": bool(job),
                "safe": bool(reliable and job),
                "safety_reason": None if (reliable and job) else ("unmanaged-source" if not job else "episode-unresolved"),
                "_identity": identity,
                "_release": release,
                "_path": path,
            }
            versions.append(version)

        grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for version in versions:
            identity = version["_identity"]
            if version["media_type"] == "custom":
                identity = self._hash(version["_path"])
            grouped.setdefault((version["media_type"], identity), []).append(version)

        groups: List[Dict[str, Any]] = []
        for (kind, identity), candidates in grouped.items():
            if len(candidates) < 2:
                continue
            keep = max(candidates, key=lambda value: self._quality_score(value, policy))
            for value in candidates:
                value.pop("_identity", None)
                value.pop("_release", None)
                value.pop("_path", None)
            groups.append(
                {
                    "id": uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}:{identity}").hex,
                    "media_type": kind,
                    "identity": identity,
                    "recommended_keep_id": keep["id"],
                    "versions": candidates,
                }
            )

        default_deletes = [
            version
            for group in groups
            for version in group["versions"]
            if version["id"] != group["recommended_keep_id"]
        ]
        now = utc_now_iso()
        reclaimable = sum(self._reclaimable(value) for value in default_deletes)
        plan = {
            "id": uuid.uuid4().hex,
            "status": "ready",
            "policy": policy,
            "created_at": now,
            "updated_at": now,
            "groups": groups,
            "summary": {
                "groups": len(groups),
                "logical_duplicate_size": sum(int(value["size"]) for value in default_deletes),
                "reclaimable_if_source_deleted": reclaimable,
                "estimated_reclaimable": reclaimable,
            },
            "error": None,
            "results": [],
        }
        return self.repository.save(plan)

    @staticmethod
    def _metadata(version: Mapping[str, Any]) -> tuple[int, int, int, int]:
        return (
            int(version["device"]),
            int(version["inode"]),
            int(version["size"]),
            int(version["mtime_ns"]),
        )

    def _validate_version(self, version: Mapping[str, Any], roots: Sequence[Path], *, readable: bool) -> None:
        paths = sorted(self._known_paths(version))
        if not paths:
            raise ConflictError("cleanup plan has no managed file path")
        for raw in paths:
            path = self._safe_path(raw, roots)
            if not path.exists() or not path.is_file():
                raise ConflictError("cleanup plan is stale: file missing")
            stat = path.stat()
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != self._metadata(version):
                raise ConflictError("cleanup plan is stale")
        if readable:
            try:
                with Path(paths[0]).open("rb") as stream:
                    stream.read(0)
            except OSError as exc:
                raise ConflictError("retained cleanup version is not readable") from exc

    @staticmethod
    def _task_complete(task: Mapping[str, Any]) -> bool:
        state = str(task.get("state") or "").casefold()
        if any(marker in state for marker in ("checking", "missing", "error")):
            return False
        return normalized_task_progress(task) >= 0.999999

    def _validate_whole_torrents(self, chosen: Sequence[Mapping[str, Any]]) -> set[str]:
        selected_by_torrent: Dict[str, List[str]] = {}
        for version in chosen:
            torrent_hash = version.get("torrent_hash")
            if torrent_hash:
                selected_by_torrent.setdefault(str(torrent_hash), []).extend(version.get("source_paths") or [])

        managed: set[str] = set()
        for torrent_hash, selected_paths in selected_by_torrent.items():
            task = self.downloader.torrent_info(torrent_hash)
            if not task:
                # The downloader no longer owns the data. Execution may use the
                # same contained-path checks for a direct source unlink.
                continue
            if not self._task_complete(task):
                raise ConflictError("downloader task is not complete")
            files = self.downloader.files(torrent_hash) or []
            torrent_videos = Counter(
                PurePosixPath(str(item.get("name") or "")).name
                for item in files
                if PurePosixPath(str(item.get("name") or "")).suffix.casefold() in VIDEO_EXTENSIONS
            )
            selected_videos = Counter(Path(path).name for path in selected_paths)
            if not torrent_videos or torrent_videos != selected_videos:
                raise ConflictError("cannot delete source for partial multi-file torrent")
            managed.add(torrent_hash)
        return managed

    def _preflight(
        self,
        plan: Mapping[str, Any],
        selections: Sequence[Mapping[str, Any]],
        delete_source: bool,
    ) -> tuple[List[Dict[str, Any]], List[Path], set[str]]:
        if not selections:
            raise ValidationAppError("at least one cleanup selection is required")
        groups = {item["id"]: item for item in plan["groups"]}
        previous = {item.get("version_id"): item for item in plan.get("results", [])}
        completed = {version_id for version_id, item in previous.items() if item.get("status") == "completed"}
        chosen: List[Dict[str, Any]] = []
        retained: List[Dict[str, Any]] = []
        selected_groups: set[str] = set()

        for selection in selections:
            group_id = str(selection.get("group_id") or "")
            if not group_id or group_id in selected_groups:
                raise ValidationAppError("cleanup groups must be unique and known")
            selected_groups.add(group_id)
            group = groups.get(group_id)
            if not group:
                raise ValidationAppError("unknown cleanup group")
            versions = {item["id"]: item for item in group["versions"]}
            version_ids = list(selection.get("delete_version_ids") or [])
            ids = set(version_ids)
            if not ids:
                continue
            if len(ids) != len(version_ids) or not ids <= set(versions):
                raise ValidationAppError("unknown or duplicate cleanup version")
            active_ids = set(versions) - completed
            active_delete_ids = ids - completed
            retained_ids = active_ids - active_delete_ids
            for version_id in ids:
                value = deepcopy(versions[version_id])
                value["_already_completed"] = version_id in completed
                chosen.append(value)
            retained.extend(deepcopy(versions[version_id]) for version_id in retained_ids)

        if not chosen:
            raise ValidationAppError("at least one cleanup selection is required")

        roots = self._roots()
        for version in chosen:
            if version.get("_already_completed"):
                continue
            self._validate_version(version, roots, readable=False)
        for version in retained:
            self._validate_version(version, roots, readable=True)

        active_chosen = [value for value in chosen if not value.get("_already_completed")]
        if delete_source:
            unmanaged = [value for value in active_chosen if not value.get("managed")]
            if unmanaged:
                raise ConflictError("cannot delete source files that are not owned by an AVS naming job")
        managed_torrents = self._validate_whole_torrents(active_chosen) if delete_source else set()
        return chosen, roots, managed_torrents

    @staticmethod
    def _remaining_link(version: Mapping[str, Any], excluded: Iterable[str] = ()) -> Path | None:
        excluded_values = set(excluded)
        for raw in [*(version.get("source_paths") or []), *(version.get("target_paths") or [])]:
            if raw not in excluded_values and Path(raw).exists():
                return Path(raw)
        return None

    def _restore_targets(self, version: Mapping[str, Any], targets: Sequence[str]) -> None:
        source = self._remaining_link(version, targets)
        if not source:
            return
        for raw in targets:
            target = Path(raw)
            try:
                if not target.exists():
                    os.link(source, target)
            except OSError:
                continue

    def execute(
        self,
        plan_id: str,
        selections: Sequence[Mapping[str, Any]],
        delete_source: bool = False,
        confirmation: str = "",
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        if confirmation != "DELETE_SELECTED_DUPLICATES":
            raise ValidationAppError("cleanup confirmation is required")
        with self._lock:
            plan = self.repository.get(plan_id)
            chosen, roots, managed_torrents = self._preflight(plan, selections, delete_source)
            results: Dict[str, Dict[str, Any]] = {
                str(item.get("version_id")): dict(item) for item in plan.get("results", []) if item.get("version_id")
            }
            current: List[Dict[str, Any]] = []
            removed_targets: Dict[str, List[str]] = {}
            active: List[Dict[str, Any]] = []

            for version in chosen:
                version_id = version["id"]
                if version.get("_already_completed"):
                    item = {"version_id": version_id, "status": "completed", "idempotent": True}
                    current.append(item)
                    results[version_id] = item
                    continue
                removed: List[str] = []
                try:
                    for raw in version.get("target_paths") or []:
                        path = self._safe_path(raw, roots)
                        os.unlink(path)
                        removed.append(raw)
                    removed_targets[version_id] = removed
                    active.append(version)
                except Exception as exc:
                    self._restore_targets(version, removed)
                    item = {"version_id": version_id, "status": "failed", "error": str(exc)}
                    current.append(item)
                    results[version_id] = item

            if not delete_source:
                for version in active:
                    item = {
                        "version_id": version["id"],
                        "status": "completed",
                        "targets_deleted": removed_targets.get(version["id"], []),
                    }
                    current.append(item)
                    results[version["id"]] = item
            else:
                by_torrent: Dict[str, List[Dict[str, Any]]] = {}
                direct: List[Dict[str, Any]] = []
                for version in active:
                    torrent_hash = str(version.get("torrent_hash") or "")
                    if torrent_hash and torrent_hash in managed_torrents:
                        by_torrent.setdefault(torrent_hash, []).append(version)
                    else:
                        direct.append(version)

                for torrent_hash, versions in by_torrent.items():
                    try:
                        self.downloader.pause(torrent_hash)
                        self.downloader.delete(torrent_hash, delete_files=True)
                        for version in versions:
                            item = {
                                "version_id": version["id"],
                                "status": "completed",
                                "targets_deleted": removed_targets.get(version["id"], []),
                                "source_deleted_by": "downloader",
                            }
                            current.append(item)
                            results[version["id"]] = item
                    except Exception as exc:
                        try:
                            if self.downloader.torrent_info(torrent_hash):
                                self.downloader.resume(torrent_hash)
                        except Exception:
                            pass
                        for version in versions:
                            self._restore_targets(version, removed_targets.get(version["id"], []))
                            item = {"version_id": version["id"], "status": "failed", "error": str(exc)}
                            current.append(item)
                            results[version["id"]] = item

                for version in direct:
                    try:
                        for raw in version.get("source_paths") or []:
                            path = self._safe_path(raw, roots)
                            os.unlink(path)
                        item = {
                            "version_id": version["id"],
                            "status": "completed",
                            "targets_deleted": removed_targets.get(version["id"], []),
                            "source_deleted_by": "avs",
                        }
                        current.append(item)
                        results[version["id"]] = item
                    except Exception as exc:
                        self._restore_targets(version, removed_targets.get(version["id"], []))
                        item = {"version_id": version["id"], "status": "failed", "error": str(exc)}
                        current.append(item)
                        results[version["id"]] = item

            plan["results"] = list(results.values())
            plan["selections"] = deepcopy(list(selections))
            plan["delete_source"] = bool(delete_source)
            plan["status"] = "partial" if any(item.get("status") == "failed" for item in plan["results"]) else "completed"
            failures = [str(item.get("error")) for item in plan["results"] if item.get("status") == "failed"]
            plan["error"] = "; ".join(failures) if failures else None
            plan["updated_at"] = utc_now_iso()
            return self.repository.save(plan), current

    def retry(self, plan_id: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        plan = self.repository.get(plan_id)
        failed = {item["version_id"] for item in plan.get("results", []) if item.get("status") == "failed"}
        selections: List[Dict[str, Any]] = []
        for selection in plan.get("selections", []):
            version_ids = [value for value in selection["delete_version_ids"] if value in failed]
            if version_ids:
                selections.append({"group_id": selection["group_id"], "delete_version_ids": version_ids})
        if not selections:
            raise ConflictError("cleanup plan has no failed items to retry")
        return self.execute(
            plan_id,
            selections,
            bool(plan.get("delete_source")),
            "DELETE_SELECTED_DUPLICATES",
        )
