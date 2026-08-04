from __future__ import annotations

import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict


STANDARD_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
}

AGENT_TOKEN_RE = re.compile(r"avs_agent_[A-Za-z0-9_-]+")
AUTHORIZATION_RE = re.compile(r"(?i)(authorization[\"':=\s]+bearer\s+)[^\s,}\]]+")
API_KEY_RE = re.compile(r"(?i)((?:x-)?api[_-]?key[\"':=\s]+)[^\s,}\]]+")


def _redact_text(value: str) -> str:
    value = AGENT_TOKEN_RE.sub("[REDACTED_AGENT_TOKEN]", value)
    value = AUTHORIZATION_RE.sub(r"\1[REDACTED]", value)
    return API_KEY_RE.sub(r"\1[REDACTED]", value)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(_redact(payload), ensure_ascii=False, default=str)


def configure_logging(level: str, path: Path, max_bytes: int, backup_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    root.setLevel(level)


def read_log_entries(
    path: Path,
    *,
    limit: int = 200,
    level: str | None = None,
    query: str | None = None,
) -> list[Dict[str, Any]]:
    """Return newest structured application logs without exposing arbitrary files."""
    normalized_level = level.casefold() if level else None
    normalized_query = query.casefold() if query else None
    rotated = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(f"{path.name}.")
        if suffix.isdigit():
            rotated.append((int(suffix), candidate))
    sources = ([path] if path.exists() else []) + [item[1] for item in sorted(rotated)]

    selected: deque[Dict[str, Any]] = deque(maxlen=limit)
    for source in sources:
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                item = {"timestamp": None, "level": "info", "logger": "legacy", "message": line}
            if normalized_level and str(item.get("level", "")).casefold() != normalized_level:
                continue
            if normalized_query and normalized_query not in json.dumps(item, ensure_ascii=False).casefold():
                continue
            selected.append(_redact(item))
            if len(selected) >= limit:
                return list(selected)
    return list(selected)
