from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


MediaType = Literal["movie", "tv", "auto"]


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
