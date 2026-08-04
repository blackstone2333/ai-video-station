from __future__ import annotations

import base64
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from .config import Settings
from .errors import ServiceUnavailableError, UpstreamError, ValidationAppError
from .torrent_meta import parse_torrent_metadata


logger = logging.getLogger(__name__)
MAGNET_HASH_RE = re.compile(r"(?i)[?&]xt=urn:btih:([a-z0-9]+)")
COMPLETED_STATES = {"uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP", "checkingUP"}
BROKEN_STATES = {"missingFiles", "error"}


def normalized_task_progress(item: Dict[str, Any]) -> float:
    """Resolve stale/missing downloader progress from independent completion fields."""
    try:
        progress = float(item.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0.0
    if item.get("state") in BROKEN_STATES:
        return max(0.0, min(1.0, progress))
    try:
        size = float(item.get("size") or 0)
        downloaded = float(item.get("downloaded") or 0)
    except (TypeError, ValueError):
        size = downloaded = 0.0
    if size > 0:
        progress = max(progress, downloaded / size)
    try:
        completed_at = float(item.get("completion_on") or 0)
    except (TypeError, ValueError):
        completed_at = 0.0
    if completed_at > 0 or item.get("state") in COMPLETED_STATES:
        progress = 1.0
    return max(0.0, min(1.0, progress))


def torrent_hash(download_link: str) -> Optional[str]:
    match = MAGNET_HASH_RE.search(download_link)
    if not match:
        return None
    value = match.group(1)
    if len(value) == 40:
        return value.lower()
    if len(value) == 32:
        try:
            return base64.b32decode(value.upper()).hex()
        except ValueError:
            return value.lower()
    return value.lower()


class QBittorrentClient:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.settings.qb_base_url)

    def _base_url(self) -> str:
        if not self.settings.qb_base_url:
            raise ServiceUnavailableError("qBittorrent is not configured; set QB_HOST and credentials")
        return self.settings.qb_base_url

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        base = self._base_url()
        try:
            response = self.session.request(
                method,
                f"{base}{path}",
                timeout=self.settings.request_timeout_seconds,
                verify=self.settings.qb_verify_ssl,
                **kwargs,
            )
            if response.status_code in {401, 403}:
                self.login()
                response = self.session.request(
                    method,
                    f"{base}{path}",
                    timeout=self.settings.request_timeout_seconds,
                    verify=self.settings.qb_verify_ssl,
                    **kwargs,
                )
            response.raise_for_status()
            return response
        except requests.Timeout as exc:
            raise UpstreamError("qBittorrent", "request timed out", timeout=True) from exc
        except requests.RequestException as exc:
            raise UpstreamError("qBittorrent", "connection failed") from exc

    def login(self) -> None:
        base = self._base_url()
        try:
            response = self.session.post(
                f"{base}/api/v2/auth/login",
                data={
                    "username": self.settings.qb_username,
                    "password": self.settings.qb_password.get_secret_value(),
                },
                timeout=self.settings.request_timeout_seconds,
                verify=self.settings.qb_verify_ssl,
            )
            response.raise_for_status()
            if response.text.strip() != "Ok.":
                raise ServiceUnavailableError("qBittorrent rejected the configured username or password")
        except requests.Timeout as exc:
            raise UpstreamError("qBittorrent", "login timed out", timeout=True) from exc
        except requests.RequestException as exc:
            raise UpstreamError("qBittorrent", "login failed") from exc

    @staticmethod
    def validate_download_link(download_link: str) -> str:
        value = download_link.strip()
        lowered = value.lower()
        if lowered.startswith(("magnet:?", "ed2k://")):
            return value
        if lowered.startswith(("http://", "https://")) and ".torrent" in lowered:
            return value
        raise ValidationAppError(
            "download_link must be a magnet, ed2k, or HTTP(S) .torrent URL",
            [{"field": "download_link", "message": "unsupported download scheme", "code": "UNSUPPORTED_LINK"}],
        )

    def categories(self) -> Dict[str, Any]:
        response = self._request("GET", "/api/v2/torrents/categories")
        value = response.json()
        return value if isinstance(value, dict) else {}

    def ensure_category(self, category: str) -> None:
        """Create a missing category without rewriting existing qB configuration."""
        if not category:
            return
        try:
            if category in self.categories():
                return
        except UpstreamError:
            logger.warning("qb_category_lookup_failed", extra={"category": category})
            return
        save_path = str(self.settings.download_path_for_category(category))
        try:
            self._request(
                "POST",
                "/api/v2/torrents/createCategory",
                data={"category": category, "savePath": save_path},
            )
        except UpstreamError:
            logger.warning("qb_category_create_failed", extra={"category": category})

    def _accepted_or_registered(self, response: requests.Response, hash_value: Optional[str]) -> bool:
        if response.text.strip() == "Ok.":
            return True
        if not hash_value:
            return False
        try:
            return self.torrent_info(hash_value) is not None
        except UpstreamError:
            return False

    def add_download(
        self,
        download_link: str,
        category: str,
        rename: Optional[str] = None,
        save_path: Optional[Any] = None,
    ) -> Dict[str, Any]:
        link = self.validate_download_link(download_link)
        self.login()
        self.ensure_category(category)
        payload = {
            "urls": link,
            "category": category,
            "savepath": str(save_path or self.settings.download_path_for_category(category)),
            "paused": "false",
            "autoTMM": "false",
        }
        if rename:
            payload["rename"] = rename
        task_hash = torrent_hash(link)
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data=payload,
        )
        if not self._accepted_or_registered(response, task_hash):
            raise UpstreamError("qBittorrent", "torrent was not accepted")
        logger.info("qb_download_added", extra={"task_hash": task_hash, "category": category})
        return {"qb_task_id": task_hash, "category": category}

    def add_torrent_file(
        self,
        content: bytes,
        filename: str,
        category: str,
        rename: Optional[str] = None,
        save_path: Optional[Any] = None,
    ) -> Dict[str, Any]:
        metadata = parse_torrent_metadata(content)
        self.login()
        self.ensure_category(category)
        payload = {
            "category": category,
            "savepath": str(save_path or self.settings.download_path_for_category(category)),
            "paused": "false",
            "autoTMM": "false",
        }
        if rename:
            payload["rename"] = rename
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data=payload,
            files={"torrents": (filename, content, "application/x-bittorrent")},
        )
        if not self._accepted_or_registered(response, metadata.info_hash):
            raise UpstreamError("qBittorrent", "torrent file was not accepted")
        logger.info("qb_torrent_file_added", extra={"task_hash": metadata.info_hash, "category": category})
        return {"qb_task_id": metadata.info_hash, "category": category, "torrent_name": metadata.name}

    def torrent_info(self, hash_value: str) -> Optional[Dict[str, Any]]:
        self.login()
        response = self._request("GET", "/api/v2/torrents/info", params={"hashes": hash_value})
        values = response.json()
        return values[0] if values else None

    def files(self, hash_value: str) -> List[Dict[str, Any]]:
        self.login()
        response = self._request("GET", "/api/v2/torrents/files", params={"hash": hash_value})
        return [
            {
                "index": item.get("index"),
                "name": item.get("name"),
                "size": item.get("size", 0),
                "progress": item.get("progress", 0),
                "priority": item.get("priority", 0),
            }
            for item in response.json()
        ]

    def rename_file(self, hash_value: str, old_path: str, new_path: str) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/renameFile",
            data={"hash": hash_value, "oldPath": old_path, "newPath": new_path},
        )

    def rename_folder(self, hash_value: str, old_path: str, new_path: str) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/renameFolder",
            data={"hash": hash_value, "oldPath": old_path, "newPath": new_path},
        )

    def rename_torrent(self, hash_value: str, name: str) -> None:
        self._request("POST", "/api/v2/torrents/rename", data={"hash": hash_value, "name": name})

    def set_category(self, hash_value: str, category: str) -> None:
        self.ensure_category(category)
        self._request("POST", "/api/v2/torrents/setCategory", data={"hashes": hash_value, "category": category})

    def resume(self, hash_value: str) -> None:
        self._request("POST", "/api/v2/torrents/resume", data={"hashes": hash_value})

    def recheck(self, hash_value: str) -> None:
        self._request("POST", "/api/v2/torrents/recheck", data={"hashes": hash_value})

    def set_location(self, hash_value: str, location: Any) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/setLocation",
            data={"hashes": hash_value, "location": str(location)},
        )

    def status(self) -> Dict[str, Any]:
        if not self.configured:
            return {
                "client": "qbittorrent",
                "configured": False,
                "connected": False,
                "version": None,
                "error": "QB_HOST is not set",
            }
        try:
            self.login()
            version = self._request("GET", "/api/v2/app/version").text.strip()
            return {
                "client": "qbittorrent",
                "configured": True,
                "connected": True,
                "version": version,
                "error": None,
            }
        except (ServiceUnavailableError, UpstreamError) as exc:
            return {
                "configured": True,
                "client": "qbittorrent",
                "connected": False,
                "version": None,
                "error": exc.detail if hasattr(exc, "detail") else str(exc),
            }

    def tasks(self) -> List[Dict[str, Any]]:
        self.login()
        response = self._request("GET", "/api/v2/torrents/info", params={"sort": "added_on", "reverse": "true"})
        values = response.json()
        fields = (
            "hash",
            "name",
            "state",
            "progress",
            "size",
            "downloaded",
            "dlspeed",
            "eta",
            "category",
            "save_path",
            "added_on",
            "completion_on",
        )
        tasks = [{field: item.get(field) for field in fields} for item in values]
        for task in tasks:
            task["progress"] = normalized_task_progress(task)
            task["completed"] = task["progress"] >= 0.999999
        return tasks
