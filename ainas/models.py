from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MediaType = Literal["movie", "tv", "anime", "custom", "auto"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SearchRequest(StrictModel):
    keyword: str = Field(min_length=1, max_length=100)
    media_type: MediaType = Field(default="auto", alias="type")

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

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()


class WatchlistAddRequest(StrictModel):
    keyword: str = Field(min_length=1, max_length=100)
    media_type: MediaType = Field(default="auto", alias="type")

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, value: str) -> str:
        value = value.strip().strip("《》")
        if not value:
            raise ValueError("keyword cannot be blank")
        return value


class WatchlistCheckRequest(StrictModel):
    item_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


class NamingCheckRequest(StrictModel):
    job_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


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
