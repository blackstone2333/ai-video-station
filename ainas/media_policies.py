"""Internal media-type policies for the supported public media enum.

The API continues to expose only ``movie``, ``tv``, ``anime``, and ``custom``.
This module centralizes the different internal behaviours those values select.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping


NamingLayout = Literal["movie", "episodic", "preserve"]
TargetLayout = Literal["title", "season", "preserve"]


class UnsupportedMediaType(ValueError):
    """Raised when a caller attempts to use a type outside the public enum."""


@dataclass(frozen=True)
class MediaPolicy:
    media_type: str
    episodic: bool
    rename_enabled: bool
    naming_layout: NamingLayout
    target_layout: TargetLayout
    category_field: str
    download_path_field: str
    medialib_path_field: str

    def category_for(self, settings: Any) -> str:
        return str(getattr(settings, self.category_field))

    def download_path_for(self, settings: Any) -> Path:
        return Path(getattr(settings, self.download_path_field))

    def medialib_path_for(self, settings: Any) -> Path:
        return Path(getattr(settings, self.medialib_path_field))


MEDIA_POLICIES: Mapping[str, MediaPolicy] = {
    "movie": MediaPolicy(
        media_type="movie",
        episodic=False,
        rename_enabled=True,
        naming_layout="movie",
        target_layout="title",
        category_field="qb_movie_category",
        download_path_field="download_movie_path",
        medialib_path_field="medialib_movie_path",
    ),
    "tv": MediaPolicy(
        media_type="tv",
        episodic=True,
        rename_enabled=True,
        naming_layout="episodic",
        target_layout="season",
        category_field="qb_tv_category",
        download_path_field="download_tv_path",
        medialib_path_field="medialib_tv_path",
    ),
    "anime": MediaPolicy(
        media_type="anime",
        episodic=True,
        rename_enabled=True,
        naming_layout="episodic",
        target_layout="season",
        category_field="qb_anime_category",
        download_path_field="download_anime_path",
        medialib_path_field="medialib_anime_path",
    ),
    "custom": MediaPolicy(
        media_type="custom",
        episodic=False,
        rename_enabled=False,
        naming_layout="preserve",
        target_layout="preserve",
        category_field="qb_custom_category",
        download_path_field="download_custom_path",
        medialib_path_field="medialib_custom_path",
    ),
}


def policy_for(media_type: str) -> MediaPolicy:
    try:
        return MEDIA_POLICIES[media_type]
    except KeyError as exc:
        raise UnsupportedMediaType(f"unsupported media type: {media_type!r}") from exc
