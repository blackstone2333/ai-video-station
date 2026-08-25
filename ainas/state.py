"""Small, versioned SQLite state store for single-host AVS deployments.

The store deliberately keeps domain validation in each repository.  It provides
atomic ordered record replacement, simple documents, and one-time import of the
legacy JSON files without deleting or rewriting those files.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStoreError(RuntimeError):
    """Raised when durable state cannot be read or committed safely."""


class StateStore:
    """Thread-safe SQLite storage with no external service dependency."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        try:
            with self._lock, self._connect() as connection:
                # WAL permits the UI to read state while the scheduler commits a
                # job update.  A single AVS worker remains the supported model.
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS avs_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS avs_namespaces (
                        namespace TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK (kind IN ('records', 'document')),
                        schema_version INTEGER NOT NULL,
                        initialized_at TEXT NOT NULL,
                        legacy_source TEXT
                    );
                    CREATE TABLE IF NOT EXISTS avs_records (
                        namespace TEXT NOT NULL,
                        record_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (namespace, record_id),
                        FOREIGN KEY (namespace) REFERENCES avs_namespaces(namespace)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS avs_records_order
                        ON avs_records(namespace, position);
                    CREATE TABLE IF NOT EXISTS avs_documents (
                        namespace TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (namespace) REFERENCES avs_namespaces(namespace)
                            ON DELETE CASCADE
                    );
                    """
                )
                current = connection.execute(
                    "SELECT value FROM avs_meta WHERE key = 'schema_version'"
                ).fetchone()
                if current is None:
                    connection.execute(
                        "INSERT INTO avs_meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif int(current["value"]) > SCHEMA_VERSION:
                    raise StateStoreError(
                        f"state database schema {current['value']} is newer than supported {SCHEMA_VERSION}"
                    )
        except (OSError, sqlite3.Error, ValueError) as exc:
            if isinstance(exc, StateStoreError):
                raise
            raise StateStoreError(f"could not initialize state database: {exc}") from exc

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _load_json(value: str) -> Any:
        return json.loads(value)

    @staticmethod
    def _read_legacy(path: Optional[Path]) -> Optional[Any]:
        if path is None or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateStoreError(f"legacy state cannot be imported from {path}: {exc}") from exc

    def ensure_records(
        self,
        namespace: str,
        *,
        legacy_path: Optional[Path] = None,
        default: Iterable[Mapping[str, Any]] = (),
        schema_version: int = 1,
    ) -> None:
        """Create a record namespace, importing a legacy ``{"items": []}`` once."""
        with self._lock:
            try:
                with self._connect() as connection:
                    exists = connection.execute(
                        "SELECT kind, schema_version FROM avs_namespaces WHERE namespace = ?", (namespace,)
                    ).fetchone()
                    if exists:
                        if exists["kind"] != "records":
                            raise StateStoreError(f"state namespace {namespace!r} has the wrong kind")
                        if int(exists["schema_version"]) > schema_version:
                            raise StateStoreError(
                                f"state namespace {namespace!r} schema {exists['schema_version']} "
                                f"is newer than supported {schema_version}"
                            )
                        return
                    legacy = self._read_legacy(legacy_path)
                    if legacy is None:
                        items = [dict(item) for item in default]
                    elif isinstance(legacy, dict) and isinstance(legacy.get("items"), list):
                        items = legacy["items"]
                    else:
                        raise StateStoreError(f"legacy record state has an invalid shape: {legacy_path}")
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO avs_namespaces(namespace, kind, schema_version, initialized_at, legacy_source) "
                        "VALUES(?, 'records', ?, ?, ?)",
                        (namespace, schema_version, _utc_now(), str(legacy_path) if legacy_path else None),
                    )
                    self._replace_records(connection, namespace, items)
                    connection.execute("COMMIT")
            except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateStoreError(f"could not initialize record state {namespace}: {exc}") from exc

    def ensure_document(
        self,
        namespace: str,
        *,
        legacy_path: Optional[Path] = None,
        default: Optional[Mapping[str, Any]] = None,
        schema_version: int = 1,
    ) -> None:
        with self._lock:
            try:
                with self._connect() as connection:
                    exists = connection.execute(
                        "SELECT kind, schema_version FROM avs_namespaces WHERE namespace = ?", (namespace,)
                    ).fetchone()
                    if exists:
                        if exists["kind"] != "document":
                            raise StateStoreError(f"state namespace {namespace!r} has the wrong kind")
                        if int(exists["schema_version"]) > schema_version:
                            raise StateStoreError(
                                f"state namespace {namespace!r} schema {exists['schema_version']} "
                                f"is newer than supported {schema_version}"
                            )
                        return
                    legacy = self._read_legacy(legacy_path)
                    value = dict(default or {}) if legacy is None else legacy
                    if not isinstance(value, dict):
                        raise StateStoreError(f"legacy document state has an invalid shape: {legacy_path}")
                    now = _utc_now()
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO avs_namespaces(namespace, kind, schema_version, initialized_at, legacy_source) "
                        "VALUES(?, 'document', ?, ?, ?)",
                        (namespace, schema_version, now, str(legacy_path) if legacy_path else None),
                    )
                    connection.execute(
                        "INSERT INTO avs_documents(namespace, payload, updated_at) VALUES(?, ?, ?)",
                        (namespace, self._json(value), now),
                    )
                    connection.execute("COMMIT")
            except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateStoreError(f"could not initialize document state {namespace}: {exc}") from exc

    @staticmethod
    def _record_id(item: Mapping[str, Any], position: int) -> str:
        value = item.get("id") or item.get("torrent_hash") or item.get("hash")
        if not value:
            raise StateStoreError(f"record at position {position} has no stable id")
        return str(value)

    def _replace_records(
        self, connection: sqlite3.Connection, namespace: str, items: Iterable[Mapping[str, Any]]
    ) -> None:
        values = [dict(item) for item in items]
        connection.execute("DELETE FROM avs_records WHERE namespace = ?", (namespace,))
        now = _utc_now()
        seen: set[str] = set()
        for position, item in enumerate(values):
            record_id = self._record_id(item, position)
            if record_id in seen:
                raise StateStoreError(f"duplicate record id {record_id!r} in {namespace}")
            seen.add(record_id)
            connection.execute(
                "INSERT INTO avs_records(namespace, record_id, position, payload, updated_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (namespace, record_id, position, self._json(item), now),
            )

    def list_records(self, namespace: str) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                with self._connect() as connection:
                    rows = connection.execute(
                        "SELECT payload FROM avs_records WHERE namespace = ? ORDER BY position",
                        (namespace,),
                    ).fetchall()
                return deepcopy([self._load_json(row["payload"]) for row in rows])
            except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                raise StateStoreError(f"could not read record state {namespace}: {exc}") from exc

    def replace_records(self, namespace: str, items: Iterable[Mapping[str, Any]]) -> None:
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._replace_records(connection, namespace, items)
                    connection.execute("COMMIT")
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                raise StateStoreError(f"could not commit record state {namespace}: {exc}") from exc

    def read_document(self, namespace: str) -> Dict[str, Any]:
        with self._lock:
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT payload FROM avs_documents WHERE namespace = ?", (namespace,)
                    ).fetchone()
                if row is None:
                    raise StateStoreError(f"document namespace {namespace!r} is not initialized")
                value = self._load_json(row["payload"])
                if not isinstance(value, dict):
                    raise StateStoreError(f"document namespace {namespace!r} has an invalid payload")
                return deepcopy(value)
            except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                raise StateStoreError(f"could not read document state {namespace}: {exc}") from exc

    def write_document(self, namespace: str, value: Mapping[str, Any]) -> None:
        with self._lock:
            try:
                with self._connect() as connection:
                    result = connection.execute(
                        "UPDATE avs_documents SET payload = ?, updated_at = ? WHERE namespace = ?",
                        (self._json(dict(value)), _utc_now(), namespace),
                    )
                    if result.rowcount != 1:
                        raise StateStoreError(f"document namespace {namespace!r} is not initialized")
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                raise StateStoreError(f"could not commit document state {namespace}: {exc}") from exc

    def integrity_check(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with self._connect() as connection:
                    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                    version = connection.execute(
                        "SELECT value FROM avs_meta WHERE key = 'schema_version'"
                    ).fetchone()[0]
                    namespaces = connection.execute("SELECT COUNT(*) FROM avs_namespaces").fetchone()[0]
                return {
                    "status": "ok" if result == "ok" else "error",
                    "detail": result,
                    "schema_version": int(version),
                    "namespaces": int(namespaces),
                }
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise StateStoreError(f"could not check state database: {exc}") from exc
