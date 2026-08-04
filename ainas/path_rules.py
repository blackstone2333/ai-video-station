"""Multiple source-to-target hardlink mappings grouped by media type."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .errors import NotFoundError, ValidationAppError
from .watchlist import utc_now_iso


MediaRuleType = Literal["movie", "tv", "anime", "custom"]


class PathRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: MediaRuleType
    name: str = Field(min_length=1, max_length=100)
    source_path: Path
    downloader_path: Optional[Path] = None
    target_path: Path
    enabled: bool = True
    rename_enabled: bool = True
    default_download: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class PathRuleRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.path_rules_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"items": self._default_rules()})
        self._sync_primary_paths(self._read()["items"])

    def _default_rules(self) -> List[Dict[str, Any]]:
        values = []
        definitions = (
            ("movie", self.settings.qb_movie_category, self.settings.download_movie_path, self.settings.medialib_movie_path),
            ("tv", self.settings.qb_tv_category, self.settings.download_tv_path, self.settings.medialib_tv_path),
            ("anime", self.settings.qb_anime_category, self.settings.download_anime_path, self.settings.medialib_anime_path),
            ("custom", self.settings.qb_custom_category, self.settings.download_custom_path, self.settings.medialib_custom_path),
        )
        now = utc_now_iso()
        for media_type, name, source, target in definitions:
            values.append(
                {
                    "id": f"default-{media_type}",
                    "media_type": media_type,
                    "name": name,
                    "source_path": str(source),
                    "downloader_path": str(source),
                    "target_path": str(target),
                    "enabled": True,
                    "rename_enabled": media_type != "custom",
                    "default_download": True,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        return values

    @staticmethod
    def _normalized(value: Any, field: str) -> Path:
        path = Path(os.path.normpath(str(value).strip()))
        if not path.is_absolute():
            raise ValidationAppError(
                "目录映射必须使用绝对路径",
                [{"field": field, "message": "请输入以 / 开头的绝对路径", "code": "ABSOLUTE_PATH_REQUIRED"}],
            )
        return path

    def _validate(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        allowed = set(PathRuleInput.model_fields)
        metadata = {"id", "created_at", "updated_at"}
        unknown = sorted(set(value) - allowed - metadata)
        if unknown:
            raise ValidationAppError(
                "目录映射包含不支持的设置",
                [{"field": name, "message": "此字段不可修改", "code": "UNKNOWN_PATH_RULE_FIELD"} for name in unknown],
            )
        try:
            parsed = PathRuleInput.model_validate({name: value[name] for name in allowed if name in value})
        except ValueError as exc:
            raise ValidationAppError(f"目录映射配置无效：{exc}") from exc
        base = self._normalized(self.settings.medialib_base_path, "medialib_base_path")
        source = self._normalized(parsed.source_path, "source_path")
        downloader = self._normalized(parsed.downloader_path or source, "downloader_path")
        target = self._normalized(parsed.target_path, "target_path")
        errors = []
        for field, path in (("source_path", source), ("target_path", target)):
            try:
                path.relative_to(base)
            except ValueError:
                errors.append(
                    {
                        "field": field,
                        "message": f"目录必须位于 Docker 根挂载 {base} 内",
                        "code": "PATH_OUTSIDE_MEDIA_ROOT",
                    }
                )
        if errors:
            raise ValidationAppError("目录映射超出了 Docker 可访问范围", errors)
        return {
            **parsed.model_dump(),
            "source_path": str(source),
            "downloader_path": str(downloader),
            "target_path": str(target),
        }

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationAppError(f"保存的目录映射无法读取：{exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise ValidationAppError("保存的目录映射格式无效")
        items = []
        for raw in value["items"]:
            if not isinstance(raw, dict):
                raise ValidationAppError("保存的目录映射格式无效")
            validated = self._validate(raw)
            items.append(
                {
                    **validated,
                    "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
                    "created_at": str(raw.get("created_at") or utc_now_iso()),
                    "updated_at": str(raw.get("updated_at") or utc_now_iso()),
                }
            )
        return {"items": items}

    def _write(self, value: Mapping[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix="path-rules-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(value), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _primary(items: List[Dict[str, Any]], media_type: str) -> Optional[Dict[str, Any]]:
        enabled = [item for item in items if item["media_type"] == media_type and item["enabled"]]
        return next((item for item in enabled if item["default_download"]), enabled[0] if enabled else None)

    def _sync_primary_paths(self, items: List[Dict[str, Any]]) -> None:
        fields = {
            "movie": ("download_movie_path", "medialib_movie_path"),
            "tv": ("download_tv_path", "medialib_tv_path"),
            "anime": ("download_anime_path", "medialib_anime_path"),
            "custom": ("download_custom_path", "medialib_custom_path"),
        }
        for media_type, (source_field, target_field) in fields.items():
            primary = self._primary(items, media_type)
            if primary:
                setattr(self.settings, source_field, Path(primary["source_path"]))
                setattr(self.settings, target_field, Path(primary["target_path"]))

    def list(self, media_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = self._read()["items"]
            if media_type:
                items = [item for item in items if item["media_type"] == media_type]
            return deepcopy(items)

    def get(self, rule_id: str) -> Dict[str, Any]:
        with self._lock:
            for item in self._read()["items"]:
                if item["id"] == rule_id:
                    return deepcopy(item)
        raise NotFoundError("path rule", rule_id)

    @staticmethod
    def _set_single_default(items: List[Dict[str, Any]], rule: Dict[str, Any]) -> None:
        if not rule["default_download"]:
            return
        for item in items:
            if item["media_type"] == rule["media_type"] and item["id"] != rule["id"]:
                item["default_download"] = False

    def add(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            validated = self._validate(value)
            now = utc_now_iso()
            item = {**validated, "id": uuid.uuid4().hex[:12], "created_at": now, "updated_at": now}
            same_type = [entry for entry in data["items"] if entry["media_type"] == item["media_type"]]
            if not same_type:
                item["default_download"] = True
            self._set_single_default(data["items"], item)
            data["items"].append(item)
            self._write(data)
            self._sync_primary_paths(data["items"])
            return deepcopy(item)

    def update(self, rule_id: str, changes: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["items"]):
                if item["id"] != rule_id:
                    continue
                validated = self._validate({**item, **changes})
                updated = {**item, **validated, "id": rule_id, "updated_at": utc_now_iso()}
                data["items"][index] = updated
                self._set_single_default(data["items"], updated)
                self._write(data)
                self._sync_primary_paths(data["items"])
                return deepcopy(updated)
        raise NotFoundError("path rule", rule_id)

    def delete(self, rule_id: str) -> None:
        with self._lock:
            data = self._read()
            original = len(data["items"])
            data["items"] = [item for item in data["items"] if item["id"] != rule_id]
            if len(data["items"]) == original:
                raise NotFoundError("path rule", rule_id)
            for media_type in ("movie", "tv", "anime", "custom"):
                group = [item for item in data["items"] if item["media_type"] == media_type and item["enabled"]]
                if group and not any(item["default_download"] for item in group):
                    group[0]["default_download"] = True
            self._write(data)
            self._sync_primary_paths(data["items"])

    def match(self, media_type: str, source_path: Any) -> Optional[Dict[str, Any]]:
        source = self._normalized(source_path, "source_path")
        candidates = []
        for item in self.list(media_type):
            if not item["enabled"]:
                continue
            rule_source = Path(item["source_path"])
            try:
                source.relative_to(rule_source)
                candidates.append((len(rule_source.parts), item))
            except ValueError:
                continue
        if candidates:
            return deepcopy(max(candidates, key=lambda pair: pair[0])[1])
        return deepcopy(self._primary(self.list(), media_type))
