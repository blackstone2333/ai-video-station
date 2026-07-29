"""Runtime-editable downloader and scheduler settings."""

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
from .qbittorrent import QBittorrentClient
from .transmission import TransmissionClient


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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.downloader_settings_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
            return Settings.model_validate(values)
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
            if self.path.exists():
                try:
                    value = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
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
            _atomic_write(self.path, "downloader-settings-", self._persisted(candidate))
            self._apply(candidate)
            return self.get()


class SystemSettingsRepository:
    FIELDS = ("watchlist_check_hours",)

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.system_settings_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    def _candidate(self, changes: Mapping[str, Any]) -> Settings:
        unknown = sorted(set(changes) - set(self.FIELDS))
        if unknown:
            raise ValidationAppError("包含不支持的系统设置")
        values = self.settings.model_dump()
        values.update(changes)
        try:
            return Settings.model_validate(values)
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

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    value = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValidationAppError(f"保存的系统设置无法读取：{exc}") from exc
                if not isinstance(value, dict):
                    raise ValidationAppError("保存的系统设置格式无效")
                candidate = self._candidate(value)
                for name in self.FIELDS:
                    setattr(self.settings, name, getattr(candidate, name))
            return self.get()

    def get(self) -> Dict[str, Any]:
        return {name: getattr(self.settings, name) for name in self.FIELDS}

    def update(self, changes: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            candidate = self._candidate(changes)
            value = {name: getattr(candidate, name) for name in self.FIELDS}
            _atomic_write(self.path, "system-settings-", value)
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
    def configured(self) -> bool:
        return bool(self._current().configured)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._current(), name)
