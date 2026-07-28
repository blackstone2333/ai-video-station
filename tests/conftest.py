from __future__ import annotations

from pathlib import Path

import pytest

from ainas.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        sixv_base_url="https://sixv.test",
        sixv_fallback_urls="https://backup.test",
        sixv_address_page="https://address.test",
        request_timeout_seconds=1,
        scheduler_enabled=False,
        auto_watch_on_empty=True,
        api_key=None,
        qb_host=None,
    )
