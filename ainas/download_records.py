from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .errors import NotFoundError, ValidationAppError
from .state import StateStore, StateStoreError
from .watchlist import utc_now_iso


class DismissedDownloadRepository:
    """Persist AVS-only hidden downloader rows without mutating the downloader."""

    def __init__(self, path: Path, state_store: StateStore | None = None) -> None:
        self.path = path
        self.state_store = state_store
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_store:
            try: self.state_store.ensure_records("dismissed_downloads", legacy_path=self.path)
            except StateStoreError as exc: raise ValidationAppError(f"dismissed downloads state is unavailable: {exc}") from exc
        elif not self.path.exists():
            self._write({"items": []})

    @staticmethod
    def _hash(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized or len(normalized) > 128:
            raise ValidationAppError("invalid downloader task hash")
        return normalized

    def _read(self) -> Dict[str, Any]:
        if self.state_store:
            try: return {"items": self.state_store.list_records("dismissed_downloads")}
            except StateStoreError as exc: raise ValidationAppError(f"dismissed downloads state is unavailable: {exc}") from exc
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError("invalid dismissed downloads shape")
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
            try: self.state_store.replace_records("dismissed_downloads", value["items"]); return
            except StateStoreError as exc: raise ValidationAppError(f"dismissed downloads state is unavailable: {exc}") from exc
        fd, temp_name = tempfile.mkstemp(prefix="dismissed-downloads-", suffix=".json", dir=str(self.path.parent))
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
            values = deepcopy(self._read()["items"])
        values.sort(key=lambda item: item.get("dismissed_at", ""), reverse=True)
        return values

    def contains(self, task_hash: str) -> bool:
        normalized = self._hash(task_hash)
        return any(item.get("hash") == normalized for item in self.list())

    def visible(self, tasks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        hidden = {item.get("hash") for item in self.list()}
        return [item for item in tasks if str(item.get("hash") or "").lower() not in hidden]

    def dismiss(self, task: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._hash(task.get("hash"))
        with self._lock:
            data = self._read()
            for item in data["items"]:
                if item.get("hash") == normalized:
                    return deepcopy(item)
            item = {
                "hash": normalized,
                "name": str(task.get("name") or normalized),
                "state": task.get("state"),
                "category": task.get("category"),
                "dismissed_at": utc_now_iso(),
            }
            data["items"].append(item)
            self._write(data)
            return deepcopy(item)

    def restore(self, task_hash: str) -> Dict[str, Any]:
        normalized = self._hash(task_hash)
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["items"]):
                if item.get("hash") == normalized:
                    restored = data["items"].pop(index)
                    self._write(data)
                    return deepcopy(restored)
        raise NotFoundError("dismissed downloader task", normalized)
