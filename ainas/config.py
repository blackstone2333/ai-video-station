from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "ai-nas-core"
    host: str = "0.0.0.0"
    port: int = Field(default=16666, ge=1, le=65535)
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    data_dir: Path = Path("data")

    sixv_base_url: str = "https://www.6vw.cc"
    sixv_fallback_urls: str = (
        "https://www.6vdyy.com,https://www.6v520.cc,https://www.xb6v.com,"
        "https://www.6vhao.tv,https://www.6vgood.net"
    )
    sixv_address_page: str = "https://www.6v123.net"
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    search_detail_limit: int = Field(default=20, ge=1, le=100)
    user_agent: str = "Mozilla/5.0 (compatible; ai-nas-core/1.3; +NAS)"

    downloader_type: str = "qbittorrent"
    qb_host: Optional[str] = None
    qb_port: int = Field(default=8080, ge=1, le=65535)
    qb_username: str = "admin"
    qb_password: SecretStr = SecretStr("")
    qb_use_https: bool = False
    qb_verify_ssl: bool = True
    qb_movie_category: str = "Movie"
    qb_tv_category: str = "TV"
    qb_anime_category: str = "Anime"

    transmission_host: Optional[str] = None
    transmission_port: int = Field(default=9091, ge=1, le=65535)
    transmission_username: str = ""
    transmission_password: SecretStr = SecretStr("")
    transmission_use_https: bool = False
    transmission_verify_ssl: bool = True
    transmission_rpc_path: str = "/transmission/rpc"

    downloads_base_path: Path = Path("/volume1/video/Downloads")
    download_movie_path: Path = Path("/volume1/video/Downloads/Movie")
    download_tv_path: Path = Path("/volume1/video/Downloads/TV")
    download_anime_path: Path = Path("/volume1/video/Downloads/Anime")

    watchlist_check_hours: int = Field(default=12, ge=1, le=168)
    watchlist_expire_days: int = Field(default=14, ge=1, le=365)
    scheduler_enabled: bool = True
    auto_watch_on_empty: bool = True

    naming_enabled: bool = True
    naming_check_minutes: int = Field(default=1, ge=1, le=60)
    naming_max_attempts: int = Field(default=10, ge=1, le=100)
    qb_naming_category: str = "Media-Naming"

    medialib_hardlink_enabled: bool = True
    medialib_base_path: Path = Path("/volume1/video")
    medialib_movie_path: Path = Path("/volume1/video/video/movies")
    medialib_tv_path: Path = Path("/volume1/video/video/tv")
    medialib_anime_path: Path = Path("/volume1/video/video/anime")
    medialib_mount_path: Path = Path("/medialib")

    api_key: Optional[SecretStr] = None
    cors_origins: str = ""
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10000)

    @field_validator("sixv_base_url", "sixv_address_page")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute HTTP(S) URL")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return value

    @field_validator("downloader_type")
    @classmethod
    def validate_downloader_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"qbittorrent", "transmission"}:
            raise ValueError("must be qbittorrent or transmission")
        return value

    @model_validator(mode="after")
    def validate_medialib_paths(self) -> "Settings":
        base = Path(os.path.normpath(str(self.medialib_base_path)))
        if not base.is_absolute():
            raise ValueError("MEDIALIB_BASE_PATH must be an absolute host path")
        if self.downloader_type == "transmission":
            for field_name, path in (
                ("DOWNLOADS_BASE_PATH", self.downloads_base_path),
                ("DOWNLOAD_MOVIE_PATH", self.download_movie_path),
                ("DOWNLOAD_TV_PATH", self.download_tv_path),
                ("DOWNLOAD_ANIME_PATH", self.download_anime_path),
            ):
                if not Path(os.path.normpath(str(path))).is_absolute():
                    raise ValueError(f"{field_name} must be an absolute host path")
        if not self.medialib_hardlink_enabled:
            return self

        for field_name, path in (
            ("MEDIALIB_MOVIE_PATH", self.medialib_movie_path),
            ("MEDIALIB_TV_PATH", self.medialib_tv_path),
            ("MEDIALIB_ANIME_PATH", self.medialib_anime_path),
            ("DOWNLOADS_BASE_PATH", self.downloads_base_path),
            ("DOWNLOAD_MOVIE_PATH", self.download_movie_path),
            ("DOWNLOAD_TV_PATH", self.download_tv_path),
            ("DOWNLOAD_ANIME_PATH", self.download_anime_path),
        ):
            normalized = Path(os.path.normpath(str(path)))
            if not normalized.is_absolute():
                raise ValueError(f"{field_name} must be an absolute host path")
            try:
                normalized.relative_to(base)
            except ValueError as exc:
                raise ValueError(f"{field_name} must be inside MEDIALIB_BASE_PATH") from exc
        if not self.medialib_mount_path.is_absolute():
            raise ValueError("MEDIALIB_MOUNT_PATH must be an absolute container path")
        return self

    @property
    def site_urls(self) -> List[str]:
        values = [self.sixv_base_url]
        values.extend(item.strip().rstrip("/") for item in self.sixv_fallback_urls.split(","))
        return list(dict.fromkeys(item for item in values if item))

    @property
    def allowed_origins(self) -> List[str]:
        return [item.strip().rstrip("/") for item in self.cors_origins.split(",") if item.strip()]

    @property
    def qb_base_url(self) -> Optional[str]:
        if not self.qb_host:
            return None
        host = self.qb_host.strip().rstrip("/")
        if host.startswith(("http://", "https://")):
            parsed = urlparse(host)
            if parsed.port:
                return host
            scheme = parsed.scheme
            hostname = parsed.hostname or ""
            return f"{scheme}://{hostname}:{self.qb_port}"
        scheme = "https" if self.qb_use_https else "http"
        return f"{scheme}://{host}:{self.qb_port}"

    @property
    def transmission_base_url(self) -> Optional[str]:
        if not self.transmission_host:
            return None
        host = self.transmission_host.strip().rstrip("/")
        if host.startswith(("http://", "https://")):
            parsed = urlparse(host)
            if parsed.port:
                return host
            scheme = parsed.scheme
            hostname = parsed.hostname or ""
            return f"{scheme}://{hostname}:{self.transmission_port}"
        scheme = "https" if self.transmission_use_https else "http"
        return f"{scheme}://{host}:{self.transmission_port}"

    def download_path_for_category(self, category: str) -> Path:
        if category == self.qb_movie_category:
            return self.download_movie_path
        if category == self.qb_tv_category:
            return self.download_tv_path
        if category == self.qb_anime_category:
            return self.download_anime_path
        return self.downloads_base_path

    @property
    def watchlist_path(self) -> Path:
        return self.data_dir / "watchlist.json"

    @property
    def naming_jobs_path(self) -> Path:
        return self.data_dir / "naming_jobs.json"

    @property
    def sites_path(self) -> Path:
        return self.data_dir / "sites.json"

    @property
    def path_settings_path(self) -> Path:
        return self.data_dir / "path_settings.json"

    def api_key_value(self) -> Optional[str]:
        if self.api_key is None:
            return None
        value = self.api_key.get_secret_value().strip()
        return value or None
