from urllib.parse import parse_qs

import responses

from ainas.config import Settings
from ainas.path_settings import PathSettingsRepository
from ainas.qbittorrent import QBittorrentClient, torrent_hash


MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def test_hash_and_unconfigured_status(settings):
    assert torrent_hash(MAGNET) == "a" * 40
    assert torrent_hash("https://x.test/file.torrent") is None
    assert QBittorrentClient(settings).status()["configured"] is False


@responses.activate
def test_add_status_and_tasks(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        qb_host="qb.test",
        qb_port=8080,
        qb_username="admin",
        qb_password="secret",
        scheduler_enabled=False,
    )
    base = "http://qb.test:8080"
    responses.post(f"{base}/api/v2/auth/login", body="Ok.", status=200)
    responses.post(f"{base}/api/v2/torrents/createCategory", body="Ok.", status=200)
    responses.post(f"{base}/api/v2/torrents/add", body="Ok.", status=200)
    client = QBittorrentClient(settings)
    PathSettingsRepository(settings).update({"download_movie_path": "/volume1/video/Incoming/Films"})
    result = client.add_download(MAGNET, settings.qb_movie_category)
    assert result["qb_task_id"] == "a" * 40
    added_payload = parse_qs(responses.calls[2].request.body)
    assert added_payload["savepath"] == ["/volume1/video/Incoming/Films"]

    responses.post(f"{base}/api/v2/auth/login", body="Ok.", status=200)
    responses.get(f"{base}/api/v2/app/version", body="4.6.7", status=200)
    assert client.status()["version"] == "4.6.7"

    responses.post(f"{base}/api/v2/auth/login", body="Ok.", status=200)
    responses.get(
        f"{base}/api/v2/torrents/info",
        json=[{"hash": "abc", "name": "Movie", "state": "downloading", "progress": 0.5, "secret": "x"}],
        status=200,
    )
    tasks = client.tasks()
    assert tasks[0]["name"] == "Movie"
    assert "secret" not in tasks[0]

    responses.post(f"{base}/api/v2/auth/login", body="Ok.", status=200)
    responses.get(f"{base}/api/v2/torrents/info", json=[{"hash": "abc", "name": "Movie"}], status=200)
    assert client.torrent_info("abc")["name"] == "Movie"

    responses.post(f"{base}/api/v2/auth/login", body="Ok.", status=200)
    responses.get(
        f"{base}/api/v2/torrents/files",
        json=[{"index": 0, "name": "01.mp4", "size": 10, "progress": 0.5, "priority": 1}],
        status=200,
    )
    assert client.files("abc")[0]["name"] == "01.mp4"

    responses.post(f"{base}/api/v2/torrents/renameFile", body="", status=200)
    responses.post(f"{base}/api/v2/torrents/renameFolder", body="", status=200)
    responses.post(f"{base}/api/v2/torrents/rename", body="", status=200)
    responses.post(f"{base}/api/v2/torrents/createCategory", body="Ok.", status=200)
    responses.post(f"{base}/api/v2/torrents/setCategory", body="", status=200)
    responses.post(f"{base}/api/v2/torrents/resume", body="", status=200)
    client.rename_file("abc", "01.mp4", "剧名 - S01E01.mp4")
    client.rename_folder("abc", "第1季", "Season 01")
    client.rename_torrent("abc", "剧名")
    client.set_category("abc", "sixv-tv")
    client.resume("abc")


@responses.activate
def test_existing_category_is_updated_to_current_path(tmp_path):
    settings = Settings(data_dir=tmp_path, qb_host="qb.test", scheduler_enabled=False)
    client = QBittorrentClient(settings)
    PathSettingsRepository(settings).update({"download_tv_path": "/volume1/video/Incoming/Series"})
    base = "http://qb.test:8080"
    responses.post(f"{base}/api/v2/torrents/createCategory", status=409)
    responses.post(f"{base}/api/v2/torrents/editCategory", body="Ok.", status=200)

    client.ensure_category(settings.qb_tv_category)

    edited_payload = parse_qs(responses.calls[1].request.body)
    assert edited_payload["savePath"] == ["/volume1/video/Incoming/Series"]
