"""Runtime-editable downloader and scheduler settings."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Protocol, runtime_checkable

from pydantic import ValidationError

from .config import Settings
from .errors import ConflictError, ServiceUnavailableError, ValidationAppError
from .qbittorrent import QBittorrentClient
from .transmission import TransmissionClient
from .state import StateStore, StateStoreError


DOWNLOADER_FIELDS = (
    "downloader_type",
    "qb_host",
    "qb_port",
    "qb_username",
    "qb_password",
    "qb_use_https",
    "qb_verify_ssl",
    "transmission_host",
    "transmission_port",
    "transmission_username",
    "transmission_password",
    "transmission_use_https",
    "transmission_verify_ssl",
    "transmission_rpc_path",
)
PASSWORD_FIELDS = {"qb_password", "transmission_password"}


@runtime_checkable
class Downloader(Protocol):
    """The stable downloader operations used by application services."""

    @property
    def configured(self) -> bool: ...

    def add_download(self, download_link: str, category: str, **kwargs: Any) -> Dict[str, Any]: ...
    def add_torrent_file(self, content: bytes, filename: str, category: str, **kwargs: Any) -> Dict[str, Any]: ...
    def torrent_info(self, hash_value: str) -> Dict[str, Any] | None: ...
    def files(self, hash_value: str) -> list[Dict[str, Any]]: ...
    def set_file_priorities(
        self, hash_value: str, selected_indices: list[int], skipped_indices: list[int]
    ) -> None: ...
    def rename_file(self, hash_value: str, old_path: str, new_path: str) -> None: ...
    def rename_folder(self, hash_value: str, old_path: str, new_path: str) -> None: ...
    def rename_torrent(self, hash_value: str, name: str) -> None: ...
    def set_category(self, hash_value: str, category: str) -> None: ...
    def resume(self, hash_value: str) -> None: ...
    def pause(self, hash_value: str) -> None: ...
    def delete(self, hash_value: str, delete_files: bool = True) -> None: ...
    def recheck(self, hash_value: str) -> None: ...
    def set_location(self, hash_value: str, location: Any) -> None: ...
    def reconcile_submission(
        self,
        *,
        category: str,
        save_path: Any,
        root_name: str,
        expected_hash: str | None = None,
    ) -> Dict[str, Any] | None: ...
    def status(self) -> Dict[str, Any]: ...
    def tasks(self) -> list[Dict[str, Any]]: ...


@dataclass(frozen=True)
class DownloaderCapabilities:
    """Named features, so callers can make a deliberate compatibility choice."""

    name: str
    operations: FrozenSet[str]

    def supports(self, operation: str) -> bool:
        return operation in self.operations


COMMON_DOWNLOADER_OPERATIONS = frozenset(
    {
        "add_download", "add_torrent_file", "torrent_info", "files", "rename_file",
        "rename_folder", "rename_torrent", "set_category", "resume", "recheck",
        "set_location", "set_file_priorities", "status", "tasks", "pause", "delete",
    }
)
DOWNLOADER_REGISTRY: Mapping[str, DownloaderCapabilities] = {
    "qbittorrent": DownloaderCapabilities(
        "qBittorrent", COMMON_DOWNLOADER_OPERATIONS | {"categories", "reconcile_submission"}
    ),
    "transmission": DownloaderCapabilities("Transmission", COMMON_DOWNLOADER_OPERATIONS),
}


def _atomic_write(path: Path, prefix: str, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class DownloaderSettingsRepository:
    def __init__(self, settings: Settings, state_store: StateStore | None = None) -> None:
        self.settings = settings
        self.state_store = state_store
        self.path = settings.downloader_settings_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_store:
            try: self.state_store.ensure_document("downloader_settings", legacy_path=self.path)
            except StateStoreError as exc: raise ValidationAppError(f"保存的下载器设置无法读取：{exc}") from exc
        self.load()

    def _candidate(self, changes: Mapping[str, Any]) -> Settings:
        unknown = sorted(set(changes) - set(DOWNLOADER_FIELDS))
        if unknown:
            raise ValidationAppError(
                "包含不支持的下载器设置",
                [{"field": name, "message": "此设置不可修改", "code": "UNKNOWN_DOWNLOADER_SETTING"} for name in unknown],
            )
        values = self.settings.model_dump()
        values.update(changes)
        try:
            candidate = Settings.model_validate(values)
        except ValidationError as exc:
            raise ValidationAppError(
                "下载器设置校验失败",
                [
                    {
                        "field": ".".join(str(part) for part in item["loc"]),
                        "message": item["msg"],
                        "code": item["type"],
                    }
                    for item in exc.errors()
                ],
            ) from exc
        return candidate

    @staticmethod
    def _secret_value(candidate: Settings, name: str) -> str:
        return getattr(candidate, name).get_secret_value()

    def _persisted(self, candidate: Settings) -> Dict[str, Any]:
        return {
            name: self._secret_value(candidate, name) if name in PASSWORD_FIELDS else getattr(candidate, name)
            for name in DOWNLOADER_FIELDS
        }

    def _apply(self, candidate: Settings) -> None:
        for name in DOWNLOADER_FIELDS:
            setattr(self.settings, name, getattr(candidate, name))

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self.state_store or self.path.exists():
                try:
                    value = self.state_store.read_document("downloader_settings") if self.state_store else json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, StateStoreError) as exc:
                    raise ValidationAppError(f"保存的下载器设置无法读取：{exc}") from exc
                if not isinstance(value, dict):
                    raise ValidationAppError("保存的下载器设置格式无效")
                self._apply(self._candidate(value))
            return self.get()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            result = {
                name: getattr(self.settings, name)
                for name in DOWNLOADER_FIELDS
                if name not in PASSWORD_FIELDS
            }
            result["qb_password_configured"] = bool(self.settings.qb_password.get_secret_value())
            result["transmission_password_configured"] = bool(
                self.settings.transmission_password.get_secret_value()
            )
            return result

    def update(self, changes: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cleaned = dict(changes)
            for name in PASSWORD_FIELDS:
                if name in cleaned and not str(cleaned[name] or "").strip():
                    cleaned.pop(name)
            candidate = self._candidate(cleaned)
            payload = self._persisted(candidate)
            try:
                self.state_store.write_document("downloader_settings", payload) if self.state_store else _atomic_write(self.path, "downloader-settings-", payload)
            except StateStoreError as exc:
                raise ValidationAppError(f"保存的下载器设置无法写入：{exc}") from exc
            self._apply(candidate)
            return self.get()


class SystemSettingsRepository:
    FIELDS = (
        "watchlist_check_hours",
        "directory_sync_enabled",
        "directory_sync_settle_seconds",
        "cleanup_auto_scan_enabled",
        "cleanup_auto_execute_enabled",
        "cleanup_auto_delete_source",
        "cleanup_policy",
        "cleanup_scan_hours",
    )

    def __init__(self, settings: Settings, state_store: StateStore | None = None) -> None:
        self.settings = settings
        self.state_store = state_store
        self.path = settings.system_settings_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_store:
            try: self.state_store.ensure_document("system_settings", legacy_path=self.path)
            except StateStoreError as exc: raise ValidationAppError(f"保存的系统设置无法读取：{exc}") from exc
        self.load()

    def _candidate(self, changes: Mapping[str, Any]) -> Settings:
        cleaned = {k: v for k, v in changes.items() if k != "directory_sync_minutes"}
        unknown = sorted(set(cleaned) - set(self.FIELDS))
        if unknown:
            raise ValidationAppError("包含不支持的系统设置")
        values = self.settings.model_dump()
        values.update(changes)
        if values.get("cleanup_auto_execute_enabled") and not values.get("cleanup_auto_scan_enabled"):
            raise ValidationAppError("自动执行重复清理前必须开启自动扫描")
        if values.get("cleanup_auto_delete_source") and not values.get("cleanup_auto_execute_enabled"):
            raise ValidationAppError("自动删除源数据前必须开启自动执行")
        try:
            candidate = Settings.model_validate(values)
        except ValidationError as exc:
            raise ValidationAppError(
                "系统设置校验失败",
                [
                    {
                        "field": ".".join(str(part) for part in item["loc"]),
                        "message": item["msg"],
                        "code": item["type"],
                    }
                    for item in exc.errors()
                ],
            ) from exc
        return candidate

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self.state_store or self.path.exists():
                try:
                    value = self.state_store.read_document("system_settings") if self.state_store else json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, StateStoreError) as exc:
                    raise ValidationAppError(f"保存的系统设置无法读取：{exc}") from exc
                if not isinstance(value, dict):
                    raise ValidationAppError("保存的系统设置格式无效")
                cleaned = {k: v for k, v in value.items() if k != "directory_sync_minutes"}
                candidate = self._candidate(cleaned)
                for name in self.FIELDS:
                    setattr(self.settings, name, getattr(candidate, name))
            return self.get()

    def get(self) -> Dict[str, Any]:
        return {name: getattr(self.settings, name) for name in self.FIELDS}

    def update(self, changes: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cleaned = {k: v for k, v in changes.items() if k != "directory_sync_minutes"}
            candidate = self._candidate(cleaned)
            value = {name: getattr(candidate, name) for name in self.FIELDS}
            try:
                self.state_store.write_document("system_settings", value) if self.state_store else _atomic_write(self.path, "system-settings-", value)
            except StateStoreError as exc:
                raise ValidationAppError(f"保存的系统设置无法写入：{exc}") from exc
            for name, field_value in value.items():
                setattr(self.settings, name, field_value)
            return self.get()


class DownloaderManager:
    """Proxy to the currently selected downloader, rebuilding it after settings change."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._fingerprint: tuple[Any, ...] | None = None
        self._client: Any = None

    def _settings_fingerprint(self) -> tuple[Any, ...]:
        return (
            self.settings.downloader_type,
            self.settings.qb_base_url,
            self.settings.qb_username,
            self.settings.qb_password.get_secret_value(),
            self.settings.qb_verify_ssl,
            self.settings.transmission_base_url,
            self.settings.transmission_username,
            self.settings.transmission_password.get_secret_value(),
            self.settings.transmission_verify_ssl,
            self.settings.transmission_rpc_path,
        )

    def _current(self) -> Any:
        with self._lock:
            fingerprint = self._settings_fingerprint()
            if self._client is None or fingerprint != self._fingerprint:
                self._client = (
                    TransmissionClient(self.settings)
                    if self.settings.downloader_type == "transmission"
                    else QBittorrentClient(self.settings)
                )
                self._fingerprint = fingerprint
            return self._client

    @property
    def capabilities(self) -> DownloaderCapabilities:
        """Capabilities of the selected adapter, without making a network call."""
        return DOWNLOADER_REGISTRY[self.settings.downloader_type]

    def supports(self, operation: str) -> bool:
        return self.capabilities.supports(operation)

    def require_capability(self, operation: str) -> None:
        if not self.supports(operation):
            raise ConflictError(
                f"{self.capabilities.name} does not support '{operation}'; "
                "select a compatible downloader or disable this feature"
            )

    @property
    def configured(self) -> bool:
        return bool(self._current().configured)

    def reconcile_submission(
        self,
        *,
        category: str,
        save_path: Any,
        root_name: str,
        expected_hash: str | None = None,
    ) -> Dict[str, Any] | None:
        """Reconcile an accepted HTTP torrent when the selected adapter can do so.

        Transmission does not expose the same invariant-rich lookup. Returning
        ``None`` keeps the naming job retryable instead of advertising a method
        that the active adapter cannot implement.
        """
        if not self.supports("reconcile_submission"):
            return None
        return self._current().reconcile_submission(
            category=category,
            save_path=save_path,
            root_name=root_name,
            expected_hash=expected_hash,
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        # Do not turn adapter differences into a Python AttributeError.  The Flask
        # AppError handler serializes this as a useful 409 response for API callers.
        if name in COMMON_DOWNLOADER_OPERATIONS or name == "categories":
            self.require_capability(name)
            value = getattr(self._current(), name, None)
            if value is None:
                raise ServiceUnavailableError(
                    f"selected downloader advertises '{name}' but its adapter is unavailable"
                )
            return value
        raise AttributeError(name)
