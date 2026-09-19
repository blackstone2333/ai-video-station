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
RANGE_SEPARATOR = r"\s*[-~至_]\s*"
EPISODE_RANGE_PATTERNS = [
    re.compile(rf"(?i)(?<![a-z0-9])S(\d{{1,2}})[ ._\-]*E(\d{{1,3}})(?:{RANGE_SEPARATOR}(?:EP?|X)?|[ .]*E)(\d{{1,3}})(?![a-z0-9])"),
    re.compile(rf"(?i)(?<![a-z0-9])EP?(\d{{1,3}}){RANGE_SEPARATOR}(?:EP?)?(\d{{1,3}})(?![a-z0-9])"),
    re.compile(rf"(?:第|全|更新至)?\s*(?<!\d)(\d{{1,3}}){RANGE_SEPARATOR}(\d{{1,3}})\s*[集话期]"),
]
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
    original_title: Optional[str] = None
    part: Optional[str] = None
    edition: Optional[str] = None
    video_format: Optional[str] = None
    episode_title: Optional[str] = None
    audio_languages: Tuple[str, ...] = ()
    original_language: Optional[str] = None
    episodes: Tuple[str, ...] = ()

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


def detect_episode(text: str, *, allow_numeric_prefix: bool = False) -> Optional[str]:
    normalized = unquote(text)
    for index, pattern in enumerate(EPISODE_RANGE_PATTERNS):
        match = pattern.search(normalized)
        if match:
            numbers = [int(value) for value in match.groups()]
            season, start, end = numbers if index == 0 else (None, *numbers)
            if end < start:
                return None
            prefix = f"S{season:02d}" if season is not None else ""
            return f"{prefix}E{start:02d}" + (f"-E{end:02d}" if end != start else "")
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
    # 6V episode packs commonly use names such as
    # ``01.2160p.HD国语中字[site].mkv``.  The leading numeric token is the
    # episode number; require a separator after it so resolutions such as
    # ``720p`` are not mistaken for episode 720.
    if allow_numeric_prefix:
        numeric_range = re.match(rf"(?i)^\s*(\d{{1,3}}){RANGE_SEPARATOR}(\d{{1,3}})(?=[ ._\-]|$)", stem)
        if numeric_range:
            start, end = map(int, numeric_range.groups())
            return f"E{start:02d}-E{end:02d}" if end >= start else None
        numeric_prefix = re.match(r"(?i)^\s*(?:EP?|第)?0*(\d{1,3})(?=[ ._\-])", stem)
        if numeric_prefix:
            return f"E{int(numeric_prefix.group(1)):02d}"
    return None


def expand_episode(value: Optional[str], season: Optional[int] = None) -> Tuple[str, ...]:
    """Expand a canonical episode token without losing the end of a joined file."""
    match = re.fullmatch(r"(?:S(\d{1,2}))?E(\d{1,3})(?:-E(\d{1,3}))?", value or "", re.IGNORECASE)
    if not match:
        return ()
    season_value = int(match.group(1)) if match.group(1) is not None else season
    start = int(match.group(2))
    end = int(match.group(3) or start)
    if end < start:
        return ()
    prefix = f"S{season_value:02d}" if season_value is not None else ""
    return tuple(f"{prefix}E{number:02d}" for number in range(start, end + 1))


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
    """Resolve content signals first and treat provider sections as weak hints."""
    if hint == "custom":
        return "custom"
    normalized = unquote(text)
    path = urlparse(url).path
    anime_path = bool(ANIME_PATH_RE.search(path))
    if ANIME_HINT_RE.search(normalized):
        return "anime"
    if detect_episode(normalized) or detect_season(normalized) is not None:
        if hint == "anime" or (hint == "auto" and anime_path):
            return "anime"
        return "tv"
    if hint in {"movie", "tv", "anime"}:
        return hint
    if anime_path:
        return "anime"
    if TV_PATH_HINT_RE.search(path):
        return "tv"
    if MOVIE_PATH_HINT_RE.search(path):
        return "movie"
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
    name = re.sub(
        r"[\[【(（]\s*(?:全集|全\s*\d{1,3}\s*集|全季|完结(?:篇)?)\s*[\]】)）]",
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
    if re.search(r"(?i)\bremux\b|原盘", text):
        return "Remux"
    if re.search(
        r"(?i)blu[ ._\-]?ray|b[dr]rip|蓝光|(?:^|[._\- ])BD(?=$|[._\- ]|[^A-Za-z0-9])",
        text,
    ):
        return "BluRay"
    if re.search(r"(?i)web[ ._\-]?dl|web[ ._\-]?rip", text):
        return "WEB-DL"
    if re.search(r"(?i)hdtv", text):
        return "HDTV"
    if re.search(r"(?i)(?:^|[._\- ])HD(?:TV)?(?=$|[._\- ]|[^A-Za-z0-9])|高清", text):
        return "HD"
    return None


LANGUAGE_NAMES = {
    "zh": (r"国语|普通话|汉语|中文", "国语"),
    "yue": (r"粤语|广东话", "粤语"),
    "en": (r"英语|英文|English", "英语"),
    "ja": (r"日语|日文|Japanese", "日语"),
    "ko": (r"韩语|韩文|Korean", "韩语"),
    "es": (r"西班牙语|西语|Spanish", "西班牙语"),
    "fr": (r"法语|French", "法语"),
    "de": (r"德语|German", "德语"),
    "ru": (r"俄语|Russian", "俄语"),
    "th": (r"泰语|Thai", "泰语"),
}
SHORT_LANGUAGE_NAMES = {
    "中": "zh", "国": "zh", "粤": "yue", "英": "en", "日": "ja",
    "韩": "ko", "西": "es", "法": "fr", "德": "de", "俄": "ru", "泰": "th",
}


def normalize_language(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().casefold()
    if normalized in LANGUAGE_NAMES:
        return normalized
    for code, (pattern, _) in LANGUAGE_NAMES.items():
        if re.search(pattern, value, re.IGNORECASE):
            return code
    return None


def _audio_languages(text: str) -> Tuple[str, ...]:
    cleaned = re.sub(
        r"(?i)(?:简体|繁体|中文|英文|中英|简英|繁英|双语)?\s*(?:字幕|双字|中字|硬字|软字)",
        " ",
        text,
    )
    found: List[str] = []
    compound = re.search(r"([中国粤英日韩西法德俄泰]{2,6})(?:多语|三语|双语)", cleaned)
    if compound:
        found.extend(SHORT_LANGUAGE_NAMES[token] for token in compound.group(1) if token in SHORT_LANGUAGE_NAMES)
    for code, (pattern, _) in LANGUAGE_NAMES.items():
        if re.search(pattern, cleaned, re.IGNORECASE):
            found.append(code)
    return tuple(dict.fromkeys(found))


def _language(text: str, audio_languages: Tuple[str, ...]) -> Optional[str]:
    if set(audio_languages) == {"zh", "en"}:
        return "国英双语"
    if len(audio_languages) >= 2 or re.search(r"多国语言|多语|三语|双语", text, re.IGNORECASE):
        return "多语言"
    if audio_languages:
        return LANGUAGE_NAMES[audio_languages[0]][1]
    return None


def detect_episode_range(text: str, season: Optional[int] = None) -> Tuple[str, ...]:
    normalized = unquote(text)
    token = detect_episode(normalized)
    season_value = season if season is not None else detect_season(normalized)
    if season_value is None:
        season_value = 1
    if token and "-E" in token:
        return expand_episode(token, season_value)
    complete = re.search(r"(?:全集|全)\s*0*(\d{1,3})\s*[集话期]", normalized)
    return expand_episode(f"E01-E{int(complete.group(1)):02d}", season_value) if complete else ()


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


def _edition(text: str) -> Optional[str]:
    patterns = (
        (r"(?i)director'?s[ ._-]*cut|导演剪辑版", "Director's Cut"),
        (r"(?i)extended[ ._-]*(?:cut|edition)?|加长版", "Extended"),
        (r"(?i)theatrical[ ._-]*(?:cut|edition)?|剧场版", "Theatrical"),
        (r"(?i)imax", "IMAX"),
        (r"(?i)criterion|标准收藏版", "Criterion"),
    )
    for pattern, label in patterns:
        if re.search(pattern, text):
            return label
    return None


def _part(text: str) -> Optional[str]:
    match = re.search(r"(?i)(?:^|[ ._\-])(?:cd|disc|disk)[ ._\-]*0*(\d+)", text)
    if match:
        return f"CD{int(match.group(1))}"
    match = re.search(r"(?i)(?:^|[ ._\-])(?:part|pt)[ ._\-]*0*(\d+)", text)
    if match:
        return f"Part{int(match.group(1))}"
    return None


def _video_format(*values: Optional[str]) -> Optional[str]:
    selected = list(dict.fromkeys(value for value in values if value))
    return ".".join(selected) if selected else None


def _original_title(page_title: str, media_name: str) -> Optional[str]:
    if re.search(r"[\u3400-\u9fff]", media_name) is None:
        return None
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9'&:,.! ]{2,80}", page_title)
    for candidate in candidates:
        value = re.sub(r"\s+", " ", candidate).strip(" .,-")
        if value and not re.search(r"(?i)season|episode|1080p|2160p|720p|bluray|web[ .-]?dl", value):
            return value
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
    metadata_original_language: Optional[str] = None,
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
    episodes = detect_episode_range(readable, season) if resolved_media_type in EPISODIC_MEDIA_TYPES else ()
    if len(episodes) > 1:
        episode = None
        season = int(episodes[0][1:3])
    resolution = _resolution(readable)
    source = _source(readable)
    hdr = _hdr(readable)
    encoding = _encoding(readable)
    audio_languages = _audio_languages(readable)
    media_name = canonical_media_name(page_title)
    return Release(
        id=release_id(page_url, download_link),
        title=title,
        url=page_url,
        download_link=html.unescape(download_link).strip(),
        size=size,
        resolution=resolution,
        source=source,
        language=_language(readable, audio_languages),
        hdr=hdr,
        encoding=encoding,
        media_type=resolved_media_type,
        episode=episode,
        size_bytes=size_bytes,
        media_name=media_name,
        year=metadata_year,
        season=season,
        link_name=link_display_name(download_link, label),
        original_title=_original_title(page_title, media_name),
        part=_part(readable),
        edition=_edition(readable),
        video_format=_video_format(resolution, source, hdr, encoding),
        audio_languages=audio_languages,
        original_language=normalize_language(metadata_original_language),
        episodes=episodes,
    )


def sort_releases(releases: Iterable[Release]) -> List[Release]:
    resolution_rank = {"4320p": 5, "2160p": 4, "1080p": 3, "720p": 2, "480p": 1}
    language_rank = {"国英双语": 4, "国语": 3, "英语": 2, "粤语": 1}
    hdr_rank = {"Dolby Vision": 4, "HDR10+": 3, "HDR10": 2, "HDR": 1}
    source_rank = {"Remux": 4, "BluRay": 3, "WEB-DL": 2, "HDTV": 1, "HD": 1}
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
