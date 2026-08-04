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
from .errors import AppError, NotFoundError
from .qbittorrent import QBittorrentClient, torrent_hash
from .quality import EPISODIC_MEDIA_TYPES, Release, canonical_media_name, detect_episode, detect_season
from .watchlist import utc_now_iso


logger = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".wmv", ".flv", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
INVALID_FILENAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


def safe_name(value: str, max_length: int = 180) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = INVALID_FILENAME_RE.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    return (normalized[:max_length].rstrip(" .") or "未命名媒体")


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
        media_name = safe_name(release.media_name or canonical_media_name(release.title))
        year = release.year
        if release.media_type == "custom":
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
            rename_enabled=release.media_type != "custom",
        )

    @staticmethod
    def _new_path(old_path: str, basename: str) -> str:
        parent = PurePosixPath(old_path).parent
        return str(parent / basename) if str(parent) != "." else basename

    @staticmethod
    def _unique_path(path: str, used: set[str]) -> str:
        if path not in used:
            used.add(path)
            return path
        value = PurePosixPath(path)
        counter = 2
        while True:
            candidate = str(value.with_name(f"{value.stem} - {counter}{value.suffix}"))
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
            value += f"-{selected_part}"
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
            value += f" - {plan.edition}"
        video_format = cls._file_video_format(path, plan.video_format)
        if video_format:
            value += f" - {video_format}"
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
            }
        videos = [item for item in values if PurePosixPath(item["name"]).suffix.lower() in VIDEO_EXTENSIONS]
        subtitles = [item for item in values if PurePosixPath(item["name"]).suffix.lower() in SUBTITLE_EXTENSIONS]
        videos.sort(key=lambda item: (-int(item.get("size") or 0), item["name"].casefold()))
        operations: List[Dict[str, str]] = []
        video_map: List[Tuple[str, str]] = []
        used_paths: set[str] = set()

        main_videos = [
            item
            for item in videos
            if not re.search(r"(?i)(?:^|[ ._\-])(?:sample|trailer)(?:$|[ ._\-])|样片|预告", PurePosixPath(item["name"]).stem)
        ]
        for index, item in enumerate(videos):
            old_path = item["name"]
            old_value = PurePosixPath(old_path)
            if plan.media_type in EPISODIC_MEDIA_TYPES:
                detected = detect_episode(old_value.name) or (plan.episode if len(videos) == 1 else None)
                season = detect_season(old_value.name) or plan.season
                episode = _episode_with_default_season(detected, season)
                if not episode:
                    continue
                # Episodic files are separate episodes, not automatically numbered movie parts.
                part = self._file_part(old_value, index, len(videos), auto_number=False)
                value = self._title_prefix(plan, part)
                value += f" - {episode}"
                if plan.episode_title:
                    value += f" - {plan.episode_title}"
                video_format = self._file_video_format(old_value, plan.video_format)
                if video_format:
                    value += f" - {video_format}"
                new_basename = f"{value}{old_value.suffix.lower()}"
            else:
                main_index = main_videos.index(item) if item in main_videos else index
                new_basename = self._movie_basename(item, plan, main_index, len(main_videos))
            new_path = self._unique_path(self._new_path(old_path, safe_name(new_basename)), used_paths)
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
        }


class NamingJobRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"items": []})

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError("invalid naming jobs shape")
            return value
        except (OSError, ValueError, json.JSONDecodeError):
            backup = self.path.with_suffix(f".corrupt-{uuid.uuid4().hex[:8]}.json")
            if self.path.exists():
                self.path.replace(backup)
            value = {"items": []}
            self._write(value)
            return value

    def _write(self, value: Dict[str, Any]) -> None:
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

    def upsert(self, torrent_hash_value: str, plan: NamingPlan, final_category: str) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item["torrent_hash"] == torrent_hash_value:
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

    def _download_path(self, plan: NamingPlan, final_category: str) -> Path:
        """Keep each release isolated below its configured media-type download root."""
        roots = {
            "movie": self.settings.download_movie_path,
            "tv": self.settings.download_tv_path,
            "anime": self.settings.download_anime_path,
            "custom": self.settings.download_custom_path,
        }
        root = roots.get(plan.media_type, self.settings.download_path_for_category(final_category))
        return Path(root) / safe_name(plan.root_name)

    def add_download(self, release: Release, final_category: str) -> Dict[str, Any]:
        plan = self.planner.from_release(release)
        if self.path_rules:
            source = self.settings.download_path_for_category(final_category)
            rule = self.path_rules.match(release.media_type, source)
            if rule:
                plan = replace(plan, rename_enabled=bool(rule["rename_enabled"]) and release.media_type != "custom")
        hash_value = torrent_hash(release.download_link)
        use_staging = self.settings.naming_enabled and plan.rename_enabled and bool(hash_value)
        track_only = not plan.rename_enabled and self.settings.medialib_hardlink_enabled and bool(hash_value)
        current_category = self.settings.qb_naming_category if use_staging else final_category
        result = self.qb.add_download(
            release.download_link,
            current_category,
            rename=plan.root_name if self.settings.naming_enabled and plan.rename_enabled else None,
            save_path=self._download_path(plan, final_category),
        )
        job = None
        if (use_staging or track_only) and hash_value:
            job = self.repository.upsert(hash_value, plan, final_category)
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
    ) -> Dict[str, Any]:
        plan = self.planner.from_release(release)
        if self.path_rules:
            source = self.settings.download_path_for_category(final_category)
            rule = self.path_rules.match(release.media_type, source)
            if rule:
                plan = replace(plan, rename_enabled=bool(rule["rename_enabled"]) and release.media_type != "custom")
        use_staging = self.settings.naming_enabled and plan.rename_enabled
        track_only = not plan.rename_enabled and self.settings.medialib_hardlink_enabled
        current_category = self.settings.qb_naming_category if use_staging else final_category
        result = self.qb.add_torrent_file(
            content,
            filename,
            current_category,
            rename=plan.root_name if use_staging else None,
            save_path=self._download_path(plan, final_category),
        )
        job = None
        if use_staging or track_only:
            job = self.repository.upsert(torrent_hash_value, plan, final_category)
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
            self.qb.set_category(job["torrent_hash"], job["final_category"])
            self.qb.resume(job["torrent_hash"])
        except AppError:
            logger.exception("naming_job_release_failed", extra={"job_id": job["id"]})

    def _process_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now_iso()
        checks = int(job.get("checks", 0)) + 1
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
        progress = float(torrent.get("progress", 0) or 0)
        selected = [item for item in files if int(item.get("priority", 1) or 0) > 0]
        files_complete = all(float(item.get("progress", progress) or 0) >= 0.999999 for item in selected)
        if progress < 0.999999 or not files_complete:
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
        preview = self.planner.plan_files(files, plan)
        try:
            verifier = getattr(self.hardlinker, "verify_named_sources", None)
            already_named = False
            if callable(verifier):
                try:
                    verifier(torrent, files, preview)
                    already_named = True
                except AppError:
                    pass
            if plan.rename_enabled:
                if not already_named:
                    for operation in preview["operations"]:
                        self.qb.rename_file(job["torrent_hash"], operation["old_path"], operation["new_path"])
                    for operation in preview["folder_operations"]:
                        self.qb.rename_folder(job["torrent_hash"], operation["old_path"], operation["new_path"])
                self.qb.rename_torrent(job["torrent_hash"], plan.root_name)
            if callable(verifier):
                verifier(torrent, files, preview)
            self.qb.set_category(job["torrent_hash"], job["final_category"])
            self.qb.resume(job["torrent_hash"])
            hardlink_enabled = bool(self.hardlinker and self.hardlinker.enabled)
            updated = self.repository.update(
                job["id"],
                {
                    "status": "completed",
                    "checks": checks,
                    "last_check": now,
                    "last_error": None,
                    "result": preview,
                    "hardlink_status": "waiting_download" if hardlink_enabled else "disabled",
                    "hardlink_attempts": 0,
                    "hardlink_error": None,
                    "hardlink_result": None,
                },
            )
            if hardlink_enabled and float(torrent.get("progress", 0)) >= 0.999999:
                return self._finish_hardlink(updated)
            return updated
        except AppError as exc:
            attempts = int(job.get("attempts", 0)) + 1
            failed = attempts >= self.settings.naming_max_attempts
            if failed:
                self._release_staging_job(job)
            return self.repository.update(
                job["id"],
                {
                    "status": "failed" if failed else "retrying",
                    "attempts": attempts,
                    "checks": checks,
                    "last_check": now,
                    "last_error": exc.detail,
                    "result": preview,
                },
            )

    def _finish_hardlink(self, job: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now_iso()
        if not self.hardlinker or not self.hardlinker.enabled:
            return self.repository.update(
                job["id"],
                {"hardlink_status": "disabled", "hardlink_error": None, "last_check": now},
            )
        try:
            torrent = self.qb.torrent_info(job["torrent_hash"])
            progress = float(torrent.get("progress", 0)) if torrent else 0
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
            result = self.hardlinker.link_completed(
                torrent,
                files,
                NamingPlan(**job["plan"]),
                job.get("result") or {},
            )
            return self.repository.update(
                job["id"],
                {
                    "hardlink_status": "done",
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
                if job["status"] == "failed":
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
                    if item["status"] in {"pending", "waiting_metadata", "waiting_download", "retrying"}
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
