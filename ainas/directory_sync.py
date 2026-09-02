"""Independent download-directory to media-library hardlink synchronization."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .errors import AppError, ConflictError, NotFoundError, ValidationAppError
from .qbittorrent import normalized_task_progress
from .state import StateStore, StateStoreError
from .watchlist import utc_now_iso


logger = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".wmv", ".flv", ".webm"}
IGNORED_VIDEO_RE = re.compile(r"(?i)(?:^|[ ._\-])(?:sample|trailer|preview)(?:$|[ ._\-])|样片|预告")
ACTIVE_JOB_STATES = {
    "submitting",
    "awaiting_binding",
    "pending",
    "waiting_metadata",
    "waiting_selection",
    "waiting_download",
    "retrying",
}


class DirectorySyncRepository:
    """Persist bounded synchronization runs in the shared state database."""

    def __init__(self, state: StateStore, limit: int = 200) -> None:
        self.state = state
        self.limit = limit
        self._lock = threading.RLock()
        try:
            state.ensure_records("directory_sync_runs")
        except StateStoreError as exc:
            raise AppError(
                "Directory Sync Repository Error",
                str(exc),
                "directory-sync-repository-error",
                500,
            ) from exc

    def list(self) -> List[Dict[str, Any]]:
        return self.state.list_records("directory_sync_runs")

    def get(self, run_id: str) -> Dict[str, Any]:
        for item in self.list():
            if item.get("id") == run_id:
                return item
        raise NotFoundError("directory sync run", run_id)

    def save(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        item = deepcopy(dict(value))
        with self._lock:
            values = [current for current in self.list() if current.get("id") != item.get("id")]
            values.append(item)
            values.sort(key=lambda current: str(current.get("completed_at") or ""), reverse=True)
            try:
                self.state.replace_records("directory_sync_runs", values[: self.limit])
            except StateStoreError as exc:
                raise AppError(
                    "Directory Sync Repository Error",
                    str(exc),
                    "directory-sync-repository-error",
                    500,
                ) from exc
        return deepcopy(item)


class DirectorySyncService:
    """Hardlink stable video files from every enabled source-to-target rule.

    This service does not depend on AVS having created the download.  Active
    downloader tasks and active AVS naming roots are excluded so a partially
    written file is never treated as ready merely because it already has its
    final extension.
    """

    def __init__(
        self,
        settings: Any,
        repository: DirectorySyncRepository,
        path_rules: Any,
        naming_jobs: Any = None,
        downloader: Any = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.path_rules = path_rules
        self.naming_jobs = naming_jobs
        self.downloader = downloader
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        # This reflects whether hardlinking is available at all.  The runtime
        # directory_sync_enabled switch controls scheduling only; an explicit
        # manual scan must remain usable when automatic scans are disabled.
        return bool(self.settings.medialib_hardlink_enabled)

    def _container_path(self, value: str | Path) -> Path:
        path = Path(value)
        mount = Path(self.settings.medialib_mount_path)
        if path == mount or mount in path.parents:
            return path
        try:
            return mount / path.relative_to(self.settings.medialib_base_path)
        except ValueError as exc:
            raise ValidationAppError("目录同步路径超出 MEDIALIB_MOUNT_PATH") from exc

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _host_path_for_downloader(self, value: str | Path) -> Path:
        translator = getattr(self.path_rules, "host_path_for_downloader", None)
        return Path(translator(value)) if callable(translator) else Path(value)

    def _download_root(self, value: str | Path) -> Path:
        return self._container_path(self._host_path_for_downloader(value)).resolve(strict=False)

    def _active_naming_roots(self) -> List[Path]:
        roots: List[Path] = []
        for job in self.naming_jobs.list() if self.naming_jobs else []:
            if job.get("status") not in ACTIVE_JOB_STATES:
                continue
            submission = job.get("submission") or {}
            candidates: List[Any] = []
            task_hash = str(job.get("torrent_hash") or "")
            if task_hash and not task_hash.startswith("pending:") and self.downloader:
                try:
                    task = self.downloader.torrent_info(task_hash)
                except AppError:
                    task = None
                if task and task.get("content_path"):
                    candidates.append(task["content_path"])
                elif task and task.get("save_path") and task.get("name"):
                    candidates.append(Path(str(task["save_path"])) / str(task["name"]))
            # An unbound submission has no safe torrent-specific directory yet.
            # Temporarily exclude its save path rather than risk importing a
            # partially-written file under an unknown task name.
            if not candidates and submission.get("save_path"):
                candidates.append(submission["save_path"])
            for candidate in candidates:
                try:
                    root = self._download_root(candidate)
                except (OSError, ValidationAppError):
                    continue
                if root not in roots:
                    roots.append(root)
        return roots

    @staticmethod
    def _task_complete(task: Mapping[str, Any]) -> bool:
        return bool(task.get("completed") or normalized_task_progress(dict(task)) >= 0.999999)

    def _active_downloader_roots(self) -> List[Path]:
        if not self.downloader or not getattr(self.downloader, "configured", False):
            return []
        roots: List[Path] = []
        tasks = self.downloader.tasks()
        for task in tasks:
            if self._task_complete(task):
                continue
            task_hash = str(task.get("hash") or "")
            info = self.downloader.torrent_info(task_hash) if task_hash else None
            candidates: List[Any] = []
            if info and info.get("content_path"):
                candidates.append(info["content_path"])
            elif task.get("save_path") and task.get("name"):
                candidates.append(Path(str(task["save_path"])) / str(task["name"]))
            elif task.get("save_path"):
                candidates.append(task["save_path"])
            for candidate in candidates:
                try:
                    root = self._download_root(candidate)
                except (OSError, ValidationAppError):
                    continue
                if root not in roots:
                    roots.append(root)
        return roots

    @staticmethod
    def _contains_symlink(path: Path, root: Path) -> bool:
        current = path
        while True:
            if current.is_symlink():
                return True
            if current == root:
                return False
            if current.parent == current:
                return True
            current = current.parent

    def _candidate_files(self, source_root: Path, target_roots: Sequence[Path]) -> Iterable[Path]:
        for path in source_root.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            if any(self._inside(path, root) for root in target_roots):
                continue
            if self._contains_symlink(path, source_root):
                continue
            if IGNORED_VIDEO_RE.search(path.stem):
                continue
            yield path

    @staticmethod
    def _result(status: str, source: Path, target: Path, **extra: Any) -> Dict[str, Any]:
        return {"status": status, "source": str(source), "target": str(target), **extra}

    def _sync_rule(
        self,
        rule: Mapping[str, Any],
        active_roots: Sequence[Path],
        target_roots: Sequence[Path],
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        source_root = self._container_path(rule["source_path"]).resolve(strict=False)
        target_root = self._container_path(rule["target_path"]).resolve(strict=False)
        results: List[Dict[str, Any]] = []
        scanned = linked = already_linked = conflicts = waiting = 0
        errors: List[str] = []

        if not source_root.exists() or source_root.is_symlink():
            errors.append(f"下载目录不存在或不可安全访问：{source_root}")
        else:
            target_root.mkdir(parents=True, exist_ok=True)
            if source_root.stat().st_dev != target_root.stat().st_dev:
                errors.append(f"源目录与媒体库不在同一文件系统：{source_root} -> {target_root}")
            else:
                settle_seconds = int(getattr(self.settings, "directory_sync_settle_seconds", 120))
                current_time = time.time()
                for source in self._candidate_files(source_root, target_roots):
                    scanned += 1
                    if any(self._inside(source.resolve(strict=False), root) for root in active_roots):
                        waiting += 1
                        continue
                    try:
                        stat = source.stat()
                        if settle_seconds and current_time - stat.st_mtime < settle_seconds:
                            waiting += 1
                            continue
                        relative = source.relative_to(source_root)
                        target = target_root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.parent.stat().st_dev != stat.st_dev:
                            raise ConflictError("目录同步目标与源文件不在同一文件系统")
                        if os.path.lexists(target):
                            if target.is_file() and not target.is_symlink() and os.path.samefile(source, target):
                                already_linked += 1
                                results.append(self._result("already-linked", source, target, size=stat.st_size))
                            else:
                                conflicts += 1
                                results.append(self._result("conflict", source, target, size=stat.st_size))
                            continue
                        os.link(source, target)
                        linked += 1
                        results.append(self._result("linked", source, target, size=stat.st_size))
                    except (OSError, ConflictError) as exc:
                        errors.append(f"{source}: {exc}")
                        results.append(self._result("failed", source, target_root, error=str(exc)))

        if errors or conflicts:
            status = "partial" if linked or already_linked else "conflict"
        elif waiting:
            status = "waiting_download"
        else:
            status = "done"
        item = {
            "id": uuid.uuid4().hex,
            "source_kind": "directory_sync",
            "name": f"目录同步 · {rule.get('name') or rule.get('id')}",
            "type": rule.get("media_type"),
            "path_rule_id": rule.get("id"),
            "source": str(source_root),
            "target": str(target_root),
            "status": status,
            "scanned": scanned,
            "linked": linked,
            "skipped": already_linked + conflicts + waiting,
            "already_linked": already_linked,
            "conflicts": conflicts,
            "waiting": waiting,
            "files": results[:1000],
            "error": "; ".join(errors[:10]) or None,
            "attempts": 0,
            "last_check": now,
            "completed_at": now,
        }
        logger.info(
            "directory_sync_completed",
            extra={
                "run_id": item["id"],
                "path_rule_id": rule.get("id"),
                "scanned": scanned,
                "linked": linked,
                "conflicts": conflicts,
                "waiting": waiting,
            },
        )
        return self.repository.save(item)

    def scan(self, path_rule_id: Optional[str] = None) -> Dict[str, Any]:
        if not self.enabled:
            return {"running": False, "enabled": False, "runs": [], "linked": 0, "waiting": 0}
        if not self._lock.acquire(blocking=False):
            return {"running": True, "enabled": True, "runs": [], "linked": 0, "waiting": 0}
        try:
            rules = [item for item in self.path_rules.list() if item.get("enabled")]
            if path_rule_id:
                rules = [item for item in rules if item.get("id") == path_rule_id]
                if not rules:
                    raise NotFoundError("enabled path rule", path_rule_id)
            target_roots = [
                self._container_path(rule["target_path"]).resolve(strict=False) for rule in rules
            ]
            active_roots = [*self._active_naming_roots(), *self._active_downloader_roots()]
            runs = [self._sync_rule(rule, active_roots, target_roots) for rule in rules]
            return {
                "running": False,
                "enabled": True,
                "runs": runs,
                "linked": sum(int(item.get("linked") or 0) for item in runs),
                "waiting": sum(int(item.get("waiting") or 0) for item in runs),
                "conflicts": sum(int(item.get("conflicts") or 0) for item in runs),
            }
        finally:
            self._lock.release()
