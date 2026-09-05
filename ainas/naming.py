from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import Settings
from .errors import AppError, ConflictError, NotFoundError
from .media_policies import policy_for
from .qbittorrent import QBittorrentClient, normalized_task_progress, torrent_hash
from .quality import Release, canonical_media_name, detect_episode, detect_season
from .resource_preferences import select_episode_files
from .watchlist import utc_now_iso
from .state import StateStore, StateStoreError


logger = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".wmv", ".flv", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
INVALID_FILENAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
PADDING_PREFIXES = ("_____padding_file_",)


def safe_name(value: str, max_length: int = 180) -> str:
    """Return a portable filename component bounded in UTF-8 bytes.

    Most NAS filesystems enforce a 255-byte component limit, not a character
    limit.  Preserve an extension because it is material to media discovery.
    """
    normalized = unicodedata.normalize("NFC", value)
    normalized = INVALID_FILENAME_RE.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = normalized or "未命名媒体"
    suffix = Path(normalized).suffix
    stem = normalized[: -len(suffix)] if suffix else normalized
    limit = max(1, int(max_length))
    def trim(value: str, budget: int) -> str:
        result = ""
        for char in value:
            if len((result + char).encode("utf-8")) > budget:
                break
            result += char
        return result.rstrip(" .")
    # A pathological extension is less useful than a valid component.
    if len(suffix.encode("utf-8")) >= limit:
        return trim(suffix, limit) or "_"
    return (trim(stem, limit - len(suffix.encode("utf-8"))) + suffix).rstrip(" .") or "未命名媒体"


def _episode_with_default_season(value: Optional[str], season: Optional[int]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("S"):
        return value
    if value.startswith("E"):
        return f"S{(season or 1):02d}{value}"
    return None


@dataclass(frozen=True)
class NamingPlan:
    media_type: str
    media_name: str
    root_name: str
    year: Optional[int]
    season: Optional[int]
    episode: Optional[str]
    link_name: Optional[str]
    original_title: Optional[str] = None
    part: Optional[str] = None
    edition: Optional[str] = None
    video_format: Optional[str] = None
    episode_title: Optional[str] = None
    rename_enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EmbyNamingPlanner:
    @staticmethod
    def from_release(release: Release) -> NamingPlan:
        policy = policy_for(release.media_type)
        media_name = safe_name(release.media_name or canonical_media_name(release.title))
        year = release.year
        if policy.naming_layout == "preserve":
            link_name = release.link_name or media_name
            root_name = safe_name(PurePosixPath(link_name.replace("\\", "/")).name)
            if root_name.casefold().endswith(".torrent"):
                root_name = safe_name(root_name[:-8])
        else:
            root_name = f"{media_name} ({year})" if year else media_name
        episode = _episode_with_default_season(release.episode, release.season)
        return NamingPlan(
            media_type=release.media_type,
            media_name=media_name,
            root_name=safe_name(root_name),
            year=year,
            season=release.season,
            episode=episode,
            link_name=release.link_name,
            original_title=safe_name(release.original_title) if release.original_title else None,
            part=safe_name(release.part) if release.part else None,
            edition=safe_name(release.edition) if release.edition else None,
            video_format=safe_name(release.video_format) if release.video_format else None,
            episode_title=safe_name(release.episode_title) if release.episode_title else None,
            rename_enabled=policy.rename_enabled,
        )

    @staticmethod
    def _new_path(old_path: str, basename: str) -> str:
        parent = PurePosixPath(old_path).parent
        return str(parent / basename) if str(parent) != "." else basename

    @staticmethod
    def _movie_path(
        old_path: str,
        basename: str,
        root_names: Tuple[str, ...] = (),
    ) -> str:
        """Keep a torrent's root while dropping nested release/advertising folders.

        qBittorrent reports a multi-file torrent path relative to the category
        save directory (or, depending on the qBittorrent version, relative to
        the torrent root).  A component matching the known title is the
        former case and is kept for the later torrent rename; an unknown first
        component is treated as the latter case and removed along with any
        deeper 6v advertising folders.  Moving the file to the torrent root
        makes the source path deterministic for the hardlink stage.
        """
        path = PurePosixPath(old_path)
        if len(path.parts) <= 1:
            return basename
        known_roots = {value.casefold() for value in root_names if value}
        if known_roots and path.parts[0].casefold() not in known_roots:
            return basename
        return str(PurePosixPath(path.parts[0]) / basename)

    @staticmethod
    def _unique_path(path: str, used: set[str]) -> str:
        if path not in used:
            used.add(path)
            return path
        value = PurePosixPath(path)
        counter = 2
        while True:
            candidate = str(value.with_name(f"{value.stem}.{counter}{value.suffix}"))
            if candidate not in used:
                used.add(candidate)
                return candidate
            counter += 1

    @staticmethod
    def _title_prefix(plan: NamingPlan, part: Optional[str] = None) -> str:
        value = plan.media_name
        if plan.original_title and plan.original_title.casefold() != plan.media_name.casefold():
            value += f".{plan.original_title}"
        selected_part = part or plan.part
        if selected_part:
            value += f".{selected_part}"
        return value

    @staticmethod
    def _file_part(
        path: PurePosixPath,
        index: int,
        main_count: int,
        *,
        auto_number: bool,
    ) -> Optional[str]:
        stem = path.stem
        disc = re.search(r"(?i)(?:^|[ ._\-])(?:cd|disc|disk)[ ._\-]*0*(\d+)", stem)
        if disc:
            return f"CD{int(disc.group(1))}"
        part = re.search(r"(?i)(?:^|[ ._\-])(?:part|pt)[ ._\-]*0*(\d+)", stem)
        if part:
            return f"Part{int(part.group(1))}"
        return f"Part{index + 1}" if auto_number and main_count > 1 else None

    @staticmethod
    def _file_video_format(path: PurePosixPath, fallback: Optional[str]) -> Optional[str]:
        """Prefer a video's own resolution while retaining release-level format details."""
        lowered = path.stem.casefold()
        resolution = next(
            (value for value in ("4320p", "2160p", "1080p", "720p", "480p") if value in lowered),
            None,
        )
        if resolution is None and re.search(r"(?:^|[ ._\-])(?:4k|uhd)(?:$|[ ._\-])", lowered):
            resolution = "2160p"
        if not resolution:
            return fallback
        details = [
            value
            for value in (fallback or "").split(".")
            if value and value.casefold() not in {"4320p", "2160p", "1080p", "720p", "480p"}
        ]
        return ".".join([resolution, *details])

    @classmethod
    def _movie_basename(
        cls,
        item: Dict[str, Any],
        plan: NamingPlan,
        index: int,
        main_count: int,
    ) -> str:
        path = PurePosixPath(item["name"])
        stem = path.stem
        lowered = stem.casefold()
        extension = path.suffix.lower()
        if re.search(r"(?:^|[ ._\-])trailer(?:$|[ ._\-])|预告", lowered):
            part = "Trailer"
        elif re.search(r"(?:^|[ ._\-])sample(?:$|[ ._\-])|样片", lowered):
            part = "Sample"
        else:
            part = cls._file_part(path, index, main_count, auto_number=True)
        value = cls._title_prefix(plan, part)
        if plan.year:
            value += f" ({plan.year})"
        if plan.edition:
            value += f".{plan.edition}"
        video_format = cls._file_video_format(path, plan.video_format)
        if video_format:
            value += f".{video_format}"
        return f"{value}{extension}"

    @staticmethod
    def _subtitle_language_suffix(old_stem: str, video_stem: str) -> str:
        remainder = old_stem[len(video_stem) :].strip(" ._-").casefold()
        if any(token in remainder for token in ("chs", "zh-cn", "zh_hans", "简", "chi", "zho")):
            return ".zh-CN"
        if any(token in remainder for token in ("cht", "zh-tw", "zh_hant", "繁")):
            return ".zh-TW"
        if any(token in remainder for token in ("eng", "english", " en")) or remainder == "en":
            return ".en"
        return ""

    def plan_files(self, files: Iterable[Dict[str, Any]], plan: NamingPlan) -> Dict[str, Any]:
        values = [item for item in files if item.get("name")]
        if not plan.rename_enabled:
            return {
                "root_name": plan.root_name,
                "video_count": sum(
                    PurePosixPath(item["name"]).suffix.lower() in VIDEO_EXTENSIONS for item in values
                ),
                "file_count": len(values),
                "operations": [],
                "folder_operations": [],
                "unresolved_episodes": [],
            }
        videos = [item for item in values if PurePosixPath(item["name"]).suffix.lower() in VIDEO_EXTENSIONS]
        subtitles = [item for item in values if PurePosixPath(item["name"]).suffix.lower() in SUBTITLE_EXTENSIONS]
        videos.sort(key=lambda item: (-int(item.get("size") or 0), item["name"].casefold()))
        operations: List[Dict[str, str]] = []
        video_map: List[Tuple[str, str]] = []
        episode_seasons: List[int] = []
        unresolved_episodes: List[str] = []
        used_paths: set[str] = set()

        main_videos = [
            item
            for item in videos
            if not re.search(r"(?i)(?:^|[ ._\-])(?:sample|trailer)(?:$|[ ._\-])|样片|预告", PurePosixPath(item["name"]).stem)
        ]
        for index, item in enumerate(videos):
            old_path = item["name"]
            old_value = PurePosixPath(old_path)
            if policy_for(plan.media_type).naming_layout == "episodic":
                detected = detect_episode(old_value.name, allow_numeric_prefix=True) or (
                    plan.episode if len(videos) == 1 else None
                )
                season = detect_season(old_value.name) or plan.season
                episode = _episode_with_default_season(detected, season)
                if not episode:
                    unresolved_episodes.append(old_path)
                    continue
                episode_seasons.append(int(episode[1:3]))
                # Episodic files are separate episodes, not automatically numbered movie parts.
                part = self._file_part(old_value, index, len(videos), auto_number=False)
                value = self._title_prefix(plan, part)
                value += f".{episode}"
                if plan.episode_title:
                    value += f".{plan.episode_title}"
                video_format = self._file_video_format(old_value, plan.video_format)
                if video_format:
                    value += f".{video_format}"
                new_basename = f"{value}{old_value.suffix.lower()}"
            else:
                main_index = main_videos.index(item) if item in main_videos else index
                new_basename = self._movie_basename(item, plan, main_index, len(main_videos))
            if policy_for(plan.media_type).naming_layout == "movie":
                planned_path = self._movie_path(
                    old_path,
                    safe_name(new_basename),
                    (plan.media_name, plan.root_name),
                )
            else:
                planned_path = self._new_path(old_path, safe_name(new_basename))
            new_path = self._unique_path(planned_path, used_paths)
            video_map.append((str(old_value.with_suffix("")), str(PurePosixPath(new_path).with_suffix(""))))
            if new_path != old_path:
                operations.append({"kind": "file", "old_path": old_path, "new_path": new_path})

        for item in subtitles:
            old_path = item["name"]
            old_value = PurePosixPath(old_path)
            old_stem_path = str(old_value.with_suffix(""))
            match = None
            for video_old_stem, video_new_stem in video_map:
                if old_stem_path == video_old_stem or old_stem_path.startswith(video_old_stem + "."):
                    match = (video_old_stem, video_new_stem)
                    break
            if not match and len(video_map) == 1:
                match = video_map[0]
            if not match:
                continue
            language = self._subtitle_language_suffix(old_stem_path, match[0])
            new_path = self._unique_path(f"{match[1]}{language}{old_value.suffix.lower()}", used_paths)
            if new_path != old_path:
                operations.append({"kind": "file", "old_path": old_path, "new_path": new_path})

        folder_operations: List[Dict[str, str]] = []
        video_parents = [PurePosixPath(item["name"]).parts[0] for item in videos if len(PurePosixPath(item["name"]).parts) > 1]
        if video_parents and len(set(video_parents)) == 1:
            old_folder = video_parents[0]
            folder_season = detect_season(old_folder)
            if (
                folder_season is None
                and policy_for(plan.media_type).episodic
                and len(episode_seasons) == len(videos)
                and len(set(episode_seasons)) == 1
            ):
                folder_season = episode_seasons[0]
            if folder_season is not None:
                new_folder = f"Season {folder_season:02d}"
                if old_folder != new_folder:
                    folder_operations.append({"kind": "folder", "old_path": old_folder, "new_path": new_folder})

        return {
            "root_name": plan.root_name,
            "video_count": len(videos),
            "file_count": len(values),
            "operations": operations,
            "folder_operations": folder_operations,
            "unresolved_episodes": unresolved_episodes,
        }


class NamingJobRepository:
    def __init__(self, path: Path, state_store: StateStore | None = None) -> None:
        self.path = path
        self.state_store = state_store
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_store:
            try: self.state_store.ensure_records("naming_jobs", legacy_path=self.path)
            except StateStoreError as exc: raise AppError("Naming Job Repository Error", str(exc), "naming-repository-error", 500) from exc
        elif not self.path.exists():
            self._write({"items": []})

    def _read(self) -> Dict[str, Any]:
        if self.state_store:
            try: return {"items": self.state_store.list_records("naming_jobs")}
            except StateStoreError as exc: raise AppError("Naming Job Repository Error", str(exc), "naming-repository-error", 500) from exc
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError("invalid naming jobs shape")
            return value
        except OSError as exc:
            # A transient disk/permission failure must never look like an empty
            # job database: that loses the only reconciliation record.
            raise AppError("Naming Job Repository Error", f"could not read naming jobs: {exc}", "naming-repository-error", 500) from exc
        except (ValueError, json.JSONDecodeError):
            backup = self.path.with_suffix(f".corrupt-{uuid.uuid4().hex[:8]}.json")
            if self.path.exists():
                self.path.replace(backup)
            value = {"items": []}
            self._write(value)
            return value

    def _write(self, value: Dict[str, Any]) -> None:
        if self.state_store:
            try: self.state_store.replace_records("naming_jobs", value["items"]); return
            except StateStoreError as exc: raise AppError("Naming Job Repository Error", str(exc), "naming-repository-error", 500) from exc
        fd, temp_name = tempfile.mkstemp(prefix="naming-jobs-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return deepcopy(self._read()["items"])

    def get(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            for item in self._read()["items"]:
                if item["id"] == job_id:
                    return deepcopy(item)
        raise NotFoundError("naming job", job_id)

    def upsert(self, torrent_hash_value: str, plan: NamingPlan, final_category: str, **submission: Any) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item.get("torrent_hash") == torrent_hash_value:
                    return deepcopy(item)
            now = utc_now_iso()
            item = {
                "id": uuid.uuid4().hex,
                "torrent_hash": torrent_hash_value,
                "final_category": final_category,
                "plan": plan.to_dict(),
                "status": "pending",
                "attempts": 0,
                "checks": 0,
                "created_at": now,
                "updated_at": now,
                "last_check": None,
                "last_error": None,
                "result": None,
                "hardlink_status": None,
                "hardlink_attempts": 0,
                "hardlink_error": None,
                "hardlink_result": None,
                "processed_file_indices": [],
                "rename_checkpoint": {"files": 0, "folders": 0, "torrent": False},
                **deepcopy(submission),
            }
            data["items"].append(item)
            self._write(data)
            return deepcopy(item)

    def update(self, job_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item["id"] == job_id:
                    item.update(deepcopy(changes))
                    item["updated_at"] = utc_now_iso()
                    self._write(data)
                    return deepcopy(item)
        raise NotFoundError("naming job", job_id)

    def delete(self, job_id: str) -> Dict[str, Any]:
        """Delete only the AVS job record; downloader tasks and files are untouched."""
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["items"]):
                if item["id"] == job_id:
                    deleted = data["items"].pop(index)
                    self._write(data)
                    return deepcopy(deleted)
        raise NotFoundError("naming job", job_id)


class NamingService:
    def __init__(
        self,
        settings: Settings,
        repository: NamingJobRepository,
        qb: QBittorrentClient,
        planner: Optional[EmbyNamingPlanner] = None,
        hardlinker: Optional[Any] = None,
        path_rules: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.qb = qb
        self.planner = planner or EmbyNamingPlanner()
        self.hardlinker = hardlinker
        self.path_rules = path_rules
        self._lock = threading.Lock()

    @staticmethod
    def _rename_started(job: Dict[str, Any]) -> bool:
        checkpoint = dict(job.get("rename_checkpoint") or {})
        return bool(
            int(checkpoint.get("files") or 0)
            or int(checkpoint.get("folders") or 0)
            or checkpoint.get("torrent")
        )

    @classmethod
    def _plan_locked(cls, job: Dict[str, Any]) -> bool:
        return bool(
            cls._rename_started(job)
            or job.get("processed_file_indices")
            or (job.get("hardlink_result") or {}).get("files")
            or job.get("hardlink_status") in {"done", "partial", "conflict"}
        )

    @staticmethod
    def _is_padding_file(item: Dict[str, Any]) -> bool:
        path = PurePosixPath(str(item.get("name") or ""))
        lowered = tuple(part.casefold() for part in path.parts)
        return bool(
            ".pad" in lowered
            or path.suffix.casefold() == ".pad"
            or path.name.casefold().startswith(PADDING_PREFIXES)
        )

    @staticmethod
    def _operation_key(operation: Dict[str, Any]) -> str:
        return f"{operation.get('old_path') or ''}\0{operation.get('new_path') or ''}"

    @classmethod
    def _completed_operation_keys(
        cls,
        job: Dict[str, Any],
        preview: Dict[str, Any],
    ) -> List[str]:
        checkpoint = dict(job.get("rename_checkpoint") or {})
        keys = [str(value) for value in checkpoint.get("file_operations") or []]
        legacy_count = int(checkpoint.get("files") or 0)
        for operation in (preview.get("operations") or [])[:legacy_count]:
            key = cls._operation_key(operation)
            if key not in keys:
                keys.append(key)
        return keys

    @staticmethod
    def _merge_hardlink_results(
        previous: Optional[Dict[str, Any]],
        current: Optional[Dict[str, Any]],
        *,
        waiting: bool,
    ) -> Dict[str, Any]:
        before = dict(previous or {})
        after = dict(current or {})
        merged_files: Dict[str, Dict[str, Any]] = {}
        rank = {"conflict": 0, "already-linked": 1, "linked": 2}
        for item in [*(before.get("files") or []), *(after.get("files") or [])]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("target") or item.get("source") or len(merged_files))
            existing = merged_files.get(key)
            if existing is None or rank.get(str(item.get("status")), 0) >= rank.get(
                str(existing.get("status")), 0
            ):
                merged_files[key] = dict(item)
        files = list(merged_files.values())
        if files:
            linked = sum(item.get("status") == "linked" for item in files)
            already_linked = sum(item.get("status") == "already-linked" for item in files)
            conflicts = sum(item.get("status") == "conflict" for item in files)
            skipped = already_linked + conflicts
        else:
            linked = int(before.get("linked") or 0) + int(after.get("linked") or 0)
            already_linked = int(before.get("already_linked") or 0) + int(
                after.get("already_linked") or 0
            )
            conflicts = int(before.get("conflicts") or 0) + int(after.get("conflicts") or 0)
            skipped = int(before.get("skipped") or 0) + int(after.get("skipped") or 0)
        if waiting:
            status = "waiting_download"
        elif conflicts:
            status = "partial" if linked or already_linked else "conflict"
        else:
            status = "done"
        return {
            "status": status,
            "linked": linked,
            "skipped": skipped,
            "already_linked": already_linked,
            "conflicts": conflicts,
            "target": after.get("target") or before.get("target"),
            "files": files,
        }

    @staticmethod
    def _unresolved_episode_paths(
        files: Iterable[Dict[str, Any]],
        plan: NamingPlan,
        preview: Dict[str, Any],
    ) -> List[str]:
        """Return episodic videos whose final planned basename has no episode token.

        Deriving this from the operation journal also protects jobs whose
        preview was persisted by an older AVS version without the diagnostic
        ``unresolved_episodes`` field.
        """
        if not policy_for(plan.media_type).episodic:
            return []
        renamed = {
            str(operation.get("old_path")): str(operation.get("new_path"))
            for operation in preview.get("operations") or []
            if isinstance(operation, dict) and operation.get("old_path") and operation.get("new_path")
        }
        unresolved: List[str] = []
        for item in files:
            old_path = str(item.get("name") or "")
            if not old_path or PurePosixPath(old_path).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            final_path = renamed.get(old_path, old_path)
            if not detect_episode(PurePosixPath(final_path).name, allow_numeric_prefix=True):
                unresolved.append(old_path)
        return unresolved

    def _require_safe_episode_plan(
        self,
        files: Iterable[Dict[str, Any]],
        plan: NamingPlan,
        preview: Dict[str, Any],
    ) -> None:
        unresolved = self._unresolved_episode_paths(files, plan, preview)
        if not unresolved:
            return
        sample = "、".join(unresolved[:3])
        suffix = "" if len(unresolved) <= 3 else f" 等 {len(unresolved)} 个文件"
        raise AppError(
            "Episode Naming Requires Attention",
            f"电视剧/动漫文件无法识别集号，已停止入库：{sample}{suffix}",
            "episode-number-unresolved",
            409,
        )

    @staticmethod
    def _canonicalize_unstarted_plan(plan: NamingPlan) -> NamingPlan:
        """Drop release qualifiers from legacy episodic plans before any rename."""
        if not policy_for(plan.media_type).episodic:
            return plan
        media_name = safe_name(canonical_media_name(plan.media_name))
        if media_name == plan.media_name:
            return plan
        root_name = safe_name(f"{media_name} ({plan.year})" if plan.year else media_name)
        return replace(plan, media_name=media_name, root_name=root_name)

    def discard_record(self, job_id: str) -> Dict[str, Any]:
        """Abandon AVS post-processing without touching downloader tasks or files."""
        if not self._lock.acquire(blocking=False):
            raise ConflictError("naming job is currently being processed; retry after it finishes")
        try:
            job = self.repository.get(job_id)
            naming_failure = job.get("status") in {"failed", "missing_in_downloader"}
            active_unfinished = job.get("status") in {
                "submitting",
                "awaiting_binding",
                "pending",
                "waiting_metadata",
                "waiting_download",
                "waiting_selection",
                "retrying",
            }
            hardlink_unfinished = job.get("status") == "completed" and job.get("hardlink_status") in {
                "pending",
                "waiting_download",
                "retrying",
                "failed",
                "partial",
                "conflict",
            }
            if active_unfinished and self._rename_started(job):
                raise ConflictError("cannot discard an active job after rename operations have started")
            if not naming_failure and not active_unfinished and not hardlink_unfinished:
                raise ConflictError("only failed or safely abandonable AVS records can be discarded")
            return self.repository.delete(job_id)
        finally:
            self._lock.release()

    def _download_path(
        self,
        plan: NamingPlan,
        final_category: str,
        rule: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """Return the downloader-visible path, distinct from AVS's host source path."""
        policy = policy_for(plan.media_type)
        root = (rule or {}).get("downloader_path") or policy.download_path_for(self.settings)
        return Path(root) / safe_name(plan.root_name)

    def preview_plan(
        self, release: Release, final_category: str, path_rule_id: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Preview naming and path-rule choices without creating a job or torrent."""
        plan = self.planner.from_release(release)
        rule = self._rule_for(release.media_type, final_category, path_rule_id)
        if rule:
            plan = replace(plan, rename_enabled=bool(rule["rename_enabled"]) and policy_for(release.media_type).rename_enabled)
        if overrides:
            allowed = set(NamingPlan.__dataclass_fields__)
            plan = replace(plan, **{key: value for key, value in overrides.items() if key in allowed})
        return {"plan": plan.to_dict(), "save_path": str(self._download_path(plan, final_category, rule)), "path_rule_id": rule.get("id") if rule else None}

    def apply_corrected_plan(self, job_id: str, plan: NamingPlan | Dict[str, Any]) -> Dict[str, Any]:
        """Replace an uncommitted plan; completed links make this unsafe."""
        job = self.repository.get(job_id)
        if self._plan_locked(job):
            raise AppError(
                "Naming Plan Locked",
                "cannot replace a plan after naming or hardlink processing has started",
                "naming-plan-locked",
                409,
            )
        value = plan.to_dict() if isinstance(plan, NamingPlan) else NamingPlan(**plan).to_dict()
        final_category = policy_for(value["media_type"]).category_for(self.settings)
        return self.repository.update(
            job_id,
            {
                "plan": value,
                "final_category": final_category,
                "result": None,
                "rename_checkpoint": {"files": 0, "folders": 0, "torrent": False},
                "processed_file_indices": [],
                "status": "retrying",
                "attempts": 0,
                "last_error": None,
            },
        )

    def preview_corrected_plan(self, job_id: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Preview a correction against the downloader's current file list."""
        job = self.repository.get(job_id)
        if self._plan_locked(job):
            raise AppError(
                "Naming Plan Locked",
                "cannot replace a plan after naming or hardlink processing has started",
                "naming-plan-locked",
                409,
            )
        current = dict(job["plan"])
        for key, value in overrides.items():
            if key in NamingPlan.__dataclass_fields__:
                current[key] = value
        media_name = safe_name(str(current.get("media_name") or "未命名媒体"))
        current["media_name"] = media_name
        policy = policy_for(str(current.get("media_type") or ""))
        if policy.naming_layout == "preserve":
            current["root_name"] = safe_name(str(current.get("link_name") or media_name))
            current["rename_enabled"] = False
        else:
            current["root_name"] = safe_name(
                f"{media_name} ({current['year']})" if current.get("year") else media_name
            )
        plan = NamingPlan(**current)
        torrent = None
        files: List[Dict[str, Any]] = []
        task_hash = str(job.get("torrent_hash") or "")
        if task_hash and not task_hash.startswith("pending:"):
            torrent = self.qb.torrent_info(task_hash)
            files = self.qb.files(task_hash) if torrent else []
        preview = self.planner.plan_files(files, plan) if files else None
        return {"job_id": job_id, "plan": plan.to_dict(), "preview": preview, "torrent_found": bool(torrent)}

    def _rule_for(self, media_type: str, final_category: str, path_rule_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not self.path_rules:
            return None
        if path_rule_id:
            rule = self.path_rules.get(path_rule_id)
            if rule.get("media_type") != media_type or not rule.get("enabled"):
                raise AppError("Invalid Path Rule", "selected path rule is disabled or does not match this media type", "invalid-path-rule", 400)
            return rule
        return self.path_rules.match(media_type, self.settings.download_path_for_category(final_category))

    def _submission(
        self,
        plan: NamingPlan,
        final_category: str,
        current_category: str,
        save_path: Path,
        *,
        preserve_task_name: bool = False,
    ) -> Dict[str, Any]:
        return {"submission": {"category": current_category, "final_category": final_category,
                "save_path": str(save_path), "root_name": plan.root_name,
                "match_name": None if preserve_task_name else plan.root_name}}

    def add_download(
        self,
        release: Release,
        final_category: str,
        path_rule_id: Optional[str] = None,
        wanted_episodes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        plan = self.planner.from_release(release)
        rule = self._rule_for(release.media_type, final_category, path_rule_id)
        if rule:
            plan = replace(plan, rename_enabled=bool(rule["rename_enabled"]) and policy_for(release.media_type).rename_enabled)
        hash_value = torrent_hash(release.download_link)
        use_staging = self.settings.naming_enabled and plan.rename_enabled
        track_only = not plan.rename_enabled and self.settings.medialib_hardlink_enabled
        current_category = self.settings.qb_naming_category if use_staging else final_category
        save_path = self._download_path(plan, final_category, rule)
        # Persist before submitting.  If qB accepts but the later local write
        # fails, this record is sufficient to resume without duplicating work.
        pending_hash = hash_value or f"pending:{release.id}"
        job = (
            self.repository.upsert(
                pending_hash,
                plan,
                final_category,
                wanted_episodes=list(wanted_episodes or []),
                selection=None,
                preserve_task_name=True,
                strict_source_paths=True,
                **self._submission(
                    plan,
                    final_category,
                    current_category,
                    save_path,
                    preserve_task_name=True,
                ),
            )
            if (use_staging or track_only)
            else None
        )
        if job and not hash_value:
            job = self.repository.update(job["id"], {"status": "submitting"})
        add_options: Dict[str, Any] = {
            # Keep the downloader's original task name. It is useful for
            # diagnostics and does not control names inside the torrent.
            "rename": None,
            "save_path": save_path,
        }
        if wanted_episodes and job:
            add_options["paused"] = True
        result = self.qb.add_download(release.download_link, current_category, **add_options)
        task_id = result.get("qb_task_id") or hash_value
        if job and not task_id:
            reconciler = getattr(self.qb, "reconcile_submission", None)
            if callable(reconciler):
                matched = reconciler(
                    category=current_category,
                    save_path=save_path,
                    root_name="",
                    expected_hash=None,
                )
                task_id = (matched or {}).get("hash")
        if job and task_id:
            job = self.repository.update(job["id"], {"torrent_hash": task_id, "status": "pending"})
        elif job:
            job = self.repository.update(job["id"], {"status": "awaiting_binding"})
        return {
            **result,
            "category": final_category,
            "current_category": current_category,
            "planned_name": plan.root_name,
            "naming_job_id": job["id"] if job else None,
            "naming_status": job["status"] if job else ("disabled" if not self.settings.naming_enabled else "unsupported"),
            "hardlink_status": job.get("hardlink_status") if job else ("disabled" if not self.settings.medialib_hardlink_enabled else None),
        }

    def add_torrent_file(
        self,
        release: Release,
        final_category: str,
        content: bytes,
        filename: str,
        torrent_hash_value: str,
        path_rule_id: Optional[str] = None,
        wanted_episodes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        plan = self.planner.from_release(release)
        rule = self._rule_for(release.media_type, final_category, path_rule_id)
        if rule:
            plan = replace(plan, rename_enabled=bool(rule["rename_enabled"]) and policy_for(release.media_type).rename_enabled)
        use_staging = self.settings.naming_enabled and plan.rename_enabled
        track_only = not plan.rename_enabled and self.settings.medialib_hardlink_enabled
        current_category = self.settings.qb_naming_category if use_staging else final_category
        save_path = self._download_path(plan, final_category, rule)
        job = self.repository.upsert(
            torrent_hash_value,
            plan,
            final_category,
            wanted_episodes=list(wanted_episodes or []),
            selection=None,
            preserve_task_name=True,
            strict_source_paths=True,
            **self._submission(
                plan,
                final_category,
                current_category,
                save_path,
                preserve_task_name=True,
            ),
        ) if (use_staging or track_only) else None
        add_options: Dict[str, Any] = {
            "rename": None,
            "save_path": save_path,
        }
        if wanted_episodes and job:
            add_options["paused"] = True
        result = self.qb.add_torrent_file(content, filename, current_category, **add_options)
        if job and result.get("qb_task_id") and result["qb_task_id"] != job.get("torrent_hash"):
            job = self.repository.update(job["id"], {"torrent_hash": result["qb_task_id"]})
        return {
            **result,
            "category": final_category,
            "current_category": current_category,
            "planned_name": plan.root_name,
            "naming_job_id": job["id"] if job else None,
            "naming_status": job["status"] if job else "disabled",
            "hardlink_status": job.get("hardlink_status") if job else None,
        }

    def _release_staging_job(self, job: Dict[str, Any]) -> None:
        try:
            self._set_final_category(job)
            self.qb.resume(job["torrent_hash"])
        except AppError:
            logger.exception("naming_job_release_failed", extra={"job_id": job["id"]})

    def _set_final_category(self, job: Dict[str, Any]) -> None:
        """Apply the final label without discarding a user-selected path rule."""
        submission = job.get("submission") or {}
        save_path = submission.get("save_path")
        location = None
        if save_path:
            location = str(Path(save_path).parent)
        setter = getattr(self.qb, "set_category")
        try:
            setter(job["torrent_hash"], job["final_category"], location=location)
        except TypeError:
            # qBittorrent's adapter has no location argument; its category
            # mapping already points at the selected route.
            setter(job["torrent_hash"], job["final_category"])

    def _process_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now_iso()
        checks = int(job.get("checks", 0)) + 1
        task_hash = str(job.get("torrent_hash") or "")
        if task_hash.startswith("pending:") or job.get("status") in {"submitting", "awaiting_binding"}:
            submission = dict(job.get("submission") or {})
            reconciler = getattr(self.qb, "reconcile_submission", None)
            match = None
            if callable(reconciler) and submission:
                try:
                    match = reconciler(
                        category=str(submission.get("category") or ""),
                        save_path=submission.get("save_path"),
                        root_name=str(
                            (
                                submission.get("match_name")
                                if "match_name" in submission
                                else submission.get("root_name")
                            )
                            or ""
                        ),
                        expected_hash=None,
                    )
                except AppError as exc:
                    return self.repository.update(
                        job["id"],
                        {"status": "awaiting_binding", "checks": checks, "last_check": now, "last_error": exc.detail},
                    )
            bound_hash = str((match or {}).get("hash") or "")
            if not bound_hash:
                terminal = checks >= self.settings.naming_max_attempts
                return self.repository.update(
                    job["id"],
                    {
                        "status": "missing_in_downloader" if terminal else "awaiting_binding",
                        "checks": checks,
                        "last_check": now,
                        "last_error": "accepted submission could not yet be bound to a downloader task",
                    },
                )
            job = self.repository.update(
                job["id"],
                {"torrent_hash": bound_hash, "status": "pending", "last_error": None},
            )
        try:
            torrent = self.qb.torrent_info(job["torrent_hash"])
        except AppError as exc:
            attempts = int(job.get("attempts", 0)) + 1
            return self.repository.update(
                job["id"],
                {
                    "status": "failed" if attempts >= self.settings.naming_max_attempts else "retrying",
                    "attempts": attempts,
                    "checks": checks,
                    "last_check": now,
                    "last_error": exc.detail,
                },
            )
        if not torrent:
            # This is distinct from a temporary metadata delay.  The bounded
            # threshold prevents a permanently lost downloader task from
            # leaving an invisible pending job forever; explicit check(job)
            # resets it for manual recovery.
            if checks >= self.settings.naming_max_attempts:
                return self.repository.update(job["id"], {"status": "missing_in_downloader", "checks": checks, "last_check": now, "last_error": "torrent is absent from downloader"})
            return self.repository.update(
                job["id"],
                {"status": "waiting_metadata", "checks": checks, "last_check": now, "last_error": None},
            )
        try:
            files = self.qb.files(job["torrent_hash"])
        except AppError as exc:
            attempts = int(job.get("attempts", 0)) + 1
            return self.repository.update(
                job["id"],
                {
                    "status": "failed" if attempts >= self.settings.naming_max_attempts else "retrying",
                    "attempts": attempts,
                    "checks": checks,
                    "last_check": now,
                    "last_error": exc.detail,
                },
            )
        if not files:
            return self.repository.update(
                job["id"],
                {"status": "waiting_metadata", "checks": checks, "last_check": now, "last_error": None},
            )
        wanted_episodes = list(job.get("wanted_episodes") or [])
        selection = dict(job.get("selection") or {})
        if wanted_episodes and not selection.get("applied"):
            selection = select_episode_files(files, wanted_episodes)
            if not selection["safe"]:
                return self.repository.update(
                    job["id"],
                    {
                        "status": "waiting_selection",
                        "selection": selection,
                        "checks": checks,
                        "last_check": now,
                        "last_error": "全集文件无法安全拆分，请确认后再继续",
                    },
                )
            self.qb.set_file_priorities(
                job["torrent_hash"],
                selection["selected_indices"],
                selection["skipped_indices"],
            )
            self.qb.resume(job["torrent_hash"])
            selection = {**selection, "applied": True, "applied_at": now}
            job = self.repository.update(
                job["id"],
                {
                    "status": "waiting_download",
                    "selection": selection,
                    "checks": checks,
                    "last_check": now,
                    "last_error": None,
                },
            )
        progress = float(torrent.get("progress", 0) or 0)
        selected_indices = set(selection.get("selected_indices") or []) if selection.get("applied") else None
        selected = [
            {
                **item,
                "index": int(item.get("index") if item.get("index") is not None else fallback_index),
            }
            for fallback_index, item in enumerate(files)
            if (
                int(item.get("index") if item.get("index") is not None else fallback_index) in selected_indices
                if selected_indices is not None
                else int(item.get("priority", 1) or 0) > 0
            )
            and not self._is_padding_file(item)
        ]
        if not selected:
            return self.repository.update(
                job["id"],
                {"status": "waiting_metadata", "checks": checks, "last_check": now, "last_error": None},
            )
        completed_files = [
            item for item in selected if float(item.get("progress", progress) or 0) >= 0.999999
        ]
        files_complete = len(completed_files) == len(selected)
        if not completed_files:
            return self.repository.update(
                job["id"],
                {
                    "status": "waiting_download",
                    "checks": checks,
                    "last_check": now,
                    "last_error": None,
                },
            )
        plan = NamingPlan(**job["plan"])
        if not self._rename_started(job):
            normalized_plan = self._canonicalize_unstarted_plan(plan)
            if normalized_plan != plan:
                plan = normalized_plan
                job = self.repository.update(job["id"], {"plan": plan.to_dict(), "result": None})
        # The preview covers the whole selected torrent and remains the operation
        # journal while individual completed files are processed incrementally.
        preview = job.get("result") or self.planner.plan_files(selected, plan)
        if job.get("strict_source_paths") and not preview.get("strict_source_paths"):
            preview = {**preview, "strict_source_paths": True}
        if not job.get("result"):
            job = self.repository.update(
                job["id"],
                {
                    "result": preview,
                    "rename_checkpoint": job.get("rename_checkpoint")
                    or {"files": 0, "folders": 0, "torrent": False},
                },
            )
        linking_batch = False
        try:
            verifier = getattr(self.hardlinker, "verify_named_sources", None)
            already_named = False
            if files_complete and callable(verifier):
                try:
                    verifier(torrent, selected, preview)
                    already_named = True
                except AppError:
                    pass
            processed_indices = {int(value) for value in job.get("processed_file_indices") or []}
            ready = [item for item in completed_files if int(item["index"]) not in processed_indices]
            checkpoint = dict(job.get("rename_checkpoint") or {})
            completed_operation_keys = self._completed_operation_keys(job, preview)
            # Validate only the files that are actually complete in this
            # pass.  One malformed/unresolved episode in a 33-episode pack
            # must not block renaming and linking the other 32 files.
            if policy_for(plan.media_type).episodic and ready:
                unresolved = set(self._unresolved_episode_paths(ready, plan, preview))
                ready = [item for item in ready if str(item.get("name") or "") not in unresolved]
            if already_named:
                completed_operation_keys = [
                    self._operation_key(operation) for operation in preview.get("operations") or []
                ]
                checkpoint["file_operations"] = completed_operation_keys
                checkpoint["files"] = len(completed_operation_keys)
                checkpoint["folders"] = len(preview.get("folder_operations") or [])
                job = self.repository.update(
                    job["id"], {"rename_checkpoint": checkpoint, "result": preview}
                )
            if plan.rename_enabled and not already_named:
                ready_paths = {str(item.get("name") or "") for item in ready}
                current_paths = {str(item.get("name") or "") for item in selected}
                for operation in preview.get("operations") or []:
                    key = self._operation_key(operation)
                    if key in completed_operation_keys:
                        continue
                    old_path = str(operation.get("old_path") or "")
                    new_path = str(operation.get("new_path") or "")
                    if old_path not in ready_paths and new_path not in ready_paths:
                        continue
                    if new_path not in current_paths or old_path in current_paths:
                        self.qb.rename_file(job["torrent_hash"], old_path, new_path)
                    current_paths.discard(old_path)
                    current_paths.add(new_path)
                    completed_operation_keys.append(key)
                    checkpoint["file_operations"] = completed_operation_keys
                    checkpoint["files"] = len(completed_operation_keys)
                    job = self.repository.update(
                        job["id"], {"rename_checkpoint": checkpoint, "result": preview}
                    )

                # A successful HTTP response is not enough: the downloader's
                # file list must expose the planned path before hardlinking.
                # This prevents a stale-source fallback from hiding a no-op.
                if ready and job.get("strict_source_paths"):
                    realized_paths = {
                        str(item.get("name") or "") for item in self.qb.files(job["torrent_hash"])
                    }
                    expected_paths = {
                        str(operation.get("new_path") or "")
                        for operation in preview.get("operations") or []
                        if self._operation_key(operation) in completed_operation_keys
                        and (
                            str(operation.get("old_path") or "") in ready_paths
                            or str(operation.get("new_path") or "") in ready_paths
                        )
                    }
                    missing = sorted(path for path in expected_paths if path not in realized_paths)
                    if missing:
                        # Do not leave an unverified rename checkpoint marked
                        # as complete.  A later retry must be allowed to issue
                        # the same file rename again if the downloader really
                        # treated the first request as a no-op.
                        missing_values = set(missing)
                        missing_keys = {
                            self._operation_key(operation)
                            for operation in preview.get("operations") or []
                            if str(operation.get("new_path") or "") in missing_values
                        }
                        completed_operation_keys = [
                            key for key in completed_operation_keys if key not in missing_keys
                        ]
                        checkpoint["file_operations"] = completed_operation_keys
                        checkpoint["files"] = len(completed_operation_keys)
                        job = self.repository.update(
                            job["id"], {"rename_checkpoint": checkpoint, "result": preview}
                        )
                        raise AppError(
                            "Downloader Rename Not Applied",
                            f"下载器未确认实际文件改名：{missing[0]}",
                            "downloader-rename-not-applied",
                            409,
                        )

            hardlink_enabled = bool(self.hardlinker and self.hardlinker.enabled)
            hardlink_result = dict(job.get("hardlink_result") or {})
            if ready:
                if hardlink_enabled:
                    linking_batch = True
                    batch_result = self.hardlinker.link_completed(torrent, ready, plan, preview)
                    linking_batch = False
                    hardlink_result = self._merge_hardlink_results(
                        hardlink_result,
                        batch_result,
                        waiting=not files_complete,
                    )
                processed_indices.update(int(item["index"]) for item in ready)
                job = self.repository.update(
                    job["id"],
                    {
                        "status": "waiting_download",
                        "checks": checks,
                        "last_check": now,
                        "last_error": None,
                        "result": preview,
                        "processed_file_indices": sorted(processed_indices),
                        "hardlink_status": "waiting_download" if hardlink_enabled else "disabled",
                        "hardlink_attempts": 0,
                        "hardlink_error": None,
                        "hardlink_result": hardlink_result if hardlink_enabled else None,
                    },
                )

            if not files_complete:
                return self.repository.update(
                    job["id"],
                    {
                        "status": "waiting_download",
                        "checks": checks,
                        "last_check": now,
                        "last_error": None,
                    },
                )

            # All selected files are complete, but one or more could not be
            # assigned an episode number.  Keep the job actionable instead of
            # failing the whole pack after the valid files were processed.
            unresolved_all = self._unresolved_episode_paths(selected, plan, preview)
            if unresolved_all:
                return self.repository.update(
                    job["id"],
                    {
                        "status": "waiting_selection",
                        "checks": checks,
                        "last_check": now,
                        "last_error": f"无法识别集号：{unresolved_all[0]}",
                        "processed_file_indices": sorted(processed_indices),
                    },
                )

            if plan.rename_enabled and not already_named:
                folder_index = int(checkpoint.get("folders", 0))
                current_paths = {str(item.get("name") or "") for item in selected}
                for index, operation in enumerate(
                    (preview.get("folder_operations") or [])[folder_index:], start=folder_index
                ):
                    old_path = str(operation.get("old_path") or "")
                    new_path = str(operation.get("new_path") or "")
                    old_prefix = old_path + "/"
                    new_prefix = new_path + "/"
                    old_present = any(path == old_path or path.startswith(old_prefix) for path in current_paths)
                    new_present = any(path == new_path or path.startswith(new_prefix) for path in current_paths)
                    if old_present or not new_present:
                        self.qb.rename_folder(job["torrent_hash"], old_path, new_path)
                    current_paths = {
                        new_path + path[len(old_path) :]
                        if path == old_path or path.startswith(old_prefix)
                        else path
                        for path in current_paths
                    }
                    checkpoint["folders"] = index + 1
                    job = self.repository.update(
                        job["id"], {"rename_checkpoint": checkpoint, "result": preview}
                    )
                if not (job.get("rename_checkpoint") or {}).get("torrent"):
                    if not job.get("preserve_task_name") and str(torrent.get("name") or "") != plan.root_name:
                        self.qb.rename_torrent(job["torrent_hash"], plan.root_name)
                    checkpoint = dict(job.get("rename_checkpoint") or {})
                    checkpoint["torrent"] = True
                    job = self.repository.update(
                        job["id"], {"rename_checkpoint": checkpoint, "result": preview}
                    )

            if callable(verifier):
                verifier(torrent, selected, preview)
            self._set_final_category(job)
            self.qb.resume(job["torrent_hash"])
            if hardlink_enabled:
                hardlink_result = self._merge_hardlink_results(
                    job.get("hardlink_result"), None, waiting=False
                )
                hardlink_status = str(hardlink_result.get("status") or "done")
            else:
                hardlink_result = None
                hardlink_status = "disabled"
            return self.repository.update(
                job["id"],
                {
                    "status": "completed",
                    "checks": checks,
                    "last_check": now,
                    "last_error": None,
                    "result": preview,
                    "processed_file_indices": sorted(processed_indices),
                    "hardlink_status": hardlink_status,
                    "hardlink_attempts": 0,
                    "hardlink_error": None,
                    "hardlink_result": hardlink_result,
                },
            )
        except AppError as exc:
            attempts = int(job.get("attempts", 0)) + 1
            failed = attempts >= self.settings.naming_max_attempts
            if failed:
                self._release_staging_job(job)
            changes = {
                "status": "failed" if failed else "retrying",
                "attempts": attempts,
                "checks": checks,
                "last_check": now,
                "last_error": exc.detail,
                "result": preview,
            }
            if linking_batch:
                hardlink_attempts = int(job.get("hardlink_attempts", 0)) + 1
                changes.update(
                    {
                        "hardlink_status": "failed"
                        if hardlink_attempts >= self.settings.naming_max_attempts
                        else "retrying",
                        "hardlink_attempts": hardlink_attempts,
                        "hardlink_error": exc.detail,
                    }
                )
            return self.repository.update(job["id"], changes)

    def _finish_hardlink(self, job: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now_iso()
        if not self.hardlinker or not self.hardlinker.enabled:
            return self.repository.update(
                job["id"],
                {"hardlink_status": "disabled", "hardlink_error": None, "last_check": now},
            )
        try:
            torrent = self.qb.torrent_info(job["torrent_hash"])
            if not torrent:
                attempts = int(job.get("hardlink_attempts", 0)) + 1
                failed = attempts >= self.settings.naming_max_attempts
                return self.repository.update(
                    job["id"],
                    {
                        "hardlink_status": "failed" if failed else "waiting_download",
                        "hardlink_attempts": attempts,
                        "hardlink_error": "torrent is absent from downloader",
                        "last_check": now,
                    },
                )
            # Downloaders disagree on whether completion is exposed as
            # ``completion_on``, ``completed`` or a percent value.  Use the
            # shared normalizer so a completed task with a stale 0% field is
            # not left in waiting_download forever.
            progress = normalized_task_progress(dict(torrent))
            if progress < 0.999999:
                return self.repository.update(
                    job["id"],
                    {"hardlink_status": "waiting_download", "hardlink_error": None, "last_check": now},
                )
            files = self.qb.files(job["torrent_hash"])
            if not files:
                return self.repository.update(
                    job["id"],
                    {
                        "hardlink_status": "waiting_download",
                        "hardlink_error": "qBittorrent file metadata is not ready",
                        "last_check": now,
                    },
                )
            plan = NamingPlan(**job["plan"])
            naming_result = job.get("result") or {}
            self._require_safe_episode_plan(files, plan, naming_result)
            result = self.hardlinker.link_completed(
                torrent,
                files,
                plan,
                naming_result,
            )
            hardlink_status = str(result.get("status") or "done") if isinstance(result, dict) else "done"
            if hardlink_status not in {"done", "partial", "conflict"}:
                hardlink_status = "failed"
            return self.repository.update(
                job["id"],
                {
                    "hardlink_status": hardlink_status,
                    "hardlink_result": result,
                    "hardlink_error": None,
                    "last_check": now,
                },
            )
        except Exception as exc:
            attempts = int(job.get("hardlink_attempts", 0)) + 1
            failed = attempts >= self.settings.naming_max_attempts
            detail = exc.detail if isinstance(exc, AppError) else str(exc) or "unexpected hardlink failure"
            logger.exception(
                "hardlink_finish_error",
                extra={"job_id": job["id"], "attempts": attempts, "failed": failed},
            )
            return self.repository.update(
                job["id"],
                {
                    "hardlink_status": "failed" if failed else "retrying",
                    "hardlink_attempts": attempts,
                    "hardlink_error": detail,
                    "last_check": now,
                },
            )

    def check(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"running": True, "checked": 0, "completed": 0, "results": []}
        try:
            if job_id:
                job = self.repository.get(job_id)
                if job["status"] in {"failed", "missing_in_downloader"}:
                    job = self.repository.update(job_id, {"status": "retrying", "attempts": 0, "last_error": None})
                if job["status"] == "completed":
                    if job.get("hardlink_status") == "failed":
                        job = self.repository.update(
                            job_id,
                            {"hardlink_status": "retrying", "hardlink_attempts": 0, "hardlink_error": None},
                        )
                    results = [self._finish_hardlink(job)]
                    return {
                        "running": False,
                        "checked": 1,
                        "completed": sum(
                            item.get("hardlink_status") in {"done", "disabled", None} for item in results
                        ),
                        "results": results,
                    }
                jobs = [job]
            else:
                jobs = [
                    item
                    for item in self.repository.list()
                    if item["status"] in {"submitting", "awaiting_binding", "pending", "waiting_metadata", "waiting_download", "retrying"}
                ]
            results = [self._process_job(item) for item in jobs]
            processed_ids = {item["id"] for item in results}
            hardlink_jobs = []
            if not job_id:
                hardlink_jobs = [
                    item
                    for item in self.repository.list()
                    if item["status"] == "completed"
                    and item["id"] not in processed_ids
                    and item.get("hardlink_status") in {"pending", "waiting_download", "retrying"}
                ]
            for item in hardlink_jobs:
                results.append(self._finish_hardlink(item))
            return {
                "running": False,
                "checked": len(results),
                "completed": sum(
                    item["status"] == "completed"
                    and item.get("hardlink_status") in {"done", "disabled", None}
                    for item in results
                ),
                "results": results,
            }
        finally:
            self._lock.release()
