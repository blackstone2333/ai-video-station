from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

from .config import Settings
from .crawler import SixVClient
from .errors import AppError, NotFoundError, ValidationAppError
from .download_records import DismissedDownloadRepository
from .medialib import MediaLibraryService
from .media_policies import policy_for
from .naming import NamingJobRepository, NamingService
from .path_rules import PathRuleRepository
from .path_settings import PathSettingsRepository
from .quality import (
    Release,
    canonical_media_name,
    detect_episode,
    detect_season,
    infer_media_type,
    link_display_name,
    build_release,
    release_id,
)
from .resource_preferences import profile_for, rank_releases
from .torrent_meta import parse_torrent_metadata
from .watchlist import WatchlistRepository, utc_now_iso
from .sites import ProviderRegistry, SiteRepository
from .transmission import TransmissionClient
from .runtime_settings import (
    DownloaderManager,
    DownloaderSettingsRepository,
    SystemSettingsRepository,
)
from .agent_access import AgentAccessRepository
from .state import StateStore, StateStoreError
from .cleanup import CleanupPlanRepository, CleanupService


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
    def __init__(self, crawler: Any, cache: ResultCache) -> None:
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
        qb: Any,
        cache: ResultCache,
        naming: Optional[NamingService] = None,
        path_rules: Optional[PathRuleRepository] = None,
    ) -> None:
        self.settings = settings
        self.qb = qb
        self.cache = cache
        self.naming = naming
        self.path_rules = path_rules or getattr(naming, "path_rules", None)

    def _route_for(self, media_type: str, path_rule_id: Optional[str]) -> Optional[Dict[str, Any]]:
        return self.path_rules.resolve(media_type, path_rule_id) if self.path_rules else None

    @staticmethod
    def _fallback_release(
        result_id: str,
        download_link: str,
        title: str,
        media_type: str,
    ) -> Release:
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", title)
        resolved_type = infer_media_type(title, download_link, media_type)
        policy = policy_for(resolved_type)
        season = detect_season(title) if policy.episodic else None
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
            media_type=resolved_type,
            episode=detect_episode(title) if policy.episodic else None,
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
        path_rule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        cached = self.cache.get(result_id)
        if cached and cached.download_link != download_link:
            raise ValidationAppError(
                "download_link does not match the selected search result",
                [{"field": "download_link", "message": "result mismatch", "code": "RESULT_MISMATCH"}],
            )
        resolved_type = cached.media_type if cached else infer_media_type(title, download_link, media_type)
        if resolved_type == "auto":
            resolved_type = infer_media_type(title, download_link, media_type)
        if resolved_type == "auto":
            raise ValidationAppError(
                "media type could not be detected; choose movie, tv, anime, or custom",
                [{"field": "type", "message": "explicit media type required", "code": "TYPE_REQUIRED"}],
            )
        category = self._category_for(resolved_type)
        release = cached or self._fallback_release(result_id, download_link, title, resolved_type)
        route = self._route_for(resolved_type, path_rule_id)
        result = (
            self.naming.add_download(release, category, path_rule_id=route["id"] if route else None)
            if self.naming
            else (self.qb.add_download(download_link, category, save_path=route["downloader_path"]) if route else self.qb.add_download(download_link, category))
        )
        return {**result, "title": title, "type": resolved_type, "path_rule_id": route["id"] if route else None}

    def _category_for(self, media_type: str) -> str:
        return policy_for(media_type).category_for(self.settings)

    @staticmethod
    def _manual_type(title: str, source_name: str, media_type: str) -> str:
        return infer_media_type(f"{title} {source_name}", source_name, media_type)

    @staticmethod
    def _require_manual_type(resolved_type: str) -> str:
        if resolved_type == "auto":
            raise ValidationAppError(
                "无法可靠识别媒体类型，请选择电影、电视剧、动漫或自定义后再下载",
                [
                    {
                        "field": "type",
                        "message": "请选择明确的媒体类型",
                        "code": "TYPE_REQUIRED",
                    }
                ],
            )
        return resolved_type

    @staticmethod
    def _with_manual_metadata(
        release: Release,
        original_title: Optional[str],
        edition: Optional[str],
        episode_title: Optional[str],
    ) -> Release:
        return replace(
            release,
            original_title=original_title.strip() if original_title else release.original_title,
            edition=edition.strip() if edition else release.edition,
            episode_title=episode_title.strip() if episode_title else release.episode_title,
        )

    def manual_link(
        self,
        download_link: str,
        title: Optional[str],
        media_type: str,
        original_title: Optional[str] = None,
        edition: Optional[str] = None,
        episode_title: Optional[str] = None,
        path_rule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        provided_title = (title or "").strip()
        if provided_title.casefold() in {"手动下载", "manual download"}:
            provided_title = ""
        source_name = link_display_name(download_link, "").strip()
        if not provided_title and not source_name:
            raise ValidationAppError(
                "磁力链接不包含可识别的资源名称，请填写标题后再下载",
                [
                    {
                        "field": "title",
                        "message": "链接缺少 dn 资源名时必须填写标题",
                        "code": "TITLE_REQUIRED_FOR_NAMELESS_LINK",
                    }
                ],
            )
        selected_title = provided_title or source_name
        source_name = source_name or selected_title
        resolved_type = self._require_manual_type(
            self._manual_type(selected_title, source_name, media_type)
        )
        result_id = release_id("manual", download_link)
        release = build_release(selected_title, source_name, "", download_link, resolved_type)
        if release is None:
            release = self._fallback_release(result_id, download_link, selected_title, resolved_type)
        release = self._with_manual_metadata(release, original_title, edition, episode_title)
        category = self._category_for(resolved_type)
        route = self._route_for(resolved_type, path_rule_id)
        result = (
            self.naming.add_download(release, category, path_rule_id=route["id"] if route else None)
            if self.naming
            else (self.qb.add_download(download_link, category, save_path=route["downloader_path"]) if route else self.qb.add_download(download_link, category))
        )
        return {**result, "title": selected_title, "type": resolved_type, "source_name": source_name, "path_rule_id": route["id"] if route else None}

    def preview_link(
        self,
        download_link: str,
        title: Optional[str],
        media_type: str,
        original_title: Optional[str] = None,
        edition: Optional[str] = None,
        episode_title: Optional[str] = None,
        path_rule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        provided_title = (title or "").strip()
        if provided_title.casefold() in {"手动下载", "manual download"}:
            provided_title = ""
        source_name = link_display_name(download_link, "").strip()
        if not provided_title and not source_name:
            raise ValidationAppError(
                "磁力链接不包含可识别的资源名称，请填写标题后再继续",
                [{"field": "title", "message": "链接缺少 dn 资源名时必须填写标题", "code": "TITLE_REQUIRED_FOR_NAMELESS_LINK"}],
            )
        selected_title = provided_title or source_name
        source_name = source_name or selected_title
        resolved_type = self._manual_type(selected_title, source_name, media_type)
        if resolved_type == "auto":
            return {
                "ready": False,
                "requires_media_type": True,
                "title": selected_title,
                "type": "auto",
                "source_name": source_name,
                "naming": None,
            }
        release = build_release(selected_title, source_name, "", download_link, resolved_type)
        if release is None:
            release = self._fallback_release(
                release_id("manual", download_link), download_link, selected_title, resolved_type
            )
        release = self._with_manual_metadata(release, original_title, edition, episode_title)
        category = self._category_for(resolved_type)
        route = self._route_for(resolved_type, path_rule_id)
        naming = (
            self.naming.preview_plan(
                release,
                category,
                path_rule_id=route["id"] if route else None,
            )
            if self.naming
            else None
        )
        return {
            "ready": True,
            "requires_media_type": False,
            "title": selected_title,
            "type": resolved_type,
            "source_name": source_name,
            "path_rule_id": route["id"] if route else None,
            "naming": naming,
        }

    def manual_torrent(
        self,
        content: bytes,
        filename: str,
        title: Optional[str],
        media_type: str,
        original_title: Optional[str] = None,
        edition: Optional[str] = None,
        episode_title: Optional[str] = None,
        path_rule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        metadata = parse_torrent_metadata(content)
        selected_title = (title or metadata.name).strip()
        resolved_type = self._require_manual_type(
            self._manual_type(selected_title, metadata.name, media_type)
        )
        magnet = f"magnet:?xt=urn:btih:{metadata.info_hash}&dn={quote(metadata.name, safe='')}"
        release = build_release(selected_title, metadata.name, "", magnet, resolved_type)
        if release is None:
            release = self._fallback_release(metadata.info_hash[:16], magnet, selected_title, resolved_type)
        release = self._with_manual_metadata(release, original_title, edition, episode_title)
        category = self._category_for(resolved_type)
        route = self._route_for(resolved_type, path_rule_id)
        if self.naming:
            result = self.naming.add_torrent_file(
                release,
                category,
                content,
                filename,
                metadata.info_hash,
                path_rule_id=route["id"] if route else None,
            )
        else:
            result = self.qb.add_torrent_file(content, filename, category, save_path=route["downloader_path"]) if route else self.qb.add_torrent_file(content, filename, category)
        return {**result, "title": selected_title, "type": resolved_type, "source_name": metadata.name, "path_rule_id": route["id"] if route else None}

    def preview_torrent(
        self,
        content: bytes,
        filename: str,
        title: Optional[str],
        media_type: str,
        original_title: Optional[str] = None,
        edition: Optional[str] = None,
        episode_title: Optional[str] = None,
        path_rule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        metadata = parse_torrent_metadata(content)
        selected_title = (title or metadata.name).strip()
        resolved_type = self._manual_type(selected_title, metadata.name, media_type)
        if resolved_type == "auto":
            return {
                "ready": False,
                "requires_media_type": True,
                "title": selected_title,
                "type": "auto",
                "source_name": metadata.name,
                "naming": None,
            }
        magnet = f"magnet:?xt=urn:btih:{metadata.info_hash}&dn={quote(metadata.name, safe='')}"
        release = build_release(selected_title, metadata.name, "", magnet, resolved_type)
        if release is None:
            release = self._fallback_release(metadata.info_hash[:16], magnet, selected_title, resolved_type)
        release = self._with_manual_metadata(release, original_title, edition, episode_title)
        category = self._category_for(resolved_type)
        route = self._route_for(resolved_type, path_rule_id)
        naming = (
            self.naming.preview_plan(
                release,
                category,
                path_rule_id=route["id"] if route else None,
            )
            if self.naming
            else None
        )
        return {
            "ready": True,
            "requires_media_type": False,
            "title": selected_title,
            "type": resolved_type,
            "source_name": metadata.name,
            "path_rule_id": route["id"] if route else None,
            "naming": naming,
        }


class WatchlistService:
    def __init__(
        self,
        settings: Settings,
        repository: WatchlistRepository,
        search: SearchService,
        qb: Any,
        naming: Optional[NamingService] = None,
        path_rules: Optional[PathRuleRepository] = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.search_service = search
        self.qb = qb
        self.naming = naming
        self.path_rules = path_rules or getattr(naming, "path_rules", None)
        self._check_lock = threading.Lock()

    def _category_for(self, media_type: str) -> str:
        return policy_for(media_type).category_for(self.settings)

    def add(
        self,
        keyword: str,
        media_type: str,
        path_rule_id: Optional[str] = None,
        viewing_mode: str = "daily",
        resource_preferences: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create an idempotent subscription and pin a selected route."""
        route = None
        if self.path_rules and path_rule_id:
            # ``auto`` cannot be type-checked until a result arrives, but the
            # explicit id is still captured and enforced at that point.
            route = self.path_rules.get(path_rule_id) if media_type == "auto" else self.path_rules.resolve(media_type, path_rule_id)
            if not route["enabled"]:
                raise ValidationAppError(
                    "路径规则已禁用",
                    [{"field": "path_rule_id", "message": "请启用规则或选择其他规则", "code": "PATH_RULE_DISABLED"}],
                )
        elif self.path_rules and media_type != "auto":
            route = self.path_rules.resolve(media_type)
        preferences = profile_for(viewing_mode, resource_preferences).to_dict()
        return self.repository.add(
            keyword,
            media_type,
            route["id"] if route else None,
            route,
            viewing_mode,
            preferences,
        )

    def _is_expired(self, item: Dict[str, Any]) -> bool:
        try:
            added = datetime.fromisoformat(item["added_at"])
        except (KeyError, TypeError, ValueError):
            return False
        if added.tzinfo is None:
            added = added.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - added >= timedelta(days=self.settings.watchlist_expire_days)

    def update(
        self,
        item_id: str,
        viewing_mode: Optional[str] = None,
        resource_preferences: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        item = self.repository.get(item_id)
        mode = viewing_mode or item.get("viewing_mode") or "daily"
        if resource_preferences is not None:
            snapshot = resource_preferences
        elif viewing_mode is not None:
            # Selecting a preset is an explicit reset to that preset. Keeping
            # the old snapshot here would make the UI label change while the
            # previous resolution/source order silently remained active.
            snapshot = None
        else:
            snapshot = item.get("resource_preferences")
        return self.repository.update(
            item_id,
            {
                "viewing_mode": mode,
                "resource_preferences": profile_for(mode, snapshot).to_dict(),
            },
        )

    @staticmethod
    def _select_tv_releases(
        releases: List[Release], downloaded_episodes: set[str]
    ) -> List[tuple[Release, Optional[List[str]]]]:
        selected: List[tuple[Release, Optional[List[str]]]] = []
        episode_seen = set()
        bundle_selected = False
        for release in releases:
            if release.episodes:
                missing = [episode for episode in release.episodes if episode not in downloaded_episodes and episode not in episode_seen]
                if missing and not bundle_selected:
                    selected.append((release, missing))
                    episode_seen.update(missing)
                    bundle_selected = True
                continue
            if release.episode:
                if release.episode in downloaded_episodes or release.episode in episode_seen:
                    continue
                episode_seen.add(release.episode)
                selected.append((release, None))
            elif not bundle_selected:
                selected.append((release, None))
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
            episode_sources = dict(item.get("episode_sources") or {})
            viewing_mode = item.get("viewing_mode")
            ranked_releases = (
                rank_releases(releases, profile_for(viewing_mode, item.get("resource_preferences")))
                if viewing_mode
                else releases
            )
            # A subscription pins its initial route.  Older records are upgraded
            # on first successful resolution so changing defaults is predictable.
            route = self.path_rules.resolve(resolved_type, item.get("path_rule_id")) if self.path_rules else None
            if route and item.get("path_rule_id") != route["id"]:
                base_changes["path_rule_id"] = route["id"]
                base_changes["path_rule_snapshot"] = route
            if policy_for(resolved_type).episodic:
                candidates = self._select_tv_releases(ranked_releases, downloaded_episodes)
            else:
                candidates = [(ranked_releases[0], None)]
            category = self._category_for(resolved_type)

            added = 0
            for release, wanted_episodes in candidates:
                if release.id in downloaded_links:
                    continue
                try:
                    if self.naming:
                        submission = self.naming.add_download(
                            release,
                            category,
                            path_rule_id=route["id"] if route else None,
                            wanted_episodes=wanted_episodes,
                        )
                    else:
                        if route:
                            submission = self.qb.add_download(release.download_link, category, save_path=route["downloader_path"])
                        else:
                            submission = self.qb.add_download(release.download_link, category)
                except AppError as exc:
                    failed_changes = {
                        **base_changes,
                        "downloaded_links": sorted(downloaded_links),
                            "downloaded_episodes": sorted(downloaded_episodes),
                            "episode_sources": episode_sources,
                        "status": (
                            "monitoring"
                            if policy_for(resolved_type).episodic
                            else item.get("status", "pending")
                        ),
                        "found_at": item.get("found_at") or (now if downloaded_links else None),
                        "last_error": exc.detail,
                    }
                    updated = self.repository.update(item["id"], failed_changes)
                    logger.warning(
                        "watchlist_item_failed",
                        extra={"item_id": item["id"], "error_code": exc.code},
                    )
                    return {
                        "item": updated,
                        "found": len(releases),
                        "downloaded": added,
                        "error": exc.detail,
                    }
                downloaded_links.add(release.id)
                effective_episodes = wanted_episodes or ([release.episode] if release.episode else [])
                for episode in effective_episodes:
                    downloaded_episodes.add(episode)
                    episode_sources[episode] = {
                        "release_id": release.id,
                        "task_id": submission.get("qb_task_id"),
                        "naming_job_id": submission.get("naming_job_id"),
                        "resolution": release.resolution,
                        "source": release.source,
                    }
                added += 1
                self.repository.update(
                    item["id"],
                    {
                        **base_changes,
                        "downloaded_links": sorted(downloaded_links),
                        "downloaded_episodes": sorted(downloaded_episodes),
                        "episode_sources": episode_sources,
                        "status": "monitoring" if policy_for(resolved_type).episodic else "found",
                        "found_at": item.get("found_at") or now,
                    },
                )

            base_changes.update(
                {
                    "downloaded_links": sorted(downloaded_links),
                    "downloaded_episodes": sorted(downloaded_episodes),
                    "episode_sources": episode_sources,
                    "status": "monitoring" if policy_for(resolved_type).episodic else "found",
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
    crawler: Any
    qb: Any
    watchlist: WatchlistRepository
    cache: ResultCache
    search: SearchService
    download: DownloadService
    watchlist_service: WatchlistService
    naming_jobs: Optional[NamingJobRepository] = None
    naming: Optional[NamingService] = None
    sites: Optional[SiteRepository] = None
    path_settings: Optional[PathSettingsRepository] = None
    path_rules: Optional[PathRuleRepository] = None
    downloader_settings: Optional[DownloaderSettingsRepository] = None
    system_settings: Optional[SystemSettingsRepository] = None
    agents: Optional[AgentAccessRepository] = None
    dismissed_downloads: Optional[DismissedDownloadRepository] = None
    state_store: Optional[StateStore] = None
    cleanup: Optional[CleanupService] = None


def build_services(settings: Settings) -> AppServices:
    try:
        state_store = StateStore(settings.state_db_path)
    except StateStoreError as exc:
        raise AppError("State Error", f"could not open persistent application state: {exc}", "state-error", 500) from exc
    path_settings = PathSettingsRepository(settings, state_store)
    downloader_settings = DownloaderSettingsRepository(settings, state_store)
    system_settings = SystemSettingsRepository(settings, state_store)
    path_rules = PathRuleRepository(settings, state_store)
    agents = AgentAccessRepository(settings, state_store)
    dismissed_downloads = DismissedDownloadRepository(settings.dismissed_downloads_path, state_store)
    default_site = {
        "id": "sixv",
        "name": "6v",
        "adapter": "sixv",
        "enabled": True,
        "base_urls": settings.site_urls,
        "address_page": settings.sixv_address_page,
        "default_type": "auto",
    }
    sites = SiteRepository(settings.sites_path, default_site, state_store)
    crawler = ProviderRegistry(settings, sites)
    qb = DownloaderManager(settings)
    watchlist = WatchlistRepository(settings.watchlist_path, state_store)
    cache = ResultCache()
    search = SearchService(crawler, cache)
    naming_jobs = NamingJobRepository(settings.naming_jobs_path, state_store)
    hardlinker = MediaLibraryService(settings, path_rules=path_rules)
    naming = NamingService(settings, naming_jobs, qb, hardlinker=hardlinker, path_rules=path_rules)
    cleanup = CleanupService(settings, CleanupPlanRepository(state_store), path_rules, naming_jobs, qb)
    download = DownloadService(settings, qb, cache, naming, path_rules)
    watchlist_service = WatchlistService(settings, watchlist, search, qb, naming, path_rules)
    return AppServices(
        crawler=crawler,
        qb=qb,
        watchlist=watchlist,
        cache=cache,
        search=search,
        download=download,
        watchlist_service=watchlist_service,
        naming_jobs=naming_jobs,
        naming=naming,
        sites=sites,
        path_settings=path_settings,
        path_rules=path_rules,
        downloader_settings=downloader_settings,
        system_settings=system_settings,
        agents=agents,
        dismissed_downloads=dismissed_downloads,
        state_store=state_store,
        cleanup=cleanup,
    )
