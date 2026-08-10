import json

import pytest

from ainas.config import Settings
from ainas.errors import ConflictError, ValidationAppError
from ainas.runtime_settings import DOWNLOADER_REGISTRY, DownloaderManager, DownloaderSettingsRepository, SystemSettingsRepository


def test_downloader_settings_persist_mask_secrets_and_reload(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    repository = DownloaderSettingsRepository(settings)
    values = repository.update(
        {
            "downloader_type": "transmission",
            "transmission_host": "nas.local",
            "transmission_port": 9092,
            "transmission_username": "media",
            "transmission_password": "secret",
            "transmission_verify_ssl": False,
        }
    )
    assert values["downloader_type"] == "transmission"
    assert values["transmission_password_configured"] is True
    assert "transmission_password" not in values
    assert json.loads(settings.downloader_settings_path.read_text())["transmission_password"] == "secret"

    reloaded_settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    reloaded = DownloaderSettingsRepository(reloaded_settings).get()
    assert reloaded["transmission_host"] == "nas.local"
    assert reloaded_settings.transmission_password.get_secret_value() == "secret"
    with pytest.raises(ValidationAppError):
        repository.update({"downloader_type": "unsupported"})


def test_system_settings_validate_persist_and_reload(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    repository = SystemSettingsRepository(settings)
    assert repository.update({"watchlist_check_hours": 72}) == {"watchlist_check_hours": 72}
    assert SystemSettingsRepository(Settings(data_dir=tmp_path, scheduler_enabled=False)).get()["watchlist_check_hours"] == 72
    with pytest.raises(ValidationAppError):
        repository.update({"watchlist_check_hours": 2})


def test_downloader_manager_rebuilds_client_when_type_changes(tmp_path, monkeypatch):
    class FakeQB:
        def __init__(self, settings): self.settings = settings
        @property
        def configured(self): return True
        def status(self): return {"client": "qbittorrent"}

    class FakeTransmission(FakeQB):
        def status(self): return {"client": "transmission"}

    monkeypatch.setattr("ainas.runtime_settings.QBittorrentClient", FakeQB)
    monkeypatch.setattr("ainas.runtime_settings.TransmissionClient", FakeTransmission)
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    manager = DownloaderManager(settings)
    assert manager.configured is True
    assert manager.status()["client"] == "qbittorrent"
    settings.downloader_type = "transmission"
    assert manager.status()["client"] == "transmission"


def test_downloader_capability_registry_exposes_structured_unsupported_operation(tmp_path, monkeypatch):
    class FakeTransmission:
        def __init__(self, settings): pass
        @property
        def configured(self): return True

    monkeypatch.setattr("ainas.runtime_settings.TransmissionClient", FakeTransmission)
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False, downloader_type="transmission")
    manager = DownloaderManager(settings)
    assert DOWNLOADER_REGISTRY["qbittorrent"].supports("categories")
    assert manager.supports("categories") is False
    with pytest.raises(ConflictError, match="does not support 'categories'"):
        manager.categories()


def test_downloader_manager_forwards_qb_submission_reconciliation(tmp_path, monkeypatch):
    class FakeQB:
        def __init__(self, settings):
            self.settings = settings

        @property
        def configured(self):
            return True

        def reconcile_submission(self, **criteria):
            return {"hash": "a" * 40, "criteria": criteria}

    monkeypatch.setattr("ainas.runtime_settings.QBittorrentClient", FakeQB)
    manager = DownloaderManager(Settings(data_dir=tmp_path, scheduler_enabled=False))

    result = manager.reconcile_submission(
        category="Media-Naming",
        save_path="/Downloads/TV",
        root_name="Show (2026)",
    )

    assert result["hash"] == "a" * 40
    assert result["criteria"]["root_name"] == "Show (2026)"
    assert manager.supports("reconcile_submission") is True


def test_transmission_reconciliation_is_an_explicit_noop(tmp_path, monkeypatch):
    class FakeTransmission:
        def __init__(self, settings):
            self.settings = settings

        @property
        def configured(self):
            return True

    monkeypatch.setattr("ainas.runtime_settings.TransmissionClient", FakeTransmission)
    manager = DownloaderManager(
        Settings(data_dir=tmp_path, scheduler_enabled=False, downloader_type="transmission")
    )

    assert manager.reconcile_submission(
        category="TV", save_path="/Downloads/TV", root_name="Show (2026)"
    ) is None
    assert manager.supports("reconcile_submission") is False
