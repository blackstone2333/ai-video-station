"""Hardlink completed qBittorrent downloads into the Emby media library."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .config import Settings
from .errors import AppError


logger = logging.getLogger(__name__)
EPISODE_SEASON_RE = re.compile(r"(?i)(?:^|[ ._\-])S(\d{1,2})E\d{1,3}(?:$|[ ._\-])")
SEASON_DIRECTORY_RE = re.compile(r"(?i)^(?:Season[ ._\-]*0*\d{1,2}|S0*\d{1,2}|第0*\d{1,2}季)$")
PADDING_PREFIXES = ("_____padding_file_",)


class HardlinkError(AppError):
    """An operational hardlink failure that can safely be retried."""

    def __init__(self, detail: str) -> None:
        super().__init__("Media Library Hardlink Error", detail, "hardlink-error", 500)


class MediaLibraryService:
    """Create idempotent hardlinks using the single ``/medialib`` mount."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.medialib_hardlink_enabled

    @property
    def mount_root(self) -> Path:
        return Path(os.path.normpath(str(self.settings.medialib_mount_path)))

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _container_path(self, raw_path: Any, label: str) -> Path:
        """Translate a NAS host path returned by qBittorrent to the container mount."""
        if raw_path is None or not str(raw_path).strip():
            raise HardlinkError(f"qBittorrent did not provide {label}")
        value = Path(os.path.normpath(str(raw_path)))
        if not value.is_absolute():
            raise HardlinkError(f"{label} must be an absolute path: {raw_path}")

        host_root = Path(os.path.normpath(str(self.settings.medialib_base_path)))
        try:
            return self.mount_root / value.relative_to(host_root)
        except ValueError:
            pass
        if self._path_within(value, self.mount_root):
            return value
        raise HardlinkError(
            f"{label} is outside MEDIALIB_BASE_PATH and cannot be reached through /medialib: {raw_path}"
        )

    @staticmethod
    def _safe_component(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
            raise HardlinkError(f"unsafe {label}: {value!r}")
        return text

    @staticmethod
    def _safe_relative(value: Any) -> Path:
        text = str(value or "")
        if not text or "\\" in text or "\x00" in text:
            raise HardlinkError(f"unsafe torrent file path: {value!r}")
        posix = PurePosixPath(text)
        if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
            raise HardlinkError(f"unsafe torrent file path: {value!r}")
        return Path(*posix.parts)

    @staticmethod
    def _is_padding(path: Path) -> bool:
        lowered = tuple(part.casefold() for part in path.parts)
        return (
            ".pad" in lowered
            or path.suffix.casefold() == ".pad"
            or path.name.casefold().startswith(PADDING_PREFIXES)
        )

    def _target_root(self, media_type: str) -> Path:
        if media_type not in {"movie", "tv", "anime"}:
            raise HardlinkError(f"unsupported media type: {media_type!r}")
        if media_type == "movie":
            host_target = self.settings.medialib_movie_path
        elif media_type == "anime":
            host_target = self.settings.medialib_anime_path
        else:
            host_target = self.settings.medialib_tv_path
        return self._container_path(host_target, f"{media_type} media library path")

    def _source_candidates(
        self,
        relative: Path,
        save_path: Path | None,
        content_path: Path | None,
        file_count: int,
    ) -> List[Path]:
        candidates: List[Path] = []
        if content_path is not None and file_count == 1 and content_path.is_file():
            candidates.append(content_path)
        if save_path is not None:
            candidates.append(save_path / relative)
        if content_path is not None:
            candidates.append(content_path / relative)
            if relative.parts and relative.parts[0] == content_path.name:
                candidates.append(content_path.parent / relative)
        return list(dict.fromkeys(candidates))

    def _resolve_source(
        self,
        relative: Path,
        save_path: Path | None,
        content_path: Path | None,
        file_count: int,
    ) -> Path:
        candidates = self._source_candidates(relative, save_path, content_path, file_count)
        source = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source is None:
            rendered = ", ".join(str(candidate) for candidate in candidates) or "no usable qBittorrent path"
            raise HardlinkError(f"downloaded file is missing: {relative} (checked: {rendered})")
        if source.is_symlink():
            raise HardlinkError(f"refusing to hardlink a symbolic link: {source}")
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(self.mount_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise HardlinkError(f"source file escapes the /medialib mount: {source}") from exc
        return resolved

    @staticmethod
    def _strip_media_root(relative: Path, root_names: Sequence[str]) -> Path:
        if len(relative.parts) < 2:
            return relative
        names = {name.casefold() for name in root_names if name}
        if relative.parts[0].casefold() in names:
            return Path(*relative.parts[1:])
        return relative

    @staticmethod
    def _tv_relative(relative: Path) -> Path:
        if len(relative.parts) > 1:
            return relative
        match = EPISODE_SEASON_RE.search(relative.name)
        if not match:
            return relative
        return Path(f"Season {int(match.group(1)):02d}") / relative

    def _destination(
        self,
        relative: Path,
        plan: Mapping[str, Any],
        content_root_name: str | None = None,
    ) -> Path:
        media_type = str(plan.get("media_type") or "")
        if media_type not in {"movie", "tv", "anime"}:
            raise HardlinkError(f"unsupported media type: {media_type!r}")
        root_name = self._safe_component(plan.get("root_name"), "media root name")
        media_name = self._safe_component(plan.get("media_name") or root_name, "media name")
        removable_roots = [root_name, media_name]
        if content_root_name and not (
            media_type in {"tv", "anime"} and SEASON_DIRECTORY_RE.fullmatch(content_root_name)
        ):
            removable_roots.append(content_root_name)
        relative = self._strip_media_root(relative, removable_roots)

        if media_type == "movie":
            return self._target_root(media_type) / root_name / relative
        return self._target_root(media_type) / media_name / self._tv_relative(relative)

    def _preflight_target(self, source: Path, destination: Path, target_root: Path) -> None:
        mount = self.mount_root.resolve(strict=True)
        try:
            destination.relative_to(target_root)
        except ValueError as exc:
            raise HardlinkError(f"target path escapes the configured media library: {destination}") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.parent.resolve(strict=True).relative_to(mount)
        except (OSError, ValueError) as exc:
            raise HardlinkError(f"target path escapes the /medialib mount: {destination.parent}") from exc
        if source.stat().st_dev != destination.parent.stat().st_dev:
            raise HardlinkError(
                f"source and target are on different filesystems; mount MEDIALIB_BASE_PATH only once: "
                f"{source} -> {destination}"
            )
        if not os.access(destination.parent, os.W_OK | os.X_OK):
            raise HardlinkError(f"target directory is not writable: {destination.parent}")

    @staticmethod
    def _result(status: str, source: Path, destination: Path) -> Dict[str, str]:
        return {"status": status, "source": str(source), "target": str(destination)}

    def link_completed(
        self,
        torrent: Mapping[str, Any],
        files: Iterable[Mapping[str, Any]],
        plan: Mapping[str, Any] | Any,
    ) -> Dict[str, Any]:
        """Hardlink every downloaded torrent file after naming has completed."""
        if not self.enabled:
            return {"status": "disabled", "linked": 0, "skipped": 0, "already_linked": 0, "files": []}

        plan_value: Mapping[str, Any]
        if isinstance(plan, Mapping):
            plan_value = plan
        elif hasattr(plan, "to_dict"):
            plan_value = plan.to_dict()
        else:
            raise HardlinkError("invalid naming plan")

        values = [dict(item) for item in files if item.get("name")]
        selected = [
            item
            for item in values
            if int(item.get("priority", 1) or 0) > 0 and not self._is_padding(self._safe_relative(item["name"]))
        ]
        if not selected:
            raise HardlinkError("qBittorrent returned no downloaded files to hardlink")

        save_path = None
        if torrent.get("save_path"):
            save_path = self._container_path(torrent["save_path"], "qBittorrent save_path")
        content_path = None
        if torrent.get("content_path"):
            content_path = self._container_path(torrent["content_path"], "qBittorrent content_path")
        if save_path is None and content_path is None:
            raise HardlinkError("qBittorrent did not provide save_path or content_path")

        prepared: List[tuple[Path, Path]] = []
        target_root = self._target_root(str(plan_value.get("media_type") or ""))
        content_root_name = content_path.name if content_path is not None and content_path.is_dir() else None
        for item in selected:
            relative = self._safe_relative(item["name"])
            source = self._resolve_source(relative, save_path, content_path, len(selected))
            destination = self._destination(relative, plan_value, content_root_name)
            self._preflight_target(source, destination, target_root)
            prepared.append((source, destination))

        results: List[Dict[str, str]] = []
        linked = skipped = already_linked = 0
        for source, destination in prepared:
            if os.path.lexists(destination):
                skipped += 1
                if destination.is_file() and not destination.is_symlink() and os.path.samefile(source, destination):
                    already_linked += 1
                    status = "already-linked"
                else:
                    status = "skipped-existing"
                results.append(self._result(status, source, destination))
                continue
            try:
                os.link(source, destination)
            except OSError as exc:
                raise HardlinkError(f"could not hardlink {source} to {destination}: {exc}") from exc
            linked += 1
            results.append(self._result("linked", source, destination))
            logger.info("medialib_hardlink_created", extra={"source": str(source), "target": str(destination)})

        return {
            "status": "done",
            "linked": linked,
            "skipped": skipped,
            "already_linked": already_linked,
            "target": str(self._destination(Path("placeholder"), plan_value, content_root_name).parent),
            "files": results,
        }


# Compatibility for deployments that imported the earlier class name.
MediaLibHardlinker = MediaLibraryService
