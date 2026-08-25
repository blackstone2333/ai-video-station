from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ainas.config import Settings


@pytest.mark.parametrize(
    "changes",
    [
        {"sixv_base_url": "ftp://invalid.test"},
        {"log_level": "verbose"},
        {"qb_movie_category": ""},
        {"qb_movie_category": "x" * 101},
        {"medialib_base_path": Path("relative")},
    ],
)
def test_settings_reject_invalid_public_configuration(tmp_path, changes):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, scheduler_enabled=False, **changes)


def test_settings_build_urls_origins_and_category_paths(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        scheduler_enabled=False,
        qb_host="https://qb.example.test",
        qb_port=9443,
        transmission_host="http://transmission.example.test",
        transmission_port=9092,
        cors_origins="https://one.test/, https://two.test",
    )

    assert settings.qb_base_url == "https://qb.example.test:9443"
    assert settings.transmission_base_url == "http://transmission.example.test:9092"
    assert settings.allowed_origins == ["https://one.test", "https://two.test"]
    assert settings.download_path_for_category(settings.qb_anime_category) == settings.download_anime_path
    assert settings.download_path_for_category(settings.qb_custom_category) == settings.download_custom_path
    assert settings.download_path_for_category("unknown") == settings.downloads_base_path
