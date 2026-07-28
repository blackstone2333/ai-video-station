from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from .config import Settings
from .crawler import SixVClient
from .errors import AppError, NotFoundError, ValidationAppError
from .medialib import MediaLibraryService
from .naming import NamingJobRepository, NamingService
from .qbittorrent import QBittorrentClient
from .quality import Release, canonical_media_name, detect_episode, detect_season, link_display_name
from .watchlist import WatchlistRepository, utc_now_iso


logger = logging.getLogger(__name__)


class ResultCache:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self.ttl_seconds = ttl_seconds
        self._values: Dict[str, tuple[float, Release]] = {}
        self._lock = threading.RLock()

    def put_all(self, releases: Iterable[Release]) -> None:
        expires_at = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._prune()
            for release in releases:
                self._values[release.id] = (expires_at, release)

    def get(self, result_id: str) -> Optional[Release]:
        with self._lock:
            self._prune()
            value = self._values.get(result_id)
            return value[1] if value else None

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._values.items() if expires_at <= now]
        for key in expired:
            self._values.pop(key, None)


@dataclass
class SearchOutcome:
    keyword: str
    media_type: str
    releases: List[Release]


class SearchService:
    def __init__(self, crawler: SixVClient, cache: ResultCache) -> None:
        self.crawler = crawler
        self.cache = cache

    def search(self, keyword: str, media_type: str) -> SearchOutcome:
        releases = self.crawler.search(keyword, media_type)
        self.cache.put_all(releases)
        resolved = media_type
        release_types = {item.media_type for item in releases}
        if media_type == "auto" and len(release_types) == 1:
            resolved = next(iter(release_types))
        return SearchOutcome(keyword=keyword, media_type=resolved, releases=releases)


class DownloadService:
    def __init__(
        self,
        settings: Settings,
        qb: QBittorrentClient,
        cache: ResultCache,
        naming: Optional[NamingService] = None,
    ) -> None:
        self.settings = settings
        self.qb = qb
        self.cache = cache
        self.naming = naming

    @staticmethod
    def _fallback_release(
        result_id: str,
        download_link: str,
        title: str,
        media_type: str,
    ) -> Release:
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", title)
        season = detect_season(title) if media_type == "tv" else None
        return Release(
            id=result_id,
            title=title,
            url="",
            download_link=download_link,
            size=None,
            resolution=None,
            source=None,
            language=None,
            hdr=None,
            encoding=None,
            media_type=media_type,
            episode=detect_episode(title) if media_type == "tv" else None,
            media_name=canonical_media_name(title),
            year=int(year_match.group(1)) if year_match else None,
            season=season,
            link_name=link_display_name(download_link, title),
        )

    def download(
        self,
        result_id: str,
        download_link: str,
        title: str,
        media_type: str,
    ) -> Dict[str, Any]:
        cached = self.cache.get(result_id)
        if cached and cached.download_link != download_link:
            raise ValidationAppError(
                "download_link does not match the selected search result",
                [{"field": "download_link", "message": "result mismatch", "code": "RESULT_MISMATCH"}],
            )
        resolved_type = cached.media_type if cached else media_type
        if resolved_type == "auto":
            resolved_type = "movie"
        category = self.settings.qb_tv_category if resolved_type == "tv" else self.settings.qb_movie_category
        release = cached or self._fallback_release(result_id, download_link, title, resolved_type)
        result = self.naming.add_download(release, category) if self.naming else self.qb.add_download(download_link, category)
        return {**result, "title": title, "type": resolved_type}


class WatchlistService:
    def __init__(
        self,
        settings: Settings,
        repository: WatchlistRepository,
        search: SearchService,
        qb: QBittorrentClient,
        naming: Optional[NamingService] = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.search_service = search
        self.qb = qb
        self.naming = naming
        self._check_lock = threading.Lock()

    def _is_expired(self, item: Dict[str, Any]) -> bool:
        try:
            added = datetime.fromisoformat(item["added_at"])
        except (KeyError, TypeError, ValueError):
            return False
        if added.tzinfo is None:
            added = added.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - added >= timedelta(days=self.settings.watchlist_expire_days)

    @staticmethod
    def _select_tv_releases(releases: List[Release], downloaded_episodes: set[str]) -> List[Release]:
        selected: List[Release] = []
        episode_seen = set()
        bundle_selected = False
        for release in releases:
            if release.episode:
                if release.episode in downloaded_episodes or release.episode in episode_seen:
                    continue
                episode_seen.add(release.episode)
                selected.append(release)
            elif not bundle_selected:
                selected.append(release)
                bundle_selected = True
        return selected

    def _check_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now_iso()
        base_changes: Dict[str, Any] = {
            "last_check": now,
            "check_count": int(item.get("check_count", 0)) + 1,
            "last_error": None,
        }
        try:
            outcome = self.search_service.search(item["keyword"], item["type"])
            releases = outcome.releases
            if not releases:
                status = item.get("status", "pending")
                if item["type"] == "movie":
                    status = "expired" if self._is_expired(item) else "pending"
                elif item.get("downloaded_links"):
                    status = "monitoring"
                base_changes["status"] = status
                updated = self.repository.update(item["id"], base_changes)
                return {"item": updated, "found": 0, "downloaded": 0}

            resolved_type = item["type"]
            if resolved_type == "auto":
                resolved_type = outcome.media_type if outcome.media_type != "auto" else releases[0].media_type
                base_changes["type"] = resolved_type

            downloaded_links = set(item.get("downloaded_links", []))
            downloaded_episodes = set(item.get("downloaded_episodes", []))
            if resolved_type == "tv":
                candidates = self._select_tv_releases(releases, downloaded_episodes)
                category = self.settings.qb_tv_category
            else:
                candidates = releases[:1]
                category = self.settings.qb_movie_category

            added = 0
            for release in candidates:
                if release.id in downloaded_links:
                    continue
                if self.naming:
                    self.naming.add_download(release, category)
                else:
                    self.qb.add_download(release.download_link, category)
                downloaded_links.add(release.id)
                if release.episode:
                    downloaded_episodes.add(release.episode)
                added += 1

            base_changes.update(
                {
                    "downloaded_links": sorted(downloaded_links),
                    "downloaded_episodes": sorted(downloaded_episodes),
                    "status": "monitoring" if resolved_type == "tv" else "found",
                    "found_at": item.get("found_at") or now,
                }
            )
            updated = self.repository.update(item["id"], base_changes)
            return {"item": updated, "found": len(releases), "downloaded": added}
        except AppError as exc:
            base_changes["last_error"] = exc.detail
            updated = self.repository.update(item["id"], base_changes)
            logger.warning(
                "watchlist_item_failed",
                extra={"item_id": item["id"], "error_code": exc.code},
            )
            return {"item": updated, "found": 0, "downloaded": 0, "error": exc.detail}
        except Exception as exc:
            base_changes["last_error"] = "unexpected check failure"
            updated = self.repository.update(item["id"], base_changes)
            logger.exception("watchlist_item_unexpected_failure", extra={"item_id": item["id"]})
            return {"item": updated, "found": 0, "downloaded": 0, "error": str(exc)}

    def check(self, item_id: Optional[str] = None) -> Dict[str, Any]:
        if not self._check_lock.acquire(blocking=False):
            return {"running": True, "checked": 0, "downloaded": 0, "results": []}
        try:
            if item_id:
                items = [self.repository.get(item_id)]
            else:
                items = self.repository.list()
            results = [self._check_item(item) for item in items]
            return {
                "running": False,
                "checked": len(results),
                "downloaded": sum(item.get("downloaded", 0) for item in results),
                "results": results,
            }
        finally:
            self._check_lock.release()


@dataclass
class AppServices:
    crawler: SixVClient
    qb: QBittorrentClient
    watchlist: WatchlistRepository
    cache: ResultCache
    search: SearchService
    download: DownloadService
    watchlist_service: WatchlistService
    naming_jobs: Optional[NamingJobRepository] = None
    naming: Optional[NamingService] = None


def build_services(settings: Settings) -> AppServices:
    crawler = SixVClient(settings)
    qb = QBittorrentClient(settings)
    watchlist = WatchlistRepository(settings.watchlist_path)
    cache = ResultCache()
    search = SearchService(crawler, cache)
    naming_jobs = NamingJobRepository(settings.naming_jobs_path)
    hardlinker = MediaLibraryService(settings)
    naming = NamingService(settings, naming_jobs, qb, hardlinker=hardlinker)
    download = DownloadService(settings, qb, cache, naming)
    watchlist_service = WatchlistService(settings, watchlist, search, qb, naming)
    return AppServices(crawler, qb, watchlist, cache, search, download, watchlist_service, naming_jobs, naming)
