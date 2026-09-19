from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .quality import Release, detect_episode, detect_season, expand_episode


VIEWING_MODES = {"daily", "collection", "compact"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}


@dataclass(frozen=True)
class ResourceProfile:
    mode: str
    resolution_order: Tuple[str, ...]
    source_order: Tuple[str, ...]
    audio_order: Tuple[str, ...] = ("multi", "original", "zh", "en", "other", "unknown")
    size_preference: str = "smaller"

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        for key in ("resolution_order", "source_order", "audio_order"):
            value[key] = list(value[key])
        return value


PROFILES = {
    "daily": ResourceProfile(
        mode="daily",
        resolution_order=("1080p", "2160p", "720p", "4320p", "480p", "unknown"),
        source_order=("Remux", "BluRay", "WEB-DL", "HDTV", "HD", "unknown"),
        size_preference="smaller",
    ),
    "collection": ResourceProfile(
        mode="collection",
        resolution_order=("4320p", "2160p", "1080p", "720p", "480p", "unknown"),
        source_order=("Remux", "BluRay", "WEB-DL", "HDTV", "HD", "unknown"),
        size_preference="larger",
    ),
    "compact": ResourceProfile(
        mode="compact",
        resolution_order=("720p", "1080p", "480p", "2160p", "4320p", "unknown"),
        source_order=("WEB-DL", "BluRay", "HDTV", "HD", "Remux", "unknown"),
        size_preference="smaller",
    ),
}


def profile_for(
    mode: str | None,
    snapshot: Mapping[str, Any] | None = None,
    media_type: str | None = None,
) -> ResourceProfile:
    selected = mode if mode in VIEWING_MODES else "daily"
    baseline = PROFILES[selected]
    default_resolution = baseline.resolution_order
    if selected == "daily" and media_type == "movie":
        default_resolution = ("2160p", "1080p", "720p", "4320p", "480p", "unknown")
    if not snapshot:
        if default_resolution != baseline.resolution_order:
            return ResourceProfile(
                mode=selected,
                resolution_order=default_resolution,
                source_order=baseline.source_order,
                audio_order=baseline.audio_order,
                size_preference=baseline.size_preference,
            )
        return baseline
    return ResourceProfile(
        mode=selected,
        resolution_order=tuple(snapshot.get("resolution_order") or default_resolution),
        source_order=tuple(snapshot.get("source_order") or baseline.source_order),
        audio_order=tuple(snapshot.get("audio_order") or baseline.audio_order),
        size_preference=str(snapshot.get("size_preference") or baseline.size_preference),
    )


def _ordered_score(value: str, order: Sequence[str]) -> int:
    try:
        return len(order) - order.index(value)
    except ValueError:
        return len(order) - order.index("unknown") if "unknown" in order else 0


def _audio_bucket(release: Release) -> str:
    languages = tuple(release.audio_languages or ())
    if len(set(languages)) >= 2 or release.language == "多语言":
        return "multi"
    if release.original_language and release.original_language in languages:
        return "original"
    if "zh" in languages or "yue" in languages:
        return "zh"
    if "en" in languages:
        return "en"
    if languages:
        return "other"
    return "unknown"


def release_score(release: Release, profile: ResourceProfile) -> Tuple[Any, ...]:
    size = int(release.size_bytes or 0)
    return (
        _ordered_score(release.resolution or "unknown", profile.resolution_order),
        _ordered_score(release.source or "unknown", profile.source_order),
        _ordered_score(_audio_bucket(release), profile.audio_order),
        size if profile.size_preference == "larger" else -size,
    )


def rank_releases(releases: Iterable[Release], profile: ResourceProfile) -> List[Release]:
    return sorted(releases, key=lambda item: release_score(item, profile), reverse=True)


def select_episode_files(files: Iterable[Mapping[str, Any]], wanted_episodes: Sequence[str]) -> Dict[str, Any]:
    values = [dict(item) for item in files if item.get("name") is not None]
    wanted = {episode for value in wanted_episodes for episode in expand_episode(value)}
    seasons = {int(item[1:3]) for item in wanted if len(item) >= 6 and item.startswith("S")}
    video_episodes: Dict[int, set[str]] = {}
    subtitle_episodes: Dict[int, set[str]] = {}
    unparsed_videos: List[int] = []
    all_indices: List[int] = []
    for fallback_index, item in enumerate(values):
        index = int(item.get("index") if item.get("index") is not None else fallback_index)
        all_indices.append(index)
        name = str(item.get("name") or "")
        suffix = PurePosixPath(name).suffix.casefold()
        season = detect_season(name)
        if season is None and len(seasons) == 1:
            season = next(iter(seasons))
        episodes = set(expand_episode(detect_episode(PurePosixPath(name).name, allow_numeric_prefix=True), season))
        if suffix in VIDEO_EXTENSIONS:
            if episodes:
                video_episodes[index] = episodes
            elif not any(token in name.casefold() for token in ("sample", "trailer", "preview", "预告")):
                unparsed_videos.append(index)
        elif suffix in SUBTITLE_EXTENSIONS and episodes:
            subtitle_episodes[index] = episodes

    # A joined video is indivisible. Its explicitly named companion episodes
    # must travel with the missing episode; report that coverage to callers.
    selected_videos = {index: episodes for index, episodes in video_episodes.items() if episodes & wanted}
    covered = set().union(*selected_videos.values()) if selected_videos else set()
    matched = sorted(covered & wanted)
    if not wanted or unparsed_videos or set(matched) != wanted:
        return {
            "safe": False,
            "reason": "episode-files-not-separable",
            "selected_indices": [],
            "skipped_indices": all_indices,
            "matched_episodes": matched,
        }
    selected = sorted(
        list(selected_videos)
        + [index for index, episodes in subtitle_episodes.items() if episodes <= covered]
    )
    return {
        "safe": True,
        "reason": None,
        "selected_indices": selected,
        "skipped_indices": sorted(set(all_indices) - set(selected)),
        "matched_episodes": matched,
        "covered_episodes": sorted(covered),
    }
