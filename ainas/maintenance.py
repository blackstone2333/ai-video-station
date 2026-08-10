"""Offline backup, verification, and restore helpers for runtime state.

This module deliberately has no dependency on the running application.  Stop the
service before making or restoring a backup so files cannot change mid-copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


FORMAT = "ai-video-station-backup"
VERSION = 1
MANIFEST = "manifest.json"


class MaintenanceError(Exception):
    """Raised for an invalid or unsafe maintenance operation."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MaintenanceError(f"清单包含不安全的路径：{value!r}")
    return path


def _inside(child: Path, parent: Path) -> Path:
    try:
        child.relative_to(parent)
    except ValueError as exc:
        raise MaintenanceError("路径超出允许的目录范围") from exc
    return child


def _regular_files(root: Path) -> Iterable[Path]:
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise MaintenanceError(f"拒绝备份符号链接：{candidate}")
        if candidate.is_file():
            resolved = candidate.resolve(strict=True)
            yield _inside(resolved, root)


def _copy_file(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    shutil.copyfile(source, destination)
    os.chmod(destination, mode)


def _backup_file(root: Path, relative: PurePosixPath) -> Path:
    """Return a regular archive file without following archive symlinks."""
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise MaintenanceError(f"备份包含符号链接：{relative.as_posix()}")
    return _inside(candidate.resolve(strict=True), root)


def _entry(source: Path, stored_path: str) -> dict[str, Any]:
    return {
        "path": stored_path,
        "sha256": _sha256(source),
        "size": source.stat().st_size,
        "mode": format(stat.S_IMODE(source.stat().st_mode) & 0o600, "04o"),
    }


def create_backup(data_dir: Path, backup_dir: Path, env_file: Path | None = None) -> Path:
    """Create a new versioned backup directory and return its path."""
    data_root = data_dir.resolve(strict=True)
    if not data_root.is_dir():
        raise MaintenanceError(f"数据目录不存在或不是目录：{data_dir}")
    base = backup_dir.resolve(strict=False)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    name = f"avs-backup-v{VERSION}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    target = base / name
    if target.exists():
        raise MaintenanceError(f"备份目录已存在，拒绝覆盖：{target}")
    target.mkdir(mode=0o700)
    entries: list[dict[str, Any]] = []
    try:
        for source in _regular_files(data_root):
            relative = source.relative_to(data_root).as_posix()
            stored = f"data/{relative}"
            destination = target / stored
            _copy_file(source, destination, 0o600)
            entries.append(_entry(destination, stored))
        if env_file is not None:
            env = env_file.resolve(strict=True)
            if env.is_symlink() or not env.is_file():
                raise MaintenanceError(".env 必须是普通文件")
            _copy_file(env, target / ".env", 0o600)
            entries.append(_entry(target / ".env", ".env"))
        manifest = {"format": FORMAT, "version": VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(), "files": entries}
        manifest_path = target / MANIFEST
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest_path, 0o600)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _load_manifest(backup: Path) -> tuple[Path, list[dict[str, Any]]]:
    root = backup.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise MaintenanceError("备份路径必须是普通目录")
    manifest_path = root / MANIFEST
    if manifest_path.is_symlink():
        raise MaintenanceError("备份清单不能是符号链接")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError("无法读取备份清单") from exc
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION or not isinstance(manifest.get("files"), list):
        raise MaintenanceError("不支持或损坏的备份清单")
    entries = manifest["files"]
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
            raise MaintenanceError("备份清单条目无效")
        relative = _safe_relative(entry["path"])
        if relative.as_posix() in seen or (relative.parts[0] != "data" and relative.as_posix() != ".env"):
            raise MaintenanceError("备份清单条目无效")
        seen.add(relative.as_posix())
    return root, entries


def verify_backup(backup: Path) -> int:
    root, entries = _load_manifest(backup)
    for entry in entries:
        relative = _safe_relative(entry["path"])
        source = _backup_file(root, relative)
        if not source.is_file() or _sha256(source) != entry["sha256"]:
            raise MaintenanceError(f"校验失败：{entry['path']}")
    return len(entries)


def _verify_sqlite_snapshot(staged_data: Path, stored_paths: set[str]) -> None:
    database = staged_data / "state.db"
    if not database.exists():
        return
    try:
        with sqlite3.connect(database, timeout=10) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise MaintenanceError(f"恢复的 SQLite 状态校验失败：{result}")
    except sqlite3.Error as exc:
        raise MaintenanceError("恢复的 SQLite 状态无法打开") from exc
    # A verification connection may create sidecars. Preserve only files that
    # belonged to the verified backup snapshot.
    for suffix in ("-wal", "-shm"):
        sidecar = staged_data / f"state.db{suffix}"
        if f"data/state.db{suffix}" not in stored_paths and sidecar.exists():
            sidecar.unlink()


def restore_backup(backup: Path, data_dir: Path, *, env_file: Path | None = None, restore_env: bool = False, overwrite: bool = False) -> int:
    """Restore a verified snapshot through an all-or-nothing directory swap."""
    root, entries = _load_manifest(backup)
    verify_backup(root)
    destination_root = data_dir.resolve(strict=False)
    if destination_root.is_symlink() or (destination_root.exists() and not destination_root.is_dir()):
        raise MaintenanceError(f"恢复目标不是目录：{data_dir}")
    if destination_root.exists() and any(destination_root.iterdir()) and not overwrite:
        raise MaintenanceError("恢复目标非空，默认拒绝覆盖；确认后使用 --overwrite")
    destination_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    env_entry = next((entry for entry in entries if entry["path"] == ".env"), None)
    env_destination: Path | None = None
    if restore_env:
        if env_file is None:
            raise MaintenanceError("恢复 .env 需要同时提供 --env-file")
        if env_entry is None:
            raise MaintenanceError("备份中不包含 .env")
        env_destination = env_file.resolve(strict=False)
        if env_destination.is_symlink():
            raise MaintenanceError(f"拒绝写入符号链接：{env_destination}")
        if env_destination.exists() and not overwrite:
            raise MaintenanceError(f"目标文件已存在，默认拒绝覆盖：{env_destination}")
        env_destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    staged_data = Path(
        tempfile.mkdtemp(prefix=f".{destination_root.name}.restore-", dir=str(destination_root.parent))
    )
    os.chmod(staged_data, 0o700)
    staged_env: Path | None = None
    stored_paths = {str(entry["path"]) for entry in entries}
    restored = 0
    try:
        for entry in entries:
            relative = _safe_relative(entry["path"])
            if relative.as_posix() == ".env":
                continue
            destination = staged_data.joinpath(*relative.parts[1:]).resolve(strict=False)
            _inside(destination, staged_data)
            source = _backup_file(root, relative)
            _copy_file(source, destination, 0o600)
            if _sha256(destination) != entry["sha256"]:
                raise MaintenanceError(f"暂存恢复校验失败：{entry['path']}")
            restored += 1
        _verify_sqlite_snapshot(staged_data, stored_paths)

        if env_destination is not None and env_entry is not None:
            fd, staged_name = tempfile.mkstemp(
                prefix=f".{env_destination.name}.restore-", dir=str(env_destination.parent)
            )
            os.close(fd)
            staged_env = Path(staged_name)
            _copy_file(_backup_file(root, PurePosixPath(".env")), staged_env, 0o600)
            if _sha256(staged_env) != env_entry["sha256"]:
                raise MaintenanceError("暂存恢复校验失败：.env")

        rollback_data = destination_root.parent / f".{destination_root.name}.rollback-{uuid.uuid4().hex}"
        rollback_env = (
            env_destination.parent / f".{env_destination.name}.rollback-{uuid.uuid4().hex}"
            if env_destination is not None
            else None
        )
        data_had_original = destination_root.exists()
        env_had_original = bool(env_destination and env_destination.exists())
        data_committed = env_committed = False
        try:
            if data_had_original:
                os.replace(destination_root, rollback_data)
            os.replace(staged_data, destination_root)
            data_committed = True
            if env_destination is not None and staged_env is not None:
                if env_had_original and rollback_env is not None:
                    os.replace(env_destination, rollback_env)
                os.replace(staged_env, env_destination)
                env_committed = True
        except OSError as exc:
            try:
                if env_committed and env_destination and env_destination.exists():
                    env_destination.unlink()
                if env_had_original and rollback_env and rollback_env.exists() and env_destination:
                    os.replace(rollback_env, env_destination)
                if data_committed and destination_root.exists():
                    shutil.rmtree(destination_root)
                if data_had_original and rollback_data.exists():
                    os.replace(rollback_data, destination_root)
            except OSError as rollback_exc:
                raise MaintenanceError(
                    f"恢复提交失败且自动回滚失败；保留回滚目录：{rollback_data}"
                ) from rollback_exc
            raise MaintenanceError("恢复提交失败，原数据已回滚") from exc

        if rollback_data.exists():
            shutil.rmtree(rollback_data)
        if rollback_env and rollback_env.exists():
            rollback_env.unlink()
        return restored + (1 if env_committed else 0)
    finally:
        if staged_data.exists():
            shutil.rmtree(staged_data, ignore_errors=True)
        if staged_env is not None and staged_env.exists():
            staged_env.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ainas.maintenance", description="AI Video Station 离线备份与恢复（操作前必须停止服务）")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="创建版本化备份；先停止服务")
    backup.add_argument("--data-dir", type=Path, default=Path("data"))
    backup.add_argument("--backup-dir", type=Path, default=Path("backups"))
    backup.add_argument("--include-env", action="store_true")
    backup.add_argument("--env-file", type=Path, default=Path(".env"))
    verify = commands.add_parser("verify", help="校验清单和 SHA-256")
    verify.add_argument("backup", type=Path)
    restore = commands.add_parser("restore", help="恢复已校验备份；先停止服务")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--data-dir", type=Path, default=Path("data"))
    restore.add_argument("--restore-env", action="store_true")
    restore.add_argument("--env-file", type=Path, default=Path(".env"))
    restore.add_argument("--overwrite", action="store_true", help="允许覆盖同名文件和非空数据目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            path = create_backup(args.data_dir, args.backup_dir, args.env_file if args.include_env else None)
            print(f"备份完成：{path}")
        elif args.command == "verify":
            print(f"校验通过：{verify_backup(args.backup)} 个文件")
        else:
            count = restore_backup(args.backup, args.data_dir, env_file=args.env_file, restore_env=args.restore_env, overwrite=args.overwrite)
            print(f"恢复完成：{count} 个文件")
    except MaintenanceError as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
