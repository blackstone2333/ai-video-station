"""Small, dependency-free BitTorrent metadata reader for manual uploads."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from .errors import ValidationAppError


@dataclass(frozen=True)
class TorrentMetadata:
    info_hash: str
    name: str


def _decode(data: bytes, index: int = 0, depth: int = 0) -> Tuple[Any, int]:
    if depth > 100 or index >= len(data):
        raise ValueError("invalid bencode structure")
    marker = data[index : index + 1]
    if marker == b"i":
        end = data.find(b"e", index + 1)
        if end < 0:
            raise ValueError("unterminated integer")
        raw = data[index + 1 : end]
        if not raw or (raw.startswith(b"-") and len(raw) == 1):
            raise ValueError("invalid integer")
        return int(raw), end + 1
    if marker == b"l":
        values = []
        cursor = index + 1
        while cursor < len(data) and data[cursor : cursor + 1] != b"e":
            value, cursor = _decode(data, cursor, depth + 1)
            values.append(value)
        if cursor >= len(data):
            raise ValueError("unterminated list")
        return values, cursor + 1
    if marker == b"d":
        values: Dict[bytes, Any] = {}
        cursor = index + 1
        while cursor < len(data) and data[cursor : cursor + 1] != b"e":
            key, cursor = _decode(data, cursor, depth + 1)
            if not isinstance(key, bytes):
                raise ValueError("dictionary key must be bytes")
            values[key], cursor = _decode(data, cursor, depth + 1)
        if cursor >= len(data):
            raise ValueError("unterminated dictionary")
        return values, cursor + 1
    if marker.isdigit():
        colon = data.find(b":", index)
        if colon < 0:
            raise ValueError("invalid byte string")
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        if length < 0 or end > len(data):
            raise ValueError("byte string exceeds payload")
        return data[start:end], end
    raise ValueError("unknown bencode marker")


def parse_torrent_metadata(data: bytes) -> TorrentMetadata:
    if not data or data[:1] != b"d":
        raise ValidationAppError("BT 种子格式无效")
    try:
        cursor = 1
        info = None
        info_bytes = None
        while cursor < len(data) and data[cursor : cursor + 1] != b"e":
            key, cursor = _decode(data, cursor, 1)
            if not isinstance(key, bytes):
                raise ValueError("invalid top-level key")
            start = cursor
            value, cursor = _decode(data, cursor, 1)
            if key == b"info":
                info = value
                info_bytes = data[start:cursor]
        if cursor != len(data) - 1 or not isinstance(info, dict) or not info_bytes:
            raise ValueError("missing info dictionary")
        raw_name = info.get(b"name.utf-8") or info.get(b"name")
        if not isinstance(raw_name, bytes):
            raise ValueError("missing torrent name")
        name = raw_name.decode("utf-8", errors="replace").strip()
        if not name:
            raise ValueError("empty torrent name")
    except (ValueError, TypeError) as exc:
        raise ValidationAppError(f"BT 种子格式无效：{exc}") from exc
    return TorrentMetadata(hashlib.sha1(info_bytes).hexdigest(), name)
