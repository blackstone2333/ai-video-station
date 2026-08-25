from __future__ import annotations

import json
import logging
import os
import ipaddress
import socket
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Settings
from .crawler import SixVClient
from .errors import NotFoundError, UpstreamError, ValidationAppError
from .quality import Release, build_release, infer_media_type, sort_releases
from .state import StateStore, StateStoreError


logger = logging.getLogger(__name__)
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_REDIRECTS = 5
MAX_PROVIDER_WORKERS = 4


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
    default_type: Literal["auto", "movie", "tv", "anime", "custom"] = "auto"
    tv_path_patterns: List[str] = Field(default_factory=lambda: ["/tv/", "/series/"])
    anime_path_patterns: List[str] = Field(
        default_factory=lambda: ["/anime/", "/animation/", "/dongman/", "/donghua/", "/dm/"]
    )
    # Private destinations are blocked by default.  This is intentionally a
    # per-site opt-in for deployments which host a provider on a trusted LAN.
    allow_private_hosts: bool = False

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
    def __init__(self, path: Path, default_site: Dict[str, Any], state_store: StateStore | None = None) -> None:
        self.path = path
        self.state_store = state_store
        self.default_site = SiteConfig.model_validate(default_site).model_dump()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_store:
            try: self.state_store.ensure_records("sites", legacy_path=self.path, default=[self.default_site])
            except StateStoreError as exc: raise ValidationAppError(f"site configuration is unavailable: {exc}") from exc
        elif not self.path.exists():
            self._write({"items": [self.default_site]})

    def _read(self) -> Dict[str, Any]:
        try:
            value = {"items": self.state_store.list_records("sites")} if self.state_store else json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError("invalid sites shape")
            return {"items": [SiteConfig.model_validate(item).model_dump() for item in value["items"]]}
        except (OSError, ValueError, json.JSONDecodeError, StateStoreError) as exc:
            raise ValidationAppError(f"site configuration is invalid: {exc}") from exc

    def _write(self, value: Dict[str, Any]) -> None:
        if self.state_store:
            try: self.state_store.replace_records("sites", value["items"]); return
            except StateStoreError as exc: raise ValidationAppError(f"site configuration is unavailable: {exc}") from exc
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
            # The SixV adapter follows site-specific discovery and redirect
            # rules.  Keep it reserved for the audited built-in record so a
            # configurable provider cannot use it to bypass the generic
            # adapter's outbound URL policy.
            if site["adapter"] == "sixv":
                raise ValidationAppError("the sixv adapter is reserved for the built-in provider")
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

    def _allowed_hosts(self) -> set[str]:
        return {(urlparse(value).hostname or "").lower() for value in self.site.base_urls}

    def _validate_url(self, value: str) -> None:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise UpstreamError(self.site.name, "provider URL must be HTTP(S)")
        if host not in self._allowed_hosts():
            raise UpstreamError(self.site.name, "provider URL host is not configured")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        except socket.gaierror:
            # URL policy still applies to a literal address; leave ordinary DNS
            # failures to requests so existing proxy/DNS configurations work.
            addresses = set()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not self.site.allow_private_hosts and (
                ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
            ):
                raise UpstreamError(self.site.name, "provider URL resolves to a blocked private destination")

    def _get_html(self, value: str) -> requests.Response:
        """Fetch a small HTML document while checking every redirect destination."""
        current = value
        for _ in range(MAX_PROVIDER_REDIRECTS + 1):
            self._validate_url(current)
            try:
                response = self.session.get(
                    current,
                    timeout=self.settings.request_timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise UpstreamError(self.site.name, "provider request failed") from exc
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise UpstreamError(self.site.name, "provider redirect has no location")
                current = urljoin(current, location)
                response.close()
                continue
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                response.close()
                raise UpstreamError(self.site.name, "provider request failed") from exc
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            # Some small self-hosted sites omit HTML headers and requests/our
            # test transport labels those as text/plain; it is still bounded and
            # parsed as inert markup.
            if content_type and content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                response.close()
                raise UpstreamError(self.site.name, "provider returned unsupported content type")
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_PROVIDER_RESPONSE_BYTES:
                response.close()
                raise UpstreamError(self.site.name, "provider response exceeds size limit")
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                        raise UpstreamError(self.site.name, "provider response exceeds size limit")
                response._content = bytes(body)  # requests' public .content cache
                return response
            except Exception:
                response.close()
                raise
        raise UpstreamError(self.site.name, "provider redirected too many times")

    def probe(self) -> str:
        base = self.site.base_urls[0]
        try:
            response = self._get_html(base)
            response.close()
            return base
        except UpstreamError:
            raise

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
        response = self._get_html(search_url)
        soup = BeautifulSoup(response.content, "html.parser")
        response.close()

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
                detail = self._get_html(detail_url)
                detail_soup = BeautifulSoup(detail.content, "html.parser")
                detail.close()
            except UpstreamError:
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


class ProviderAdapter(Protocol):
    def probe(self) -> str: ...
    def search(self, keyword: str, media_type: str = "auto") -> List[Release]: ...


class ProviderRegistry:
    """A fixed adapter registry; site configuration can select, never load, code."""

    _ADAPTERS: Dict[str, Callable[[Settings, Dict[str, Any]], ProviderAdapter]] = {
        "sixv": lambda settings, site: SixVClient(settings, site=site),
        "generic": lambda settings, site: GenericHTMLProvider(settings, site),
    }

    def __init__(self, settings: Settings, repository: SiteRepository, max_workers: int = MAX_PROVIDER_WORKERS) -> None:
        self.settings = settings
        self.repository = repository
        self.max_workers = max(1, min(MAX_PROVIDER_WORKERS, max_workers))

    def _providers(self) -> List[Any]:
        providers = []
        for site in self.repository.list():
            if not site["enabled"]:
                continue
            factory = self._ADAPTERS.get(site["adapter"])
            if factory is None:  # defensive: SiteConfig normally forbids this
                logger.warning("provider_adapter_rejected", extra={"site_id": site["id"]})
                continue
            providers.append((site, factory(self.settings, site)))
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
        # Futures are collected in site order below, not completion order: a
        # concurrent slow provider cannot perturb otherwise deterministic output.
        indexed: Dict[int, List[Release]] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(providers) or 1)) as executor:
            futures = {executor.submit(provider.search, keyword, media_type): (index, site) for index, (site, provider) in enumerate(providers)}
            for future in as_completed(futures):
                index, site = futures[future]
                try:
                    indexed[index] = future.result()
                except UpstreamError as exc:
                    errors.append(exc.detail)
                    logger.warning("provider_search_failed", extra={"site_id": site["id"], "error": exc.detail})
                except Exception:
                    errors.append(f"{site['name']}: provider failed")
                    logger.exception("provider_search_failed", extra={"site_id": site["id"]})
        for index, (site, _) in enumerate(providers):
            releases.extend(replace(item, provider=item.provider or site["id"]) for item in indexed.get(index, []))
        if not releases and errors and len(errors) == len(providers):
            raise UpstreamError("media providers", "; ".join(errors))
        unique = {item.id: item for item in releases}
        return sort_releases(unique.values())

    def preview(self, keyword: str = "test", media_type: str = "auto", site_id: Optional[str] = None) -> Dict[str, Any]:
        """Safe integration check: concise status only, never upstream response bodies."""
        values = [(site, provider) for site, provider in self._providers() if site_id is None or site["id"] == site_id]
        reports = []
        for site, provider in values:
            try:
                probe = provider.probe()
                sample = provider.search(keyword, media_type)[:3]
                reports.append({"site_id": site["id"], "ok": True, "probe": probe, "sample_count": len(sample), "sample": [item.to_api() for item in sample]})
            except UpstreamError as exc:
                reports.append({"site_id": site["id"], "ok": False, "error": {"code": exc.code, "detail": exc.detail}})
            except Exception:
                logger.exception("provider_preview_failed", extra={"site_id": site["id"]})
                reports.append({"site_id": site["id"], "ok": False, "error": {"code": "provider-error", "detail": "provider check failed"}})
        return {"ok": bool(reports) and all(report["ok"] for report in reports), "providers": reports}

    test_provider = preview
