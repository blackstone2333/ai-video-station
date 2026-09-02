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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .errors import AppError, ConflictError, NotFoundError, ValidationAppError
from .qbittorrent import normalized_task_progress
from .state import StateStore, StateStoreError
from .watchlist import utc_now_iso

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover
    FileSystemEvent = object  # type: ignore
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore


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
        # directory_sync_enabled switch controls filesystem watching / scheduling;
        # an explicit manual scan must remain usable when automatic scans are disabled.
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

    def _candidate_files_for_paths(
        self,
        changed_paths: Sequence[Path | str],
        source_root: Path,
        target_roots: Sequence[Path],
    ) -> Iterable[Path]:
        seen: Set[Path] = set()
        for item in changed_paths:
            path = Path(item).resolve(strict=False)
            if not self._inside(path, source_root):
                continue
            if path.is_dir():
                for sub_path in self._candidate_files(path, target_roots):
                    if sub_path not in seen:
                        seen.add(sub_path)
                        yield sub_path
            elif path.is_file():
                if path.is_symlink() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
                    continue
                if any(self._inside(path, root) for root in target_roots):
                    continue
                if self._contains_symlink(path, source_root):
                    continue
                if IGNORED_VIDEO_RE.search(path.stem):
                    continue
                if path not in seen:
                    seen.add(path)
                    yield path

    @staticmethod
    def _result(status: str, source: Path, target: Path, **extra: Any) -> Dict[str, Any]:
        return {"status": status, "source": str(source), "target": str(target), **extra}

    def _sync_rule(
        self,
        rule: Mapping[str, Any],
        active_roots: Sequence[Path],
        target_roots: Sequence[Path],
        changed_paths: Optional[Sequence[Path | str]] = None,
        is_auto: bool = False,
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
                candidates = (
                    self._candidate_files_for_paths(changed_paths, source_root, target_roots)
                    if changed_paths is not None
                    else self._candidate_files(source_root, target_roots)
                )
                for source in candidates:
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
        should_save = not is_auto or linked > 0 or conflicts > 0 or bool(errors)
        if should_save:
            saved = self.repository.save(item)
            logger.info(
                "directory_sync_completed",
                extra={
                    "run_id": item["id"],
                    "path_rule_id": rule.get("id"),
                    "scanned": scanned,
                    "linked": linked,
                    "conflicts": conflicts,
                    "waiting": waiting,
                    "is_auto": is_auto,
                },
            )
            return saved
        logger.debug(
            "directory_sync_skipped_empty",
            extra={
                "path_rule_id": rule.get("id"),
                "scanned": scanned,
                "already_linked": already_linked,
                "waiting": waiting,
            },
        )
        return item

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
            runs = [
                self._sync_rule(rule, active_roots, target_roots, changed_paths=None, is_auto=False)
                for rule in rules
            ]
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

    def scan_changed(
        self,
        path_rule_id: str,
        changed_paths: Sequence[Path | str],
        is_auto: bool = True,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"running": False, "enabled": False, "runs": [], "linked": 0, "waiting": 0}
        if not self._lock.acquire(blocking=False):
            return {"running": True, "enabled": True, "runs": [], "linked": 0, "waiting": 0}
        try:
            rules = [item for item in self.path_rules.list() if item.get("enabled")]
            rule = next((item for item in rules if item.get("id") == path_rule_id), None)
            if not rule:
                return {"running": False, "enabled": True, "runs": [], "linked": 0, "waiting": 0}
            target_roots = [
                self._container_path(r["target_path"]).resolve(strict=False) for r in rules
            ]
            active_roots = [*self._active_naming_roots(), *self._active_downloader_roots()]
            run = self._sync_rule(
                rule,
                active_roots,
                target_roots,
                changed_paths=changed_paths,
                is_auto=is_auto,
            )
            return {
                "running": False,
                "enabled": True,
                "runs": [run],
                "linked": int(run.get("linked") or 0),
                "waiting": int(run.get("waiting") or 0),
                "conflicts": int(run.get("conflicts") or 0),
            }
        finally:
            self._lock.release()


class DirectorySyncEventHandler(FileSystemEventHandler):
    """Event handler for a specific path rule's watched source directory."""

    def __init__(self, watcher: DirectorySyncWatcher, rule_id: str, source_root: Path) -> None:
        super().__init__()
        self.watcher = watcher
        self.rule_id = rule_id
        self.source_root = source_root

    def on_any_event(self, event: Any) -> None:
        event_type = getattr(event, "event_type", "")
        if event_type in ("deleted", "opened"):
            return
        is_directory = getattr(event, "is_directory", False)
        if is_directory and event_type == "modified":
            return

        raw_path = getattr(event, "dest_path", None) if event_type == "moved" else getattr(event, "src_path", "")
        if not raw_path:
            return
        path = Path(raw_path).resolve(strict=False)
        if path == self.source_root:
            return
        try:
            path.relative_to(self.source_root)
        except ValueError:
            return

        if any(part.startswith(".") and part != "." for part in path.parts):
            return

        if not is_directory:
            if path.suffix.casefold() not in VIDEO_EXTENSIONS:
                return
            if IGNORED_VIDEO_RE.search(path.stem):
                return

        self.watcher.queue_event(self.rule_id, path)


class DirectorySyncWatcher:
    """Watch download source directories using filesystem events and trigger targeted sync."""

    def __init__(
        self,
        service: DirectorySyncService,
        path_rules: Any,
        settings: Any,
        observer_cls: Any = None,
    ) -> None:
        self.service = service
        self.path_rules = path_rules
        self.settings = settings
        self.observer_cls = Observer if observer_cls is None else observer_cls
        self._observer: Any = None
        self._watches: Dict[str, tuple[Any, Path]] = {}
        self._timers: Dict[str, threading.Timer] = {}
        self._pending_paths: Dict[str, Set[Path]] = {}
        self._lock = threading.RLock()
        self._running = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            if not self.observer_cls:
                logger.warning("directory_sync_watcher_unavailable_no_watchdog")
                return
            self._observer = self.observer_cls()
            self._sync_watches_locked()
            try:
                self._observer.start()
                self._running = True
                logger.info("directory_sync_watcher_started", extra={"watches": len(self._watches)})
            except Exception as exc:
                logger.error("directory_sync_watcher_start_failed", extra={"error": str(exc)})
                self._running = False
                self._observer = None

    def stop(self) -> None:
        with self._lock:
            if not self._running and not self._observer:
                return
            self._running = False
            for timer in list(self._timers.values()):
                timer.cancel()
            self._timers.clear()
            self._pending_paths.clear()
            self._watches.clear()
            if self._observer:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=2.0)
                except Exception:
                    pass
                self._observer = None
            logger.info("directory_sync_watcher_stopped")

    def reload(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._sync_watches_locked()

    def _sync_watches_locked(self) -> None:
        if not self._observer:
            return
        auto_enabled = bool(getattr(self.settings, "directory_sync_enabled", True))
        if not auto_enabled or not self.service.enabled:
            for rule_id, (watch, _) in list(self._watches.items()):
                try:
                    self._observer.unschedule(watch)
                except Exception:
                    pass
            self._watches.clear()
            for timer in list(self._timers.values()):
                timer.cancel()
            self._timers.clear()
            self._pending_paths.clear()
            logger.info("directory_sync_watcher_disabled_or_cleared")
            return

        enabled_rules = [r for r in self.path_rules.list() if r.get("enabled")]
        active_rule_ids = set()

        for rule in enabled_rules:
            rule_id = str(rule.get("id"))
            active_rule_ids.add(rule_id)
            try:
                source_root = self.service._container_path(rule["source_path"]).resolve(strict=False)
            except Exception:
                continue

            if not source_root.exists() or not source_root.is_dir() or source_root.is_symlink():
                if rule_id in self._watches:
                    watch, _ = self._watches.pop(rule_id)
                    try:
                        self._observer.unschedule(watch)
                    except Exception:
                        pass
                continue

            current_watch = self._watches.get(rule_id)
            if current_watch:
                watch, watched_path = current_watch
                if watched_path == source_root:
                    continue
                try:
                    self._observer.unschedule(watch)
                except Exception:
                    pass
                self._watches.pop(rule_id, None)

            handler = DirectorySyncEventHandler(self, rule_id, source_root)
            try:
                watch = self._observer.schedule(handler, str(source_root), recursive=True)
                self._watches[rule_id] = (watch, source_root)
                logger.info(
                    "directory_sync_watch_registered",
                    extra={"rule_id": rule_id, "path": str(source_root)},
                )
            except Exception as exc:
                logger.warning(
                    "directory_sync_watch_failed",
                    extra={"rule_id": rule_id, "path": str(source_root), "error": str(exc)},
                )

        for rule_id in list(self._watches):
            if rule_id not in active_rule_ids:
                watch, _ = self._watches.pop(rule_id)
                try:
                    self._observer.unschedule(watch)
                except Exception:
                    pass
                if rule_id in self._timers:
                    self._timers.pop(rule_id).cancel()
                self._pending_paths.pop(rule_id, None)

    def queue_event(self, rule_id: str, path: Path) -> None:
        with self._lock:
            if not self._running or not getattr(self.settings, "directory_sync_enabled", True):
                return
            self._pending_paths.setdefault(rule_id, set()).add(path)
            if rule_id in self._timers:
                self._timers[rule_id].cancel()
            settle = int(getattr(self.settings, "directory_sync_settle_seconds", 120))
            delay = max(0.05, float(settle))
            timer = threading.Timer(delay, self._trigger_sync, args=(rule_id,))
            timer.daemon = True
            self._timers[rule_id] = timer
            timer.start()

    def _trigger_sync(self, rule_id: str) -> None:
        with self._lock:
            paths = self._pending_paths.pop(rule_id, set())
            self._timers.pop(rule_id, None)
        if not paths:
            return
        if not getattr(self.settings, "directory_sync_enabled", True) or not self.service.enabled:
            return
        try:
            self.service.scan_changed(path_rule_id=rule_id, changed_paths=list(paths), is_auto=True)
        except Exception as exc:  # pragma: no cover
            logger.error("directory_sync_trigger_error", extra={"rule_id": rule_id, "error": str(exc)})
