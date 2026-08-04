"""Hardlink completed qBittorrent downloads into the Emby media library."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

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

    def __init__(self, settings: Settings, path_rules: Optional[Any] = None) -> None:
        self.settings = settings
        self.path_rules = path_rules

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
        if self.path_rules:
            value = self.path_rules.host_path_for_downloader(value)

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

    def _target_root(self, media_type: str, source_path: Any = None) -> Path:
        if media_type not in {"movie", "tv", "anime", "custom"}:
            raise HardlinkError(f"unsupported media type: {media_type!r}")
        host_source = (
            self.path_rules.host_path_for_downloader(source_path)
            if self.path_rules and source_path
            else source_path
        )
        rule = self.path_rules.match(media_type, host_source) if self.path_rules and host_source else None
        if rule:
            host_target = Path(rule["target_path"])
        elif media_type == "movie":
            host_target = self.settings.medialib_movie_path
        elif media_type == "anime":
            host_target = self.settings.medialib_anime_path
        elif media_type == "custom":
            host_target = self.settings.medialib_custom_path
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
        for base in (save_path, content_path):
            if base is None:
                continue
            candidates.append(base / relative)
            if len(relative.parts) > 1 and relative.parts[0].casefold() == base.name.casefold():
                candidates.append(base.joinpath(*relative.parts[1:]))
        if content_path is not None:
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

    def _resolve_source_variants(
        self,
        relatives: Sequence[Path],
        save_path: Path | None,
        content_path: Path | None,
        file_count: int,
    ) -> Path:
        candidates: List[Path] = []
        for relative in relatives:
            candidates.extend(self._source_candidates(relative, save_path, content_path, file_count))
        candidates = list(dict.fromkeys(candidates))
        source = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source is None:
            expected = relatives[0] if relatives else Path("unknown")
            rendered = ", ".join(str(candidate) for candidate in candidates) or "no usable qBittorrent path"
            raise HardlinkError(f"downloaded file is missing: {expected} (checked: {rendered})")
        if source.is_symlink():
            raise HardlinkError(f"refusing to hardlink a symbolic link: {source}")
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(self.mount_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise HardlinkError(f"source file escapes the /medialib mount: {source}") from exc
        return resolved

    def _folder_operations(self, naming_result: Mapping[str, Any] | None) -> List[tuple[Path, Path]]:
        values: List[tuple[Path, Path]] = []
        for operation in (naming_result or {}).get("folder_operations") or []:
            if not isinstance(operation, Mapping):
                continue
            values.append(
                (self._safe_relative(operation.get("old_path")), self._safe_relative(operation.get("new_path")))
            )
        values.sort(key=lambda item: len(item[0].parts), reverse=True)
        return values

    @staticmethod
    def _apply_folder_operations(path: Path, operations: Sequence[tuple[Path, Path]]) -> Path:
        value = path
        for old_path, new_path in operations:
            if value.parts[: len(old_path.parts)] == old_path.parts:
                value = new_path.joinpath(*value.parts[len(old_path.parts) :])
        return value

    def _renamed_relative(self, relative: Path, naming_result: Mapping[str, Any] | None) -> Path:
        """Resolve stale downloader paths to the paths produced by the naming job."""
        if not naming_result:
            return relative

        folder_operations = self._folder_operations(naming_result)

        def apply_folders(path: Path) -> Path:
            return self._apply_folder_operations(path, folder_operations)

        aliases: Dict[Path, Path] = {}
        for operation in naming_result.get("operations") or []:
            if not isinstance(operation, Mapping):
                continue
            old_path = self._safe_relative(operation.get("old_path"))
            new_path = self._safe_relative(operation.get("new_path"))
            final_path = apply_folders(new_path)
            aliases[old_path] = final_path
            aliases[apply_folders(old_path)] = final_path
            aliases[new_path] = final_path

        return aliases.get(relative, apply_folders(relative))

    def _renamed_relatives(
        self,
        relative: Path,
        naming_result: Mapping[str, Any] | None,
    ) -> List[Path]:
        final = self._renamed_relative(relative, naming_result)
        variants = [final]
        if naming_result:
            folder_operations = self._folder_operations(naming_result)
            for operation in naming_result.get("operations") or []:
                if not isinstance(operation, Mapping):
                    continue
                old_path = self._safe_relative(operation.get("old_path"))
                new_path = self._safe_relative(operation.get("new_path"))
                if relative in {old_path, new_path, self._apply_folder_operations(old_path, folder_operations)}:
                    variants.extend(
                        [
                            self._apply_folder_operations(old_path, folder_operations),
                            new_path,
                            old_path,
                        ]
                    )
        variants.append(relative)
        return list(dict.fromkeys(variants))

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
        target_root: Optional[Path] = None,
    ) -> Path:
        media_type = str(plan.get("media_type") or "")
        if media_type not in {"movie", "tv", "anime", "custom"}:
            raise HardlinkError(f"unsupported media type: {media_type!r}")
        if media_type == "custom":
            root_name = self._safe_component(plan.get("root_name"), "custom resource link name")
            if content_root_name and relative.parts[0].casefold() == content_root_name.casefold():
                relative = Path(*relative.parts[1:]) if len(relative.parts) > 1 else Path(relative.name)
            return (target_root or self._target_root(media_type)) / root_name / relative
        root_name = self._safe_component(plan.get("root_name"), "media root name")
        media_name = self._safe_component(plan.get("media_name") or root_name, "media name")
        removable_roots = [root_name, media_name]
        if content_root_name and not (
            media_type in {"tv", "anime"} and SEASON_DIRECTORY_RE.fullmatch(content_root_name)
        ):
            removable_roots.append(content_root_name)
        relative = self._strip_media_root(relative, removable_roots)

        if media_type == "movie":
            return (target_root or self._target_root(media_type)) / root_name / relative
        return (target_root or self._target_root(media_type)) / root_name / self._tv_relative(relative)

    def verify_named_sources(
        self,
        torrent: Mapping[str, Any],
        files: Iterable[Mapping[str, Any]],
        naming_result: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        """Confirm that every selected file exists at its planned post-naming path."""
        values = [dict(item) for item in files if item.get("name")]
        selected = [
            item
            for item in values
            if int(item.get("priority", 1) or 0) > 0 and not self._is_padding(self._safe_relative(item["name"]))
        ]
        if not selected:
            raise HardlinkError("qBittorrent returned no downloaded files to verify")

        save_path = (
            self._container_path(torrent["save_path"], "qBittorrent save_path")
            if torrent.get("save_path")
            else None
        )
        content_path = (
            self._container_path(torrent["content_path"], "qBittorrent content_path")
            if torrent.get("content_path")
            else None
        )
        if save_path is None and content_path is None:
            raise HardlinkError("qBittorrent did not provide save_path or content_path")

        verified = []
        for item in selected:
            original = self._safe_relative(item["name"])
            expected = self._renamed_relatives(original, naming_result)[0]
            source = self._resolve_source_variants([expected], save_path, content_path, len(selected))
            self._verify_source_size(source, item)
            verified.append({"relative": str(expected), "source": str(source)})
        return {"status": "ready", "count": len(verified), "files": verified}

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

    @staticmethod
    def _verify_source_size(source: Path, item: Mapping[str, Any]) -> None:
        expected = int(item.get("size") or 0)
        actual = source.stat().st_size
        if expected and actual != expected:
            raise HardlinkError(
                f"downloaded file size does not match qBittorrent metadata: {source} ({actual} != {expected})"
            )

    def link_completed(
        self,
        torrent: Mapping[str, Any],
        files: Iterable[Mapping[str, Any]],
        plan: Mapping[str, Any] | Any,
        naming_result: Mapping[str, Any] | None = None,
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
        source_rule_path = torrent.get("content_path") or torrent.get("save_path")
        target_root = self._target_root(str(plan_value.get("media_type") or ""), source_rule_path)
        content_root_name = content_path.name if content_path is not None and content_path.is_dir() else None
        for item in selected:
            original_relative = self._safe_relative(item["name"])
            relatives = self._renamed_relatives(original_relative, naming_result)
            relative = relatives[0]
            source = self._resolve_source_variants(relatives, save_path, content_path, len(selected))
            self._verify_source_size(source, item)
            destination = self._destination(relative, plan_value, content_root_name, target_root)
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
            "target": str(self._destination(Path("placeholder"), plan_value, content_root_name, target_root).parent),
            "files": results,
        }


# Compatibility for deployments that imported the earlier class name.
MediaLibHardlinker = MediaLibraryService
