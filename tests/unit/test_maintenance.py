import json
from pathlib import Path

import pytest

from ainas.maintenance import MaintenanceError, create_backup, restore_backup, verify_backup


def _backup(data_dir: Path, backup_dir: Path) -> Path:
    return create_backup(data_dir, backup_dir)


def test_backup_verify_and_restore_round_trip(tmp_path: Path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "settings.json").write_text('{"key":"value"}', encoding="utf-8")
    backup = _backup(source, tmp_path / "backups")

    assert verify_backup(backup) == 1
    destination = tmp_path / "restore"
    assert restore_backup(backup, destination) == 1
    assert (destination / "nested" / "settings.json").read_text(encoding="utf-8") == '{"key":"value"}'
    assert (backup / "manifest.json").stat().st_mode & 0o077 == 0


def test_verify_rejects_checksum_failure(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "state.json").write_text("original", encoding="utf-8")
    backup = _backup(source, tmp_path / "backups")
    (backup / "data" / "state.json").write_text("changed", encoding="utf-8")

    with pytest.raises(MaintenanceError, match="校验失败"):
        verify_backup(backup)


def test_restore_rejects_manifest_path_traversal(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "state.json").write_text("safe", encoding="utf-8")
    backup = _backup(source, tmp_path / "backups")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "data/../../escape"
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MaintenanceError, match="不安全"):
        restore_backup(backup, tmp_path / "restore")


def test_restore_refuses_nonempty_target_without_overwrite(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "state.json").write_text("safe", encoding="utf-8")
    backup = _backup(source, tmp_path / "backups")
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "existing.json").write_text("keep", encoding="utf-8")

    with pytest.raises(MaintenanceError, match="默认拒绝覆盖"):
        restore_backup(backup, destination)


def test_overwrite_restore_is_exact_and_removes_stale_sqlite_sidecars(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "state.db").write_bytes(b"not-a-database-snapshot")
    backup = _backup(source, tmp_path / "backups")
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "state.db").write_bytes(b"old")
    (destination / "state.db-wal").write_bytes(b"stale-wal")
    (destination / "state.db-shm").write_bytes(b"stale-shm")

    with pytest.raises(MaintenanceError, match="SQLite"):
        restore_backup(backup, destination, overwrite=True)

    assert (destination / "state.db").read_bytes() == b"old"
    assert (destination / "state.db-wal").read_bytes() == b"stale-wal"


def test_overwrite_restore_replaces_snapshot_and_removes_unlisted_files(tmp_path: Path):
    import sqlite3

    source = tmp_path / "source"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as connection:
        connection.execute("CREATE TABLE state(value TEXT)")
        connection.execute("INSERT INTO state VALUES('new')")
    backup = _backup(source, tmp_path / "backups")
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "state.db").write_bytes(b"old")
    (destination / "state.db-wal").write_bytes(b"stale-wal")
    (destination / "state.db-shm").write_bytes(b"stale-shm")
    (destination / "unlisted.json").write_text("stale", encoding="utf-8")

    assert restore_backup(backup, destination, overwrite=True) == 1

    with sqlite3.connect(destination / "state.db") as connection:
        assert connection.execute("SELECT value FROM state").fetchone()[0] == "new"
    assert not (destination / "state.db-wal").exists()
    assert not (destination / "state.db-shm").exists()
    assert not (destination / "unlisted.json").exists()
