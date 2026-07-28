from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

import requests

from .config import Settings
from .errors import ServiceUnavailableError, UpstreamError
from .qbittorrent import QBittorrentClient, torrent_hash


logger = logging.getLogger(__name__)
STATUS_NAMES = {
    0: "pausedDL",
    1: "queuedDL",
    2: "checkingDL",
    3: "queuedDL",
    4: "downloading",
    5: "stalledUP",
    6: "uploading",
}


class TransmissionClient:
    """Transmission RPC adapter exposing the downloader contract used by NamingService."""

    def __init__(self, settings: Settings, session: Optional[requests.Session] = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self._session_id: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.transmission_base_url)

    def _url(self) -> str:
        if not self.settings.transmission_base_url:
            raise ServiceUnavailableError("Transmission is not configured; set TRANSMISSION_HOST")
        path = "/" + self.settings.transmission_rpc_path.strip("/")
        return f"{self.settings.transmission_base_url}{path}"

    def _rpc(self, method: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"X-Transmission-Session-Id": self._session_id} if self._session_id else {}
        auth = None
        if self.settings.transmission_username:
            auth = (
                self.settings.transmission_username,
                self.settings.transmission_password.get_secret_value(),
            )
        try:
            response = self.session.post(
                self._url(),
                json={"method": method, "arguments": arguments or {}},
                headers=headers,
                auth=auth,
                timeout=self.settings.request_timeout_seconds,
                verify=self.settings.transmission_verify_ssl,
            )
            if response.status_code == 409:
                new_session_id = response.headers.get("X-Transmission-Session-Id")
                if not new_session_id or new_session_id == self._session_id:
                    raise UpstreamError("Transmission", "session negotiation failed")
                self._session_id = new_session_id
                return self._rpc(method, arguments)
            if response.status_code in {401, 403}:
                raise ServiceUnavailableError("Transmission rejected the configured credentials")
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise UpstreamError("Transmission", "request timed out", timeout=True) from exc
        except requests.RequestException as exc:
            raise UpstreamError("Transmission", "connection failed") from exc
        except ValueError as exc:
            raise UpstreamError("Transmission", "returned invalid JSON") from exc
        if payload.get("result") != "success":
            raise UpstreamError("Transmission", str(payload.get("result") or "RPC request failed"))
        return dict(payload.get("arguments") or {})

    def add_download(self, download_link: str, category: str, rename: Optional[str] = None) -> Dict[str, Any]:
        link = QBittorrentClient.validate_download_link(download_link)
        arguments: Dict[str, Any] = {
            "filename": link,
            "download-dir": str(self.settings.download_path_for_category(category)),
            "paused": False,
            "labels": [category],
        }
        result = self._rpc("torrent-add", arguments)
        value = result.get("torrent-added") or result.get("torrent-duplicate") or {}
        task_hash = value.get("hashString") or torrent_hash(link)
        logger.info("transmission_download_added", extra={"task_hash": task_hash, "category": category})
        return {"qb_task_id": task_hash, "category": category, "downloader": "transmission"}

    def _get(self, hash_value: str, fields: List[str]) -> Optional[Dict[str, Any]]:
        values = self._rpc("torrent-get", {"ids": [hash_value], "fields": fields}).get("torrents", [])
        return dict(values[0]) if values else None

    def torrent_info(self, hash_value: str) -> Optional[Dict[str, Any]]:
        value = self._get(
            hash_value,
            ["hashString", "name", "percentDone", "downloadDir", "status", "totalSize", "addedDate", "doneDate"],
        )
        if not value:
            return None
        save_path = str(value.get("downloadDir") or "")
        name = str(value.get("name") or "")
        return {
            **value,
            "hash": value.get("hashString"),
            "progress": value.get("percentDone", 0),
            "save_path": save_path,
            "content_path": str(PurePosixPath(save_path) / name) if save_path and name else save_path,
        }

    def files(self, hash_value: str) -> List[Dict[str, Any]]:
        value = self._get(hash_value, ["files", "fileStats"])
        if not value:
            return []
        stats = value.get("fileStats") or []
        result = []
        for index, item in enumerate(value.get("files") or []):
            stat = stats[index] if index < len(stats) else {}
            length = int(item.get("length") or 0)
            completed = int(item.get("bytesCompleted") or 0)
            result.append(
                {
                    "index": index,
                    "name": item.get("name"),
                    "size": length,
                    "progress": completed / length if length else 0,
                    "priority": 1 if stat.get("wanted", True) else 0,
                }
            )
        return result

    def _rename_path(self, hash_value: str, old_path: str, new_path: str) -> None:
        old = PurePosixPath(old_path)
        new = PurePosixPath(new_path)
        if old.parent != new.parent:
            raise UpstreamError("Transmission", "renaming across directories is not supported")
        self._rpc("torrent-rename-path", {"ids": [hash_value], "path": str(old), "name": new.name})

    def rename_file(self, hash_value: str, old_path: str, new_path: str) -> None:
        self._rename_path(hash_value, old_path, new_path)

    def rename_folder(self, hash_value: str, old_path: str, new_path: str) -> None:
        self._rename_path(hash_value, old_path, new_path)

    def rename_torrent(self, hash_value: str, name: str) -> None:
        # Files/folders are already renamed explicitly. Transmission has no safe display-name-only RPC.
        return None

    def set_category(self, hash_value: str, category: str) -> None:
        self._rpc("torrent-set", {"ids": [hash_value], "labels": [category]})
        self._rpc(
            "torrent-set-location",
            {
                "ids": [hash_value],
                "location": str(self.settings.download_path_for_category(category)),
                "move": True,
            },
        )

    def resume(self, hash_value: str) -> None:
        self._rpc("torrent-start", {"ids": [hash_value]})

    def status(self) -> Dict[str, Any]:
        if not self.configured:
            return {
                "client": "transmission",
                "configured": False,
                "connected": False,
                "version": None,
                "error": "TRANSMISSION_HOST is not set",
            }
        try:
            value = self._rpc("session-get")
            return {
                "client": "transmission",
                "configured": True,
                "connected": True,
                "version": value.get("version"),
                "error": None,
            }
        except (ServiceUnavailableError, UpstreamError) as exc:
            return {
                "client": "transmission",
                "configured": True,
                "connected": False,
                "version": None,
                "error": exc.detail,
            }

    def tasks(self) -> List[Dict[str, Any]]:
        fields = [
            "hashString",
            "name",
            "status",
            "percentDone",
            "totalSize",
            "downloadedEver",
            "rateDownload",
            "eta",
            "labels",
            "downloadDir",
            "addedDate",
            "doneDate",
        ]
        values = self._rpc("torrent-get", {"fields": fields}).get("torrents", [])
        return [
            {
                "hash": item.get("hashString"),
                "name": item.get("name"),
                "state": STATUS_NAMES.get(int(item.get("status") or 0), "unknown"),
                "progress": item.get("percentDone", 0),
                "size": item.get("totalSize", 0),
                "downloaded": item.get("downloadedEver", 0),
                "dlspeed": item.get("rateDownload", 0),
                "eta": item.get("eta"),
                "category": (item.get("labels") or [""])[0],
                "save_path": item.get("downloadDir"),
                "added_on": item.get("addedDate"),
                "completion_on": item.get("doneDate"),
            }
            for item in values
        ]
