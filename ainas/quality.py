from __future__ import annotations

import base64
import hashlib
import html
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse


CAM_PATTERNS = [
    re.compile(r"(?i)(^|[.\s\-_\[\]()])(?:hd)?ts([.\s\-_\[\]()]|$)"),
    re.compile(r"(?i)(^|[.\s\-_\[\]()])(?:hd)?tc([.\s\-_\[\]()]|$)"),
    re.compile(r"(?i)(^|[.\s\-_\[\]()])(?:hq)?cam([.\s\-_\[\]()]|$)"),
    re.compile(r"枪版|抢先版|清晰\s*TS", re.IGNORECASE),
]

SIZE_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(TB|TiB|GB|GiB|G|MB|MiB|M)\b")
EPISODE_PATTERNS = [
    re.compile(r"(?i)\bS(\d{1,2})[ ._\-]*E(\d{1,3})(?:[ ._\-]*(?:E|X)(\d{1,3}))?\b"),
    re.compile(r"(?i)(?:^|[ ._\-])E(?:P)?(\d{1,3})(?:[ ._\-]|$)"),
    re.compile(r"第\s*(\d{1,3})\s*[集话期]"),
    re.compile(r"(?i)(?:更新|全)?\s*(\d{1,3})\s*集"),
]
EPISODIC_MEDIA_TYPES = {"tv", "anime"}
ANIME_PATH_RE = re.compile(r"(?i)/(?:dm|dongman|donghua|anime|animation|cartoon)(?:/|$)")
TV_PATH_HINT_RE = re.compile(r"(?i)/(?:dlz|rj|mj|tv|lianxuju|dianshiju|guoju|duanju|rihanju|oumeiju|dsj)(?:/|$)")
MOVIE_PATH_HINT_RE = re.compile(r"(?i)/(?:dy|jddy|movie|dianying|film)(?:/|$)")
ANIME_HINT_RE = re.compile(r"(?i)动漫|动画(?:剧|系列|片)?|番剧|anime|animation|cartoon")


@dataclass(frozen=True)
class Release:
    id: str
    title: str
    url: str
    download_link: str
    size: Optional[str]
    resolution: Optional[str]
    source: Optional[str]
    language: Optional[str]
    hdr: Optional[str]
    encoding: Optional[str]
    media_type: str
    episode: Optional[str]
    size_bytes: int = 0
    media_name: Optional[str] = None
    year: Optional[int] = None
    season: Optional[int] = None
    link_name: Optional[str] = None
    provider: Optional[str] = None

    def to_api(self) -> Dict[str, Any]:
        value = asdict(self)
        value.pop("size_bytes", None)
        value["type"] = value.pop("media_type")
        return value


def is_cam_release(title: str) -> bool:
    normalized = unquote(html.unescape(title))
    return any(pattern.search(normalized) for pattern in CAM_PATTERNS)


def parse_size(text: str) -> Tuple[Optional[str], int]:
    match = SIZE_RE.search(text)
    if not match:
        return None, 0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    if unit in {"TB", "TIB"}:
        size_bytes = int(amount * 1024**4)
        label = f"{amount:g}TB"
    elif unit in {"GB", "GIB", "G"}:
        size_bytes = int(amount * 1024**3)
        label = f"{amount:g}GB"
    else:
        size_bytes = int(amount * 1024**2)
        label = f"{amount:g}MB"
    return label, size_bytes


def detect_episode(text: str) -> Optional[str]:
    normalized = unquote(text)
    season_episode = EPISODE_PATTERNS[0].search(normalized)
    if season_episode:
        value = f"S{int(season_episode.group(1)):02d}E{int(season_episode.group(2)):02d}"
        if season_episode.group(3):
            value += f"-E{int(season_episode.group(3)):02d}"
        return value
    one_x = re.search(r"(?i)(?:^|[^0-9])(\d{1,2})x(\d{1,3})(?:[^0-9]|$)", normalized)
    if one_x:
        return f"S{int(one_x.group(1)):02d}E{int(one_x.group(2)):02d}"
    episode = EPISODE_PATTERNS[1].search(normalized)
    if episode:
        return f"E{int(episode.group(1)):02d}"
    for pattern in EPISODE_PATTERNS[2:]:
        match = pattern.search(normalized)
        if match:
            return f"E{int(match.group(1)):02d}"
    basename = PurePosixPath(normalized.replace("\\", "/")).name
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    bare = re.fullmatch(r"(?i)\s*(?:EP?|第)?0*(\d{1,3})(?:集)?\s*", stem)
    if bare:
        return f"E{int(bare.group(1)):02d}"
    return None


CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_number(value: str) -> Optional[int]:
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = CHINESE_DIGITS.get(left, 1) if left else 1
        ones = CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return CHINESE_DIGITS.get(value)
    return None


def detect_season(text: str) -> Optional[int]:
    normalized = unquote(text)
    match = re.search(r"第\s*([0-9零〇一二两三四五六七八九十]{1,3})\s*季", normalized)
    if match:
        return _chinese_number(match.group(1))
    match = re.search(r"(?i)(?:season|\bS)\s*[._\- ]*0*(\d{1,2})(?:\b|[^0-9])", normalized)
    if match:
        return int(match.group(1))
    episode = detect_episode(normalized)
    if episode and episode.startswith("S"):
        return int(episode[1:3])
    return None


def infer_media_type(text: str, url: str = "", hint: str = "auto") -> str:
    """Resolve a media type without treating every unknown category as a movie."""
    if hint == "custom":
        return "custom"
    normalized = unquote(text)
    if ANIME_PATH_RE.search(urlparse(url).path) or ANIME_HINT_RE.search(normalized):
        return "anime"
    if TV_PATH_HINT_RE.search(urlparse(url).path):
        return "tv"
    if MOVIE_PATH_HINT_RE.search(urlparse(url).path):
        return "movie"
    if hint in EPISODIC_MEDIA_TYPES:
        return hint
    if detect_episode(normalized) or detect_season(normalized) is not None:
        return "tv"
    return hint


def canonical_media_name(value: str) -> str:
    name = html.unescape(value.split("|", 1)[0]).strip()
    name = re.sub(r"<[^>]+>", "", name).strip()
    quoted = re.search(r"《([^》]{1,100})》", name)
    if quoted:
        name = quoted.group(1).strip()
    name = re.sub(
        r"[\[【(（]\s*(?:第\s*[0-9零〇一二两三四五六七八九十]+\s*季|Season\s*\d+|S\d{1,2})\s*[\]】)）]",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"\s+第\s*[0-9零〇一二两三四五六七八九十]+\s*季\s*$", "", name)
    name = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", name)
    return re.sub(r"\s+", " ", name).strip(" .-_《》") or "未命名媒体"


def link_display_name(download_link: str, fallback: str) -> str:
    try:
        parsed = urlparse(html.unescape(download_link))
    except ValueError:
        return fallback.strip()
    if parsed.scheme.lower() == "magnet":
        names = parse_qs(parsed.query).get("dn")
        if names and names[0].strip():
            return unquote(names[0]).strip()
    if parsed.path:
        basename = PurePosixPath(unquote(parsed.path)).name
        if basename:
            return basename
    return fallback.strip()


def _resolution(text: str) -> Optional[str]:
    lowered = text.lower()
    for token in ("4320p", "2160p", "1080p", "720p", "480p"):
        if token in lowered:
            return token
    if re.search(r"(?i)\b4k\b|超高清|uhd", text):
        return "2160p"
    return None


def _source(text: str) -> Optional[str]:
    if re.search(
        r"(?i)blu[ ._\-]?ray|b[dr]rip|remux|蓝光|(?:^|[._\- ])BD(?=$|[._\- ]|[^A-Za-z0-9])",
        text,
    ):
        return "BluRay"
    if re.search(r"(?i)web[ ._\-]?dl|web[ ._\-]?rip", text):
        return "WEB-DL"
    if re.search(r"(?i)(?:^|[._\- ])HD(?:TV)?(?=$|[._\- ]|[^A-Za-z0-9])|高清", text):
        return "HD"
    return None


def _language(text: str) -> Optional[str]:
    if re.search(r"国英双语|双语|国语.*英语|英语.*国语", text, re.IGNORECASE):
        return "国英双语"
    if re.search(r"英语|English", text, re.IGNORECASE):
        return "英语"
    if re.search(r"国语|普通话|中文", text, re.IGNORECASE):
        return "国语"
    if re.search(r"粤语", text, re.IGNORECASE):
        return "粤语"
    return None


def _hdr(text: str) -> Optional[str]:
    if re.search(r"(?i)dolby[ ._\-]?vision|\bDV\b|杜比视界", text):
        return "Dolby Vision"
    if re.search(r"(?i)HDR10\+", text):
        return "HDR10+"
    if re.search(r"(?i)HDR10", text):
        return "HDR10"
    if re.search(r"(?i)\bHDR\b", text):
        return "HDR"
    return None


def _encoding(text: str) -> Optional[str]:
    if re.search(r"(?i)\b(?:x265|h[ .]?265|hevc)\b", text):
        return "x265"
    if re.search(r"(?i)\b(?:x264|h[ .]?264|avc)\b", text):
        return "x264"
    if re.search(r"(?i)\bAV1\b", text):
        return "AV1"
    return None


def release_id(page_url: str, download_link: str) -> str:
    return hashlib.sha256(f"{page_url}\n{download_link}".encode("utf-8")).hexdigest()[:16]


def build_release(
    page_title: str,
    label: str,
    page_url: str,
    download_link: str,
    media_type: str,
    metadata_year: Optional[int] = None,
) -> Optional[Release]:
    decoded_link = unquote(html.unescape(download_link))
    readable = " ".join(item for item in (page_title.strip(), label.strip(), decoded_link) if item)
    if is_cam_release(readable):
        return None
    size, size_bytes = parse_size(readable)
    title = page_title.strip()
    if label.strip() and label.strip() not in title:
        title = f"{title} | {label.strip()}"
    resolved_media_type = infer_media_type(readable, page_url, media_type)
    episode = detect_episode(readable) if resolved_media_type in EPISODIC_MEDIA_TYPES else None
    season = detect_season(readable) if resolved_media_type in EPISODIC_MEDIA_TYPES else None
    if episode and episode.startswith("S"):
        season = int(episode[1:3])
    return Release(
        id=release_id(page_url, download_link),
        title=title,
        url=page_url,
        download_link=html.unescape(download_link).strip(),
        size=size,
        resolution=_resolution(readable),
        source=_source(readable),
        language=_language(readable),
        hdr=_hdr(readable),
        encoding=_encoding(readable),
        media_type=resolved_media_type,
        episode=episode,
        size_bytes=size_bytes,
        media_name=canonical_media_name(page_title),
        year=metadata_year,
        season=season,
        link_name=link_display_name(download_link, label),
    )


def sort_releases(releases: Iterable[Release]) -> List[Release]:
    resolution_rank = {"4320p": 5, "2160p": 4, "1080p": 3, "720p": 2, "480p": 1}
    language_rank = {"国英双语": 4, "国语": 3, "英语": 2, "粤语": 1}
    hdr_rank = {"Dolby Vision": 4, "HDR10+": 3, "HDR10": 2, "HDR": 1}
    source_rank = {"BluRay": 3, "WEB-DL": 2, "HD": 1}
    return sorted(
        releases,
        key=lambda item: (
            resolution_rank.get(item.resolution or "", 0),
            item.size_bytes,
            language_rank.get(item.language or "", 0),
            hdr_rank.get(item.hdr or "", 0),
            source_rank.get(item.source or "", 0),
            item.title,
        ),
        reverse=True,
    )


def decode_thunder_url(url: str) -> str:
    if not url.lower().startswith("thunder://"):
        return url
    payload = url.split("://", 1)[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", "ignore")
        if decoded.startswith("AA") and decoded.endswith("ZZ"):
            decoded = decoded[2:-2]
        return decoded or url
    except (ValueError, UnicodeDecodeError):
        return url
