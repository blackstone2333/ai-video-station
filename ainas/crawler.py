from __future__ import annotations

import base64
import html
import logging
import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .errors import UpstreamError
from .quality import ANIME_PATH_RE, Release, build_release, decode_thunder_url, infer_media_type, sort_releases


logger = logging.getLogger(__name__)
ARTICLE_PATH_RE = re.compile(r"/(?:[a-zA-Z0-9_-]+/)+(?:((?:19|20)\d{2}-\d{2}-\d{2})/)?\d+\.html$")
TV_PATH_RE = re.compile(
    r"/(?:dlz|rj|mj|tv|lianxuju|dianshiju|guoju|duanju|rihanju|oumeiju|dsj)/",
    re.IGNORECASE,
)
DOWNLOAD_RE = re.compile(
    r"(?i)(magnet:\?[^\s\"'<>]+|ed2k://[^\s\"'<>]+|thunder://[A-Za-z0-9+/=_-]+|https?://[^\s\"'<>]+\.torrent(?:\?[^\s\"'<>]*)?)"
)
KNOWN_SITE_RE = re.compile(r"(^|\.)(?:6v|xb6v|66s6)[a-z0-9.-]*$", re.IGNORECASE)
LANGUAGE_CODES = (
    (re.compile(r"英语|英文|\bEnglish\b", re.IGNORECASE), "en"),
    (re.compile(r"普通话|国语|汉语|中文|\bChinese\b", re.IGNORECASE), "zh"),
    (re.compile(r"粤语|广东话|\bCantonese\b", re.IGNORECASE), "yue"),
    (re.compile(r"日语|日文|\bJapanese\b", re.IGNORECASE), "ja"),
    (re.compile(r"韩语|韩文|\bKorean\b", re.IGNORECASE), "ko"),
    (re.compile(r"西班牙语|\bSpanish\b", re.IGNORECASE), "es"),
    (re.compile(r"法语|法文|\bFrench\b", re.IGNORECASE), "fr"),
    (re.compile(r"德语|德文|\bGerman\b", re.IGNORECASE), "de"),
)


@dataclass(frozen=True)
class SearchItem:
    title: str
    url: str
    media_type: str


class SixVClient:
    def __init__(
        self,
        settings: Settings,
        session: Optional[requests.Session] = None,
        site: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.settings = settings
        self.site = dict(site or {})
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent, "Accept": "text/html,*/*;q=0.8"})
        self._active_base: Optional[str] = None
        self._lock = threading.RLock()

    @property
    def base_urls(self) -> List[str]:
        values = self.site.get("base_urls") or self.settings.site_urls
        return [str(item).rstrip("/") for item in values if str(item).strip()]

    @property
    def address_page(self) -> str:
        return str(self.site.get("address_page") or self.settings.sixv_address_page).rstrip("/")

    @staticmethod
    def decode_html(content: bytes) -> str:
        head = content[:2048].lower()
        if content.startswith(b"\xef\xbb\xbf") or b"charset=utf-8" in head or b"charset=\"utf-8" in head:
            return content.decode("utf-8", "ignore")
        return content.decode("gb18030", "ignore")

    def _discover_domains(self) -> List[str]:
        try:
            response = self.session.get(
                self.address_page,
                timeout=self.settings.request_timeout_seconds,
                allow_redirects=True,
            )
            response.raise_for_status()
            soup = BeautifulSoup(self.decode_html(response.content), "html.parser")
            domains: List[str] = []
            for anchor in soup.find_all("a", href=True):
                parsed = urlparse(anchor["href"])
                host = (parsed.hostname or "").lower()
                if parsed.scheme in {"http", "https"} and KNOWN_SITE_RE.search(host):
                    domains.append(urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/"))
            return list(dict.fromkeys(domains))
        except requests.RequestException as exc:
            logger.warning("domain_discovery_failed", extra={"error_type": type(exc).__name__})
            return []

    def probe(self, force: bool = False) -> str:
        with self._lock:
            if self._active_base and not force:
                return self._active_base
            candidates = self.base_urls
            discovered: List[str] = []
            for pass_number in range(2):
                for base in candidates + discovered:
                    try:
                        response = self.session.get(
                            f"{base.rstrip('/')}/",
                            timeout=self.settings.request_timeout_seconds,
                            allow_redirects=True,
                        )
                        response.raise_for_status()
                        final = urlparse(response.url)
                        selected = f"{final.scheme}://{final.netloc}".rstrip("/")
                        self._active_base = selected
                        logger.info("sixv_domain_selected", extra={"domain": selected})
                        return selected
                    except requests.RequestException:
                        continue
                if pass_number == 0:
                    discovered = self._discover_domains()
            raise UpstreamError("6v", "all configured and discovered domains are unavailable")

    def _article_url(self, href: str, base: str) -> str:
        parsed = urlparse(href)
        if parsed.scheme in {"http", "https"}:
            host = (parsed.hostname or "").lower()
            if KNOWN_SITE_RE.search(host):
                return urljoin(f"{base}/", parsed.path.lstrip("/"))
            return href
        return urljoin(f"{base}/", href)

    @staticmethod
    def _media_type_from_url(url: str) -> str:
        path = urlparse(url).path
        if ANIME_PATH_RE.search(path):
            return "anime"
        return "tv" if TV_PATH_RE.search(path) else infer_media_type("", url, "auto")

    @staticmethod
    def parse_search_page(page_html: str, base: str) -> List[SearchItem]:
        soup = BeautifulSoup(page_html, "html.parser")
        anchors = soup.select(".listInfo h3 a[href], .list-info h3 a[href], .search-list h3 a[href]")
        if not anchors:
            anchors = [
                anchor
                for anchor in soup.find_all("a", href=True)
                if ARTICLE_PATH_RE.search(urlparse(anchor.get("href", "")).path)
            ]
        items: List[SearchItem] = []
        seen = set()
        for anchor in anchors:
            href = anchor.get("href", "").strip()
            if not href:
                continue
            path = urlparse(href).path
            if not ARTICLE_PATH_RE.search(path):
                continue
            url = urljoin(f"{base}/", path.lstrip("/"))
            if url in seen:
                continue
            title = anchor.get("title") or anchor.get_text(" ", strip=True)
            if not title:
                continue
            title = BeautifulSoup(str(title), "html.parser").get_text(" ", strip=True)
            seen.add(url)
            items.append(SearchItem(title=title.strip(), url=url, media_type=SixVClient._media_type_from_url(url)))
        return items

    @staticmethod
    def _decode_javascript(raw_html: str) -> str:
        expanded = html.unescape(raw_html).replace("\\/", "/")
        expanded = re.sub(
            r"\\x([0-9a-fA-F]{2})",
            lambda match: chr(int(match.group(1), 16)),
            expanded,
        )
        expanded = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            expanded,
        )
        decoded_fragments = []
        for match in re.finditer(r"(?i)atob\(['\"]([A-Za-z0-9+/=_-]{16,})['\"]\)", expanded):
            try:
                payload = match.group(1) + "=" * (-len(match.group(1)) % 4)
                decoded_fragments.append(base64.b64decode(payload).decode("utf-8", "ignore"))
            except ValueError:
                continue
        return "\n".join([expanded] + decoded_fragments)

    @staticmethod
    def _clean_download_link(value: str) -> Optional[str]:
        value = html.unescape(value).strip().rstrip(".,;，；")
        value = decode_thunder_url(value)
        lowered = value.lower()
        if lowered.startswith(("magnet:?", "ed2k://")):
            return value
        if lowered.startswith(("http://", "https://")) and ".torrent" in urlparse(value).path.lower():
            return value
        return None

    @staticmethod
    def parse_detail_page(
        page_html: str,
        page_url: str,
        page_title: str,
        media_type: str,
    ) -> List[Release]:
        soup = BeautifulSoup(page_html, "html.parser")
        page_text = soup.get_text("\n", strip=True)
        year_match = re.search(r"(?:◎\s*年\s*代|年\s*份|上映年份)\s*[:：]?\s*((?:19|20)\d{2})", page_text)
        metadata_year = int(year_match.group(1)) if year_match else None
        language_match = re.search(
            r"(?:◎\s*语\s*言|语\s*言)\s*[:：]?\s*([^\n]{1,40})",
            page_text,
            re.IGNORECASE,
        )
        language_text = language_match.group(1) if language_match else ""
        metadata_original_language = next(
            (code for pattern, code in LANGUAGE_CODES if pattern.search(language_text)),
            None,
        )
        candidates: List[Tuple[str, str]] = []
        for anchor in soup.find_all("a", href=True):
            link = SixVClient._clean_download_link(anchor.get("href", ""))
            if link:
                candidates.append((link, anchor.get_text(" ", strip=True) or link))

        expanded = SixVClient._decode_javascript(page_html)
        for match in DOWNLOAD_RE.finditer(expanded):
            link = SixVClient._clean_download_link(match.group(1))
            if link:
                candidates.append((link, link))

        def protocol_rank(candidate: Tuple[str, str]) -> int:
            lowered = candidate[0].lower()
            if lowered.startswith("magnet:?"):
                return 3
            if lowered.startswith(("http://", "https://")):
                return 2
            return 1

        candidates.sort(key=protocol_rank, reverse=True)
        releases: List[Release] = []
        seen = set()
        seen_labels = set()
        for link, label in candidates:
            if link in seen:
                continue
            label_key = re.sub(r"\s+", "", label).casefold()
            if label != link and label_key in seen_labels:
                continue
            seen.add(link)
            if label != link:
                seen_labels.add(label_key)
            release = build_release(
                page_title,
                label,
                page_url,
                link,
                media_type,
                metadata_year,
                metadata_original_language,
            )
            if release:
                releases.append(release)
        return sort_releases(releases)

    def _fetch_detail(self, item: SearchItem, base: str) -> List[Release]:
        url = self._article_url(item.url, base)
        try:
            response = self.session.get(
                url,
                timeout=self.settings.request_timeout_seconds,
                allow_redirects=True,
            )
            response.raise_for_status()
            return self.parse_detail_page(self.decode_html(response.content), url, item.title, item.media_type)
        except requests.Timeout as exc:
            raise UpstreamError("6v", f"detail page timed out: {url}", timeout=True) from exc
        except requests.RequestException as exc:
            raise UpstreamError("6v", f"detail page failed: {url}") from exc

    def search(self, keyword: str, media_type: str = "auto") -> List[Release]:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            base = self.probe(force=attempt > 0)
            host = (urlparse(base).hostname or "").lower()
            is_new_site = "xb6v." in host
            search_path = "/e/search/11index.php" if is_new_site else "/e/search/index.php"
            search_fields = (
                {
                    "show": "title",
                    "tempid": "1",
                    "tbname": "article",
                    "mid": "1",
                    "dopost": "search",
                    "keyboard": keyword,
                }
                if is_new_site
                else {"show": "title,smalltext", "tempid": "1", "tbname": "Article", "keyboard": keyword}
            )
            body = urlencode(
                search_fields,
                encoding="utf-8" if is_new_site else "gbk",
                errors="ignore",
            )
            try:
                response = self.session.post(
                    f"{base}{search_path}",
                    data=body.encode("ascii"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=self.settings.request_timeout_seconds,
                    allow_redirects=True,
                )
                response.raise_for_status()
                effective = urlparse(response.url)
                effective_base = f"{effective.scheme}://{effective.netloc}"
                items = self.parse_search_page(self.decode_html(response.content), effective_base)
                title_matches = [item for item in items if keyword.casefold() in item.title.casefold()]
                if title_matches:
                    items = title_matches
                    # 6V sections are editorial hints, not a reliable media
                    # taxonomy.  An exact name hit must survive a misplaced
                    # section; the user's AVS type becomes the detail-page hint.
                    if media_type != "auto":
                        items = [replace(item, media_type=media_type) for item in items]
                elif media_type != "auto":
                    # When the name did not identify a result, keep the site
                    # section as a fallback to avoid broad unrelated matches.
                    items = [item for item in items if item.media_type in {media_type, "auto"}]
                items.sort(key=lambda item: (keyword.casefold() in item.title.casefold(), item.title), reverse=True)
                releases: List[Release] = []
                for item in items[: self.settings.search_detail_limit]:
                    try:
                        releases.extend(self._fetch_detail(item, effective_base))
                    except UpstreamError as exc:
                        logger.warning(
                            "detail_page_skipped",
                            extra={"url": item.url, "error_type": type(exc).__name__},
                        )
                if media_type != "auto":
                    releases = [item for item in releases if item.media_type == media_type]
                return sort_releases(releases)
            except requests.Timeout as exc:
                last_error = exc
            except requests.RequestException as exc:
                last_error = exc
        if isinstance(last_error, requests.Timeout):
            raise UpstreamError("6v", "search timed out", timeout=True) from last_error
        raise UpstreamError("6v", "search failed on all available domains") from last_error
