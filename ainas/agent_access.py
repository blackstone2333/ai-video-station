"""Revocable agent tokens and connection presence tracking."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .config import Settings
from .errors import NotFoundError, ValidationAppError
from .watchlist import utc_now_iso


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class AgentAccessRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.agents_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"items": []})

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationAppError(f"Agent 连接记录无法读取：{exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise ValidationAppError("Agent 连接记录格式无效")
        return value

    def _write(self, value: Dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix="agents-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _public(item: Dict[str, Any], offline_minutes: int) -> Dict[str, Any]:
        last_seen = _parse_time(item.get("last_seen"))
        online = bool(
            not item.get("revoked")
            and last_seen
            and datetime.now(timezone.utc) - last_seen <= timedelta(minutes=offline_minutes)
        )
        return {
            key: deepcopy(value)
            for key, value in item.items()
            if key != "token_hash"
        } | {"online": online}

    def create(self, name: str = "My Agent") -> Dict[str, Any]:
        name = name.strip() or "My Agent"
        if len(name) > 100:
            raise ValidationAppError("Agent 名称不能超过 100 个字符")
        token = f"avs_agent_{secrets.token_urlsafe(32)}"
        now = utc_now_iso()
        item = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "token_hash": self._hash(token),
            "created_at": now,
            "connected_at": None,
            "last_seen": None,
            "capabilities": [],
            "revoked": False,
        }
        with self._lock:
            data = self._read()
            data["items"].append(item)
            self._write(data)
        return {**self._public(item, self.settings.agent_offline_minutes), "token": token}

    def authenticate(self, token: str, touch: bool = True) -> Optional[Dict[str, Any]]:
        if not token.startswith("avs_agent_"):
            return None
        token_hash = self._hash(token)
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item.get("revoked") or not hmac.compare_digest(str(item.get("token_hash", "")), token_hash):
                    continue
                if touch:
                    last_seen = _parse_time(item.get("last_seen"))
                    if not last_seen or datetime.now(timezone.utc) - last_seen >= timedelta(seconds=60):
                        item["last_seen"] = utc_now_iso()
                        self._write(data)
                return self._public(item, self.settings.agent_offline_minutes)
        return None

    def connect(self, agent_id: str, name: str, capabilities: Iterable[str]) -> Dict[str, Any]:
        clean_name = name.strip() or "My Agent"
        clean_capabilities = list(dict.fromkeys(str(item).strip() for item in capabilities if str(item).strip()))[:50]
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item["id"] != agent_id or item.get("revoked"):
                    continue
                now = utc_now_iso()
                item.update(
                    {
                        "name": clean_name[:100],
                        "capabilities": clean_capabilities,
                        "connected_at": item.get("connected_at") or now,
                        "last_seen": now,
                    }
                )
                self._write(data)
                return self._public(item, self.settings.agent_offline_minutes)
        raise NotFoundError("agent", agent_id)

    def list(self) -> list[Dict[str, Any]]:
        with self._lock:
            values = [self._public(item, self.settings.agent_offline_minutes) for item in self._read()["items"]]
            values.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            return values

    def revoke(self, agent_id: str) -> None:
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item["id"] == agent_id:
                    item["revoked"] = True
                    item["last_seen"] = None
                    self._write(data)
                    return
        raise NotFoundError("agent", agent_id)
