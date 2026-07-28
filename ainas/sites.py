from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Settings
from .crawler import SixVClient
from .errors import NotFoundError, UpstreamError, ValidationAppError
from .quality import Release, build_release, infer_media_type, sort_releases


logger = logging.getLogger(__name__)


class SiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], min_length=4, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    adapter: Literal["sixv", "generic"] = "generic"
    enabled: bool = True
    base_urls: List[str] = Field(min_length=1, max_length=20)
    address_page: Optional[str] = None
    search_url: Optional[str] = None
    result_selector: str = ".search-item"
    title_selector: str = "a"
    link_selector: str = "a"
    download_selector: str = "a[href]"
    default_type: Literal["auto", "movie", "tv", "anime"] = "auto"
    tv_path_patterns: List[str] = Field(default_factory=lambda: ["/tv/", "/series/"])
    anime_path_patterns: List[str] = Field(
        default_factory=lambda: ["/anime/", "/animation/", "/dongman/", "/donghua/", "/dm/"]
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_urls")
    @classmethod
    def validate_urls(cls, values: List[str]) -> List[str]:
        normalized = []
        for value in values:
            parsed = urlparse(value.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("base_urls must contain absolute HTTP(S) URLs")
            normalized.append(value.strip().rstrip("/"))
        return list(dict.fromkeys(normalized))

    @field_validator("result_selector", "title_selector", "link_selector", "download_selector")
    @classmethod
    def validate_selector(cls, value: str) -> str:
        value = value.strip()
        try:
            BeautifulSoup("<html></html>", "html.parser").select(value)
        except Exception as exc:
            raise ValueError("must be a valid CSS selector") from exc
        return value

    @model_validator(mode="after")
    def validate_generic_fields(self) -> "SiteConfig":
        if self.adapter == "generic" and (not self.search_url or "{keyword}" not in self.search_url):
            raise ValueError("generic search_url must contain {keyword}")
        if self.adapter == "generic":
            rendered = str(self.search_url).replace("{base_url}", self.base_urls[0]).replace("{keyword}", "test")
            parsed = urlparse(rendered)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("generic search_url must render to an absolute HTTP(S) URL")
        return self


class SiteRepository:
    def __init__(self, path: Path, default_site: Dict[str, Any]) -> None:
        self.path = path
        self.default_site = SiteConfig.model_validate(default_site).model_dump()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"items": [self.default_site]})

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError("invalid sites shape")
            return {"items": [SiteConfig.model_validate(item).model_dump() for item in value["items"]]}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationAppError(f"site configuration is invalid: {exc}") from exc

    def _write(self, value: Dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix="sites-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return deepcopy(self._read()["items"])

    def add(self, value: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            try:
                site = SiteConfig.model_validate(value).model_dump()
            except ValueError as exc:
                raise ValidationAppError(f"site configuration is invalid: {exc}") from exc
            if any(item["id"] == site["id"] for item in data["items"]):
                raise ValidationAppError("site id already exists")
            data["items"].append(site)
            self._write(data)
            return deepcopy(site)

    def update(self, site_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["items"]):
                if item["id"] == site_id:
                    try:
                        updated = SiteConfig.model_validate({**item, **changes, "id": site_id}).model_dump()
                    except ValueError as exc:
                        raise ValidationAppError(f"site configuration is invalid: {exc}") from exc
                    data["items"][index] = updated
                    self._write(data)
                    return deepcopy(updated)
        raise NotFoundError("site", site_id)

    def delete(self, site_id: str) -> None:
        with self._lock:
            data = self._read()
            filtered = [item for item in data["items"] if item["id"] != site_id]
            if len(filtered) == len(data["items"]):
                raise NotFoundError("site", site_id)
            self._write({"items": filtered})


class GenericHTMLProvider:
    def __init__(self, settings: Settings, site: Dict[str, Any], session: Optional[requests.Session] = None) -> None:
        self.settings = settings
        self.site = SiteConfig.model_validate(site)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent, "Accept": "text/html,*/*;q=0.8"})

    def probe(self) -> str:
        base = self.site.base_urls[0]
        try:
            response = self.session.get(base, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()
            return base
        except requests.RequestException as exc:
            raise UpstreamError(self.site.name, "site probe failed") from exc

    def _type_for(self, title: str, url: str) -> str:
        path = urlparse(url).path.casefold()
        if any(pattern.casefold() in path for pattern in self.site.anime_path_patterns):
            return "anime"
        if any(pattern.casefold() in path for pattern in self.site.tv_path_patterns):
            return "tv"
        return infer_media_type(title, url, self.site.default_type)

    def search(self, keyword: str, media_type: str = "auto") -> List[Release]:
        base = self.site.base_urls[0]
        search_url = str(self.site.search_url).replace("{base_url}", base).replace("{keyword}", quote_plus(keyword))
        try:
            response = self.session.get(search_url, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
        except requests.RequestException as exc:
            raise UpstreamError(self.site.name, "search failed") from exc

        releases: List[Release] = []
        for node in soup.select(self.site.result_selector)[: self.settings.search_detail_limit]:
            title_node = node.select_one(self.site.title_selector) if self.site.title_selector else node
            link_node = node.select_one(self.site.link_selector) if self.site.link_selector else node
            title = title_node.get_text(" ", strip=True) if title_node else ""
            href = link_node.get("href", "") if link_node else ""
            if not title or not href:
                continue
            detail_url = urljoin(f"{base}/", href)
            resolved_type = self._type_for(title, detail_url)
            if resolved_type == "auto" and media_type != "auto":
                resolved_type = media_type
            if media_type != "auto" and resolved_type != media_type:
                continue
            try:
                detail = self.session.get(detail_url, timeout=self.settings.request_timeout_seconds)
                detail.raise_for_status()
                detail_soup = BeautifulSoup(detail.content, "html.parser")
            except requests.RequestException:
                logger.warning("generic_detail_skipped", extra={"site_id": self.site.id, "url": detail_url})
                continue
            for anchor in detail_soup.select(self.site.download_selector):
                link = SixVClient._clean_download_link(anchor.get("href", ""))
                if not link:
                    continue
                release = build_release(
                    title,
                    anchor.get_text(" ", strip=True) or link,
                    detail_url,
                    link,
                    resolved_type,
                )
                if release:
                    releases.append(replace(release, provider=self.site.id))
        return sort_releases(releases)


class ProviderRegistry:
    def __init__(self, settings: Settings, repository: SiteRepository) -> None:
        self.settings = settings
        self.repository = repository

    def _providers(self) -> List[Any]:
        providers = []
        for site in self.repository.list():
            if not site["enabled"]:
                continue
            if site["adapter"] == "sixv":
                providers.append((site, SixVClient(self.settings, site=site)))
            else:
                providers.append((site, GenericHTMLProvider(self.settings, site)))
        return providers

    def probe(self) -> str:
        errors = []
        for site, provider in self._providers():
            try:
                return provider.probe()
            except UpstreamError as exc:
                errors.append(exc.detail)
        raise UpstreamError("media providers", "; ".join(errors) or "no enabled sites")

    def search(self, keyword: str, media_type: str = "auto") -> List[Release]:
        providers = self._providers()
        releases: List[Release] = []
        errors = []
        for site, provider in providers:
            try:
                values = provider.search(keyword, media_type)
                releases.extend(
                    replace(item, provider=item.provider or site["id"])
                    for item in values
                )
            except UpstreamError as exc:
                errors.append(exc.detail)
                logger.warning("provider_search_failed", extra={"site_id": site["id"], "error": exc.detail})
        if not releases and errors and len(errors) == len(providers):
            raise UpstreamError("media providers", "; ".join(errors))
        unique = {item.id: item for item in releases}
        return sort_releases(unique.values())
