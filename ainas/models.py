from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


MediaType = Literal["movie", "tv", "anime", "custom", "auto"]
ViewingMode = Literal["daily", "collection", "compact"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SearchRequest(StrictModel):
    keyword: str = Field(min_length=1, max_length=100)
    media_type: MediaType = Field(default="auto", alias="type")
    add_to_watchlist: bool = Field(
        default=False,
        validation_alias=AliasChoices("add_to_watchlist", "addto_watchlist", "addToWatchlist"),
    )
    viewing_mode: ViewingMode = "daily"

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, value: str) -> str:
        value = value.strip().strip("《》")
        if not value:
            raise ValueError("keyword cannot be blank")
        return value


class DownloadRequest(StrictModel):
    result_id: str = Field(min_length=8, max_length=64)
    download_link: str = Field(min_length=8, max_length=8192)
    title: str = Field(min_length=1, max_length=500)
    media_type: MediaType = Field(default="auto", alias="type")
    path_rule_id: Optional[str] = Field(default=None, min_length=8, max_length=64)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()


class ManualDownloadRequest(StrictModel):
    download_link: str = Field(min_length=8, max_length=8192)
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    media_type: MediaType = Field(default="auto", alias="type")
    original_title: Optional[str] = Field(default=None, max_length=300)
    edition: Optional[str] = Field(default=None, max_length=100)
    episode_title: Optional[str] = Field(default=None, max_length=300)
    path_rule_id: Optional[str] = Field(default=None, min_length=8, max_length=64)
    subscribe: bool = False
    viewing_mode: ViewingMode = "daily"


class ManualTorrentRequest(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    media_type: MediaType = Field(default="auto", alias="type")
    original_title: Optional[str] = Field(default=None, max_length=300)
    edition: Optional[str] = Field(default=None, max_length=100)
    episode_title: Optional[str] = Field(default=None, max_length=300)
    path_rule_id: Optional[str] = Field(default=None, min_length=8, max_length=64)
    subscribe: bool = False
    viewing_mode: ViewingMode = "daily"


class WatchlistAddRequest(StrictModel):
    keyword: str = Field(min_length=1, max_length=100)
    media_type: MediaType = Field(default="auto", alias="type")
    path_rule_id: Optional[str] = Field(default=None, min_length=8, max_length=64)
    viewing_mode: ViewingMode = "daily"
    resource_preferences: Optional[Dict[str, Any]] = None

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, value: str) -> str:
        value = value.strip().strip("《》")
        if not value:
            raise ValueError("keyword cannot be blank")
        return value


class WatchlistCheckRequest(StrictModel):
    item_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


class WatchlistPatchRequest(StrictModel):
    viewing_mode: Optional[ViewingMode] = None
    resource_preferences: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def require_change(self) -> "WatchlistPatchRequest":
        if not self.model_fields_set:
            raise ValueError("at least one watchlist setting is required")
        return self


class NamingCheckRequest(StrictModel):
    job_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


class DownloaderRelocateRequest(StrictModel):
    location: Path

    @field_validator("location")
    @classmethod
    def absolute_location(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("location must be an absolute downloader path")
        return value


class SitePatchRequest(StrictModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    enabled: Optional[bool] = None
    base_urls: Optional[List[str]] = Field(default=None, min_length=1, max_length=20)
    address_page: Optional[str] = None
    search_url: Optional[str] = None
    result_selector: Optional[str] = None
    title_selector: Optional[str] = None
    link_selector: Optional[str] = None
    download_selector: Optional[str] = None
    default_type: Optional[Literal["auto", "movie", "tv", "anime", "custom"]] = None
    tv_path_patterns: Optional[List[str]] = None
    anime_path_patterns: Optional[List[str]] = None
    allow_private_hosts: Optional[bool] = None


class PathSettingsPatchRequest(StrictModel):
    qb_movie_category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    qb_tv_category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    qb_anime_category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    qb_custom_category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    downloads_base_path: Optional[Path] = None
    download_movie_path: Optional[Path] = None
    download_tv_path: Optional[Path] = None
    download_anime_path: Optional[Path] = None
    download_custom_path: Optional[Path] = None
    medialib_movie_path: Optional[Path] = None
    medialib_tv_path: Optional[Path] = None
    medialib_anime_path: Optional[Path] = None
    medialib_custom_path: Optional[Path] = None
    medialib_hardlink_enabled: Optional[bool] = None

    @model_validator(mode="after")
    def require_change(self) -> "PathSettingsPatchRequest":
        if not self.model_fields_set or all(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("at least one path setting is required")
        return self


class PathRulePatchRequest(StrictModel):
    media_type: Optional[Literal["movie", "tv", "anime", "custom"]] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    source_path: Optional[Path] = None
    downloader_path: Optional[Path] = None
    target_path: Optional[Path] = None
    enabled: Optional[bool] = None
    rename_enabled: Optional[bool] = None
    default_download: Optional[bool] = None

    @model_validator(mode="after")
    def require_change(self) -> "PathRulePatchRequest":
        if not self.model_fields_set:
            raise ValueError("at least one path rule setting is required")
        return self


class PathRulePreflightRequest(StrictModel):
    rule_id: str = Field(min_length=8, max_length=64)


class PathRuleDeleteRequest(StrictModel):
    disable_media_path: bool = False


class PaginationParams(StrictModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class NamingJobFilterRequest(PaginationParams):
    status: Optional[
        Literal[
            "submitting",
            "awaiting_binding",
            "pending",
            "waiting_metadata",
            "waiting_download",
            "waiting_selection",
            "retrying",
            "missing_in_downloader",
            "completed",
            "failed",
        ]
    ] = None
    media_type: Optional[Literal["movie", "tv", "anime", "custom"]] = None
    query: Optional[str] = Field(default=None, max_length=200)


class NamingPlanOverrideRequest(StrictModel):
    media_type: Optional[Literal["movie", "tv", "anime", "custom"]] = None
    media_name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    original_title: Optional[str] = Field(default=None, max_length=300)
    year: Optional[int] = Field(default=None, ge=1888, le=2100)
    season: Optional[int] = Field(default=None, ge=0, le=999)
    episode: Optional[str] = Field(default=None, max_length=100)
    episode_title: Optional[str] = Field(default=None, max_length=300)
    edition: Optional[str] = Field(default=None, max_length=100)
    part: Optional[str] = Field(default=None, max_length=100)
    video_format: Optional[str] = Field(default=None, max_length=100)
    rename_enabled: Optional[bool] = None

    @model_validator(mode="after")
    def require_override(self) -> "NamingPlanOverrideRequest":
        if not (self.model_fields_set - {"job_id"}):
            raise ValueError("at least one naming override is required")
        return self


class NamingPlanPreviewRequest(StrictModel):
    media_type: Literal["movie", "tv", "anime", "custom"]
    source_name: str = Field(min_length=1, max_length=500)
    path_rule_id: Optional[str] = Field(default=None, min_length=8, max_length=64)
    media_name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    original_title: Optional[str] = Field(default=None, max_length=300)
    year: Optional[int] = Field(default=None, ge=1888, le=2100)
    season: Optional[int] = Field(default=None, ge=0, le=999)
    episode: Optional[str] = Field(default=None, max_length=100)
    episode_title: Optional[str] = Field(default=None, max_length=300)
    edition: Optional[str] = Field(default=None, max_length=100)
    rename_enabled: Optional[bool] = None


class NamingPlanApplyRequest(NamingPlanOverrideRequest):
    job_id: str = Field(min_length=8, max_length=64)


class DownloaderSettingsPatchRequest(StrictModel):
    downloader_type: Optional[Literal["qbittorrent", "transmission"]] = None
    qb_host: Optional[str] = Field(default=None, max_length=500)
    qb_port: Optional[int] = Field(default=None, ge=1, le=65535)
    qb_username: Optional[str] = Field(default=None, max_length=200)
    qb_password: Optional[str] = Field(default=None, max_length=1000)
    qb_use_https: Optional[bool] = None
    qb_verify_ssl: Optional[bool] = None
    transmission_host: Optional[str] = Field(default=None, max_length=500)
    transmission_port: Optional[int] = Field(default=None, ge=1, le=65535)
    transmission_username: Optional[str] = Field(default=None, max_length=200)
    transmission_password: Optional[str] = Field(default=None, max_length=1000)
    transmission_use_https: Optional[bool] = None
    transmission_verify_ssl: Optional[bool] = None
    transmission_rpc_path: Optional[str] = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_change(self) -> "DownloaderSettingsPatchRequest":
        if not self.model_fields_set:
            raise ValueError("at least one downloader setting is required")
        return self


class SystemSettingsPatchRequest(StrictModel):
    watchlist_check_hours: int = Field(ge=3, le=168)


class AgentBootstrapRequest(StrictModel):
    name: str = Field(default="My Agent", min_length=1, max_length=100)
    scopes: List[Literal["read", "search", "download", "watchlist", "naming", "settings"]] = Field(
        default_factory=lambda: ["read"], max_length=6
    )


class AgentConnectRequest(StrictModel):
    name: str = Field(default="My Agent", min_length=1, max_length=100)
    capabilities: List[str] = Field(default_factory=list, max_length=50)


class AgentScopesUpdateRequest(StrictModel):
    scopes: List[Literal["read", "search", "download", "watchlist", "naming", "settings"]] = Field(
        min_length=1, max_length=6
    )


class ProviderPreviewRequest(StrictModel):
    keyword: str = Field(default="test", min_length=1, max_length=100)
    media_type: MediaType = Field(default="auto", alias="type")
    site_id: Optional[str] = Field(default=None, min_length=4, max_length=64)
