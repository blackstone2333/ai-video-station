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
from .media_policies import MEDIA_POLICIES, UnsupportedMediaType, policy_for
from .watchlist import utc_now_iso
from .state import StateStore, StateStoreError


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
    def __init__(self, settings: Settings, state_store: StateStore | None = None) -> None:
        self.settings = settings
        self.state_store = state_store
        self.path = settings.path_rules_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_store:
            try: self.state_store.ensure_records("path_rules", legacy_path=self.path, default=self._default_rules())
            except StateStoreError as exc: raise ValidationAppError(f"保存的目录映射无法读取：{exc}") from exc
        elif not self.path.exists():
            self._write({"items": self._default_rules()})
        self._sync_primary_paths(self._read()["items"])

    def _default_rules(self) -> List[Dict[str, Any]]:
        values = []
        now = utc_now_iso()
        for media_type, policy in MEDIA_POLICIES.items():
            name = policy.category_for(self.settings)
            source = policy.download_path_for(self.settings)
            target = policy.medialib_path_for(self.settings)
            values.append(
                {
                    "id": f"default-{media_type}",
                    "media_type": media_type,
                    "name": name,
                    "source_path": str(source),
                    "downloader_path": str(source),
                    "target_path": str(target),
                    "enabled": True,
                    "rename_enabled": policy.rename_enabled,
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
            value = {"items": self.state_store.list_records("path_rules")} if self.state_store else json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, StateStoreError) as exc:
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
        if self.state_store:
            try: self.state_store.replace_records("path_rules", value["items"]); return
            except StateStoreError as exc: raise ValidationAppError(f"保存的目录映射无法写入：{exc}") from exc
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
        for media_type, policy in MEDIA_POLICIES.items():
            primary = self._primary(items, media_type)
            if primary:
                setattr(self.settings, policy.download_path_field, Path(primary["source_path"]))
                setattr(self.settings, policy.medialib_path_field, Path(primary["target_path"]))

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

    def resolve(self, media_type: str, rule_id: Optional[str] = None) -> Dict[str, Any]:
        """Return an enabled route suitable for a download before it is submitted.

        This is deliberately separate from ``match``: matching is for completed
        files, whereas a requested route must never silently fall back when an
        operator supplied an invalid id.
        """
        try:
            policy_for(media_type)
        except UnsupportedMediaType:
            raise ValidationAppError(
                "下载类型必须明确", [{"field": "type", "message": "请选择有效媒体类型", "code": "TYPE_REQUIRED"}]
            )
        rule = self.get(rule_id) if rule_id else self._primary(self.list(), media_type)
        if not rule:
            raise ValidationAppError(
                "此媒体类型没有可用的下载路径",
                [{"field": "path_rule_id", "message": "请先启用一个路径规则", "code": "PATH_RULE_REQUIRED"}],
            )
        if rule["media_type"] != media_type:
            raise ValidationAppError(
                "路径规则的媒体类型不匹配",
                [{"field": "path_rule_id", "message": "规则不能用于此媒体类型", "code": "PATH_RULE_TYPE_MISMATCH"}],
            )
        if not rule["enabled"]:
            raise ValidationAppError(
                "路径规则已禁用",
                [{"field": "path_rule_id", "message": "请启用规则或选择其他规则", "code": "PATH_RULE_DISABLED"}],
            )
        return deepcopy(rule)

    @staticmethod
    def _access(path: Path, mode: int) -> bool:
        return bool(path.exists() and os.access(path, mode))

    def preflight(self, rule_id: str) -> Dict[str, Any]:
        """Inspect a mapping without altering the NAS filesystem."""
        rule = self.get(rule_id)
        base = Path(self.settings.medialib_base_path)
        mount = Path(self.settings.medialib_mount_path)
        source, target = Path(rule["source_path"]), Path(rule["target_path"])
        reasons: List[Dict[str, str]] = []

        def container_path(path: Path, field: str) -> Optional[Path]:
            try:
                return mount / path.relative_to(base)
            except ValueError:
                reasons.append({"field": field, "code": "OUTSIDE_MOUNT", "message": f"{field} is outside {base}"})
                return None

        container_source = container_path(source, "source_path")
        container_target = container_path(target, "target_path")
        # AVS runs inside the container, where the host's MEDIALIB_BASE_PATH is
        # visible only through MEDIALIB_MOUNT_PATH.  Filesystem diagnostics must
        # therefore inspect the translated paths, while still returning both
        # namespaces for operators.
        source_exists = bool(container_source and container_source.exists())
        target_exists = bool(container_target and container_target.exists())
        if container_source is not None and not source_exists:
            reasons.append({"field": "source_path", "code": "SOURCE_MISSING", "message": "源下载目录不存在"})
        if container_target is not None and not target_exists:
            reasons.append({"field": "target_path", "code": "TARGET_MISSING", "message": "媒体库目标目录不存在（检查不会创建目录）"})
        source_readable = bool(container_source and self._access(container_source, os.R_OK))
        source_writable = bool(container_source and self._access(container_source, os.W_OK))
        target_readable = bool(container_target and self._access(container_target, os.R_OK))
        target_writable = bool(container_target and self._access(container_target, os.W_OK))
        for field, exists, readable, writable in (
            ("source_path", source_exists, source_readable, source_writable),
            ("target_path", target_exists, target_readable, target_writable),
        ):
            if exists and not readable:
                reasons.append({"field": field, "code": "NOT_READABLE", "message": "目录不可读取"})
            if exists and not writable:
                reasons.append({"field": field, "code": "NOT_WRITABLE", "message": "目录不可写入"})
        source_dev = os.stat(container_source).st_dev if source_exists and container_source else None
        target_dev = os.stat(container_target).st_dev if target_exists and container_target else None
        same_filesystem = source_dev is not None and target_dev is not None and source_dev == target_dev
        if source_dev is not None and target_dev is not None and not same_filesystem:
            reasons.append({"field": "filesystem", "code": "CROSS_FILESYSTEM", "message": "源和目标不在同一文件系统，无法创建硬链接"})
        downloader_path = str(rule.get("downloader_path") or "").strip()
        if not downloader_path:
            reasons.append({"field": "downloader_path", "code": "DOWNLOADER_PATH_MISSING", "message": "下载器保存路径未配置"})
        return {
            "rule_id": rule_id,
            "media_type": rule["media_type"],
            "source": {"nas_path": str(source), "container_path": str(container_source) if container_source else None, "exists": source_exists, "readable": source_readable, "writable": source_writable, "st_dev": source_dev, "inside_mount": container_source is not None},
            "target": {"nas_path": str(target), "container_path": str(container_target) if container_target else None, "exists": target_exists, "readable": target_readable, "writable": target_writable, "st_dev": target_dev, "inside_mount": container_target is not None},
            "downloader_path": downloader_path or None,
            "downloader_path_configured": bool(downloader_path),
            "same_filesystem": same_filesystem,
            "status": "ok" if not reasons else "error",
            "reasons": reasons,
        }

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
                return deepcopy(updated) | {"default_change": self._default_impact(data["items"], updated["media_type"])}
        raise NotFoundError("path rule", rule_id)

    @classmethod
    def _default_impact(cls, items: List[Dict[str, Any]], media_type: str) -> Dict[str, Any]:
        primary = cls._primary(items, media_type)
        return {"media_type": media_type, "replacement_default_id": primary["id"] if primary else None}

    def disable_media_type(self, media_type: str) -> Dict[str, Any]:
        """Explicit, safe opt-out for a media path before removing its rules."""
        try:
            policy_for(media_type)
        except UnsupportedMediaType:
            raise ValidationAppError("不支持的媒体类型")
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item["media_type"] == media_type:
                    item["enabled"] = False
                    item["default_download"] = False
                    item["updated_at"] = utc_now_iso()
            self._write(data)
            self._sync_primary_paths(data["items"])
            return self._default_impact(data["items"], media_type)

    def delete(self, rule_id: str, *, disable_media_path: bool = False) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            removed = next((item for item in data["items"] if item["id"] == rule_id), None)
            if not removed:
                raise NotFoundError("path rule", rule_id)
            enabled_group = [item for item in data["items"] if item["media_type"] == removed["media_type"] and item["enabled"]]
            if removed["enabled"] and len(enabled_group) == 1 and not disable_media_path:
                raise ValidationAppError(
                    "不能删除此媒体类型唯一启用的路径规则",
                    [{"field": "path_rule_id", "message": "先禁用该媒体路径，或添加并启用替代规则", "code": "LAST_ENABLED_PATH_RULE"}],
                )
            if disable_media_path:
                for item in data["items"]:
                    if item["media_type"] == removed["media_type"]:
                        item["enabled"] = False
                        item["default_download"] = False
            original = len(data["items"])
            data["items"] = [item for item in data["items"] if item["id"] != rule_id]
            for media_type in MEDIA_POLICIES:
                group = [item for item in data["items"] if item["media_type"] == media_type and item["enabled"]]
                if group and not any(item["default_download"] for item in group):
                    group[0]["default_download"] = True
            self._write(data)
            self._sync_primary_paths(data["items"])
            return {"deleted_id": rule_id, "default_change": self._default_impact(data["items"], removed["media_type"])}

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

    def host_path_for_downloader(self, downloader_path: Any) -> Path:
        """Translate a downloader-container path back to the NAS host namespace."""
        value = self._normalized(downloader_path, "downloader_path")
        candidates = []
        for item in self.list():
            if not item["enabled"]:
                continue
            downloader_root = Path(item["downloader_path"])
            try:
                relative = value.relative_to(downloader_root)
                candidates.append((len(downloader_root.parts), Path(item["source_path"]) / relative))
            except ValueError:
                continue
        return max(candidates, key=lambda pair: pair[0])[1] if candidates else value
