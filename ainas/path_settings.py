"""Persist editable download and media-library paths for the admin console."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Mapping

from pydantic import ValidationError

from .config import Settings
from .errors import ValidationAppError


PATH_FIELDS = (
    "downloads_base_path",
    "download_movie_path",
    "download_tv_path",
    "download_anime_path",
    "medialib_movie_path",
    "medialib_tv_path",
    "medialib_anime_path",
)
EDITABLE_FIELDS = (*PATH_FIELDS, "medialib_hardlink_enabled")


class PathSettingsRepository:
    """Store path overrides and apply them to the shared Settings instance."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.path_settings_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    @staticmethod
    def _normalized_path(value: Any, field_name: str) -> Path:
        text = str(value).strip()
        if not text:
            raise ValidationAppError(
                "目录不能为空",
                [{"field": field_name, "message": "请输入绝对路径", "code": "EMPTY_PATH"}],
            )
        path = Path(os.path.normpath(text))
        if not path.is_absolute():
            raise ValidationAppError(
                "目录必须使用绝对路径",
                [{"field": field_name, "message": "请输入以 / 开头的 NAS 绝对路径", "code": "ABSOLUTE_PATH_REQUIRED"}],
            )
        return path

    def _validated(self, changes: Mapping[str, Any]) -> Dict[str, Any]:
        unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
        if unknown:
            raise ValidationAppError(
                "包含不支持的路径设置",
                [{"field": name, "message": "此设置不可在后台修改", "code": "UNKNOWN_PATH_SETTING"} for name in unknown],
            )

        normalized: Dict[str, Any] = {}
        for name, value in changes.items():
            normalized[name] = self._normalized_path(value, name) if name in PATH_FIELDS else value

        base = self._normalized_path(self.settings.medialib_base_path, "medialib_base_path")
        complete = {name: normalized.get(name, getattr(self.settings, name)) for name in PATH_FIELDS}
        errors = []
        for name, value in complete.items():
            path = self._normalized_path(value, name)
            complete[name] = path
            try:
                path.relative_to(base)
            except ValueError:
                errors.append(
                    {
                        "field": name,
                        "message": f"目录必须位于 Docker 根挂载 {base} 内",
                        "code": "PATH_OUTSIDE_MEDIA_ROOT",
                    }
                )
        if errors:
            raise ValidationAppError("目录超出了 Docker 可访问范围", errors)

        candidate_values = self.settings.model_dump()
        candidate_values.update(complete)
        if "medialib_hardlink_enabled" in normalized:
            candidate_values["medialib_hardlink_enabled"] = normalized["medialib_hardlink_enabled"]
        try:
            candidate = Settings.model_validate(candidate_values)
        except ValidationError as exc:
            errors = [
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                    "code": item["type"],
                }
                for item in exc.errors()
            ]
            raise ValidationAppError("路径设置校验失败", errors) from exc

        normalized.update({name: complete[name] for name in changes if name in PATH_FIELDS})
        if "medialib_hardlink_enabled" in changes:
            normalized["medialib_hardlink_enabled"] = candidate.medialib_hardlink_enabled
        return normalized

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationAppError(f"保存的路径设置无法读取：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationAppError("保存的路径设置格式无效")
        return self._validated(value)

    def _write(self, value: Mapping[str, Any]) -> None:
        payload = {
            name: str(value[name]) if name in PATH_FIELDS else value[name]
            for name in EDITABLE_FIELDS
            if name in value
        }
        fd, temp_name = tempfile.mkstemp(prefix="paths-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _apply(self, changes: Mapping[str, Any]) -> None:
        for name, value in changes.items():
            setattr(self.settings, name, value)

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self.path.exists():
                self._apply(self._read())
            return self.get()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            result: Dict[str, Any] = {
                name: str(getattr(self.settings, name)) if name in PATH_FIELDS else getattr(self.settings, name)
                for name in EDITABLE_FIELDS
            }
            result["medialib_base_path"] = str(self.settings.medialib_base_path)
            result["medialib_mount_path"] = str(self.settings.medialib_mount_path)
            return result

    def update(self, changes: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            validated = self._validated(changes)
            persisted = {
                name: validated.get(name, getattr(self.settings, name))
                for name in EDITABLE_FIELDS
            }
            self._write(persisted)
            self._apply(validated)
            return self.get()
