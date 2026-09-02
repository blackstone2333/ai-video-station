from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "fnos"
IMAGE = (
    "ghcr.io/blackstone2333/ai-video-station"
    "@sha256:2a79686112045f2cb3e43a9ad7c7b588808a90ea54d772743b16121e2d2bc541"
)


def _manifest() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (PACKAGE / "manifest").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_fnos_manifest_and_user_copy_are_player_neutral():
    manifest = _manifest()

    assert manifest["appname"] == "ai-video-station"
    assert manifest["version"] == "2.2.0"
    assert manifest["platform"] == "all"
    assert manifest["source"] == "thirdparty"
    assert manifest["service_port"] == "16666"
    assert "标准媒体目录" in manifest["desc"]
    assert not any(brand in manifest["desc"].casefold() for brand in ("emby", "jellyfin"))

    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    assert all(name in readme for name in ("飞牛影视", "Emby", "Jellyfin"))


def test_fnos_json_compose_and_required_files_are_valid():
    required = [
        "manifest",
        "config/privilege",
        "config/resource",
        "ICON.PNG",
        "ICON_256.PNG",
        "app/docker/docker-compose.yaml",
        "app/ui/config",
        "app/ui/images/icon_64.png",
        "app/ui/images/icon_256.png",
        "app/bin/configure",
        "cmd/main",
        "cmd/install_init",
        "cmd/install_callback",
        "cmd/upgrade_init",
        "cmd/upgrade_callback",
        "cmd/config_init",
        "cmd/config_callback",
        "cmd/uninstall_init",
        "cmd/uninstall_callback",
        "wizard/install",
        "wizard/config",
    ]
    assert all((PACKAGE / value).is_file() for value in required)

    privilege = json.loads((PACKAGE / "config/privilege").read_text(encoding="utf-8"))
    resource = json.loads((PACKAGE / "config/resource").read_text(encoding="utf-8"))
    ui = json.loads((PACKAGE / "app/ui/config").read_text(encoding="utf-8"))
    install_wizard = json.loads((PACKAGE / "wizard/install").read_text(encoding="utf-8"))
    config_wizard = json.loads((PACKAGE / "wizard/config").read_text(encoding="utf-8"))
    compose = yaml.safe_load((PACKAGE / "app/docker/docker-compose.yaml").read_text(encoding="utf-8"))

    assert privilege["defaults"]["run-as"] == "package"
    assert resource["data-share"]["shares"] == [{"name": "ai-video-station/media"}]
    assert ui[".url"]["ai-video-station.Application"]["url"] == "/admin"
    assert install_wizard and config_wizard

    service = compose["services"]["ai-video-station"]
    assert service["image"] == IMAGE
    assert service["user"] == "${TRIM_UID}:${TRIM_GID}"
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["volumes"] == [
        "${TRIM_PKGVAR}:/data:rw",
        "${TRIM_DATA_SHARE_PATHS}:/medialib:rw",
    ]


def test_fnos_scripts_are_executable_and_shell_syntax_is_valid():
    scripts = [PACKAGE / "build.sh", PACKAGE / "app/bin/configure", *sorted((PACKAGE / "cmd").iterdir())]
    for script in scripts:
        assert script.stat().st_mode & stat.S_IXUSR
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_fnos_configure_creates_safe_defaults_and_media_tree(tmp_path: Path):
    pkgvar = tmp_path / "data"
    media_root = tmp_path / "media"
    log_file = tmp_path / "lifecycle.log"
    key = "A" * 32
    env = {
        "PATH": os.environ["PATH"],
        "TRIM_PKGVAR": str(pkgvar),
        "TRIM_DATA_SHARE_PATHS": str(media_root),
        "TRIM_TEMP_LOGFILE": str(log_file),
    }

    subprocess.run([str(PACKAGE / "app/bin/configure"), key], check=True, env=env)

    env_file = pkgvar / "avs.env"
    settings = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert settings["API_KEY"] == key
    assert settings["MEDIALIB_BASE_PATH"] == str(media_root)
    assert settings["MEDIALIB_HARDLINK_ENABLED"] == "true"
    assert settings["CLEANUP_AUTO_SCAN_ENABLED"] == "false"
    assert settings["CLEANUP_AUTO_EXECUTE_ENABLED"] == "false"
    assert settings["CLEANUP_AUTO_DELETE_SOURCE"] == "false"

    expected = {
        "Downloads/Movie",
        "Downloads/TV",
        "Downloads/Anime",
        "Downloads/Custom",
        "Library/Movies",
        "Library/TV",
        "Library/Anime",
        "Library/Custom",
    }
    assert all((media_root / relative).is_dir() for relative in expected)
