from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import NotFoundError
from .errors import AppError
from .state import StateStore, StateStoreError
from .resource_preferences import profile_for


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WatchlistRepository:
    def __init__(self, path: Path, state_store: Optional[StateStore] = None) -> None:
        self.path = path
        self.state_store = state_store
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_store:
            try:
                self.state_store.ensure_records("watchlist", legacy_path=self.path)
            except StateStoreError as exc:
                raise AppError("State Error", str(exc), "state-error", 500) from exc
        elif not self.path.exists():
            self._write({"items": []})

    def _read(self) -> Dict[str, Any]:
        if self.state_store:
            try:
                return {"items": self.state_store.list_records("watchlist")}
            except StateStoreError as exc:
                raise AppError("State Error", str(exc), "state-error", 500) from exc
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError("invalid watchlist shape")
            return value
        except (OSError, ValueError, json.JSONDecodeError):
            backup = self.path.with_suffix(f".corrupt-{uuid.uuid4().hex[:8]}.json")
            if self.path.exists():
                self.path.replace(backup)
            value = {"items": []}
            self._write(value)
            return value

    def _write(self, value: Dict[str, Any]) -> None:
        if self.state_store:
            try:
                self.state_store.replace_records("watchlist", value["items"])
                return
            except StateStoreError as exc:
                raise AppError("State Error", str(exc), "state-error", 500) from exc
        fd, temp_name = tempfile.mkstemp(prefix="watchlist-", suffix=".json", dir=str(self.path.parent))
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

    def get(self, item_id: str) -> Dict[str, Any]:
        with self._lock:
            for item in self._read()["items"]:
                if item["id"] == item_id:
                    return deepcopy(item)
        raise NotFoundError("watchlist item", item_id)

    def find(self, keyword: str, media_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        normalized = keyword.strip().casefold()
        with self._lock:
            for item in self._read()["items"]:
                same_type = (
                    media_type is None
                    or item["type"] == media_type
                    or item["type"] == "auto"
                    or media_type == "auto"
                )
                if item["keyword"].casefold() == normalized and same_type:
                    return deepcopy(item)
        return None

    def add(
        self,
        keyword: str,
        media_type: str,
        path_rule_id: Optional[str] = None,
        path_rule_snapshot: Optional[Dict[str, Any]] = None,
        viewing_mode: str = "daily",
        resource_preferences: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized = keyword.strip().casefold()
        with self._lock:
            data = self._read()
            for item in data["items"]:
                # A prior auto subscription and a subsequently resolved type are
                # the same intent; do not create parallel polling/download jobs.
                equivalent_type = item["type"] == media_type or item["type"] == "auto" or media_type == "auto"
                if item["keyword"].casefold() == normalized and equivalent_type:
                    return deepcopy(item)
            now = utc_now_iso()
            item = {
                "id": uuid.uuid4().hex,
                "keyword": keyword.strip(),
                "type": media_type,
                "added_at": now,
                "last_check": None,
                "check_count": 0,
                "downloaded_episodes": [],
                "downloaded_links": [],
                "episode_sources": {},
                "status": "monitoring" if media_type in {"tv", "anime"} else "pending",
                "found_at": None,
                "last_error": None,
                "path_rule_id": path_rule_id,
                "path_rule_snapshot": deepcopy(path_rule_snapshot) if path_rule_snapshot else None,
                "viewing_mode": viewing_mode,
                "resource_preferences": deepcopy(resource_preferences) if resource_preferences else profile_for(viewing_mode).to_dict(),
            }
            data["items"].append(item)
            self._write(data)
            return deepcopy(item)

    def update(self, item_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item["id"] == item_id:
                    item.update(deepcopy(changes))
                    self._write(data)
                    return deepcopy(item)
        raise NotFoundError("watchlist item", item_id)

    def delete(self, item_id: str) -> None:
        with self._lock:
            data = self._read()
            original_count = len(data["items"])
            data["items"] = [item for item in data["items"] if item["id"] != item_id]
            if len(data["items"]) == original_count:
                raise NotFoundError("watchlist item", item_id)
            self._write(data)
