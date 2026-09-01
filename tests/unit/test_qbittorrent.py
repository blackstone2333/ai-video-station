from urllib.parse import parse_qs

import pytest
import responses

from ainas.config import Settings
from ainas.errors import ServiceUnavailableError, UpstreamError
from ainas.path_settings import PathSettingsRepository
from ainas.qbittorrent import QBittorrentClient, torrent_hash
from tests.unit.test_torrent_meta import TORRENT


MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def test_hash_and_unconfigured_status(settings):
    assert torrent_hash(MAGNET) == "a" * 40
    assert torrent_hash("https://x.test/file.torrent") is None
    assert QBittorrentClient(settings).status()["configured"] is False


def qb_settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        qb_host="qb.test",
        qb_port=8080,
        qb_username="admin",
        qb_password="secret",
        scheduler_enabled=False,
    )


@responses.activate
def test_login_accepts_qbittorrent_5_empty_204_response(tmp_path):
    settings = qb_settings(tmp_path)
    responses.post(f"{settings.qb_base_url}/api/v2/auth/login", body="", status=204)

    QBittorrentClient(settings).login()


@responses.activate
def test_login_accepts_legacy_ok_response(tmp_path):
    settings = qb_settings(tmp_path)
    responses.post(f"{settings.qb_base_url}/api/v2/auth/login", body="Ok.", status=200)

    QBittorrentClient(settings).login()


@responses.activate
def test_login_rejects_legacy_fails_response(tmp_path):
    settings = qb_settings(tmp_path)
    responses.post(f"{settings.qb_base_url}/api/v2/auth/login", body="Fails.", status=200)

    with pytest.raises(ServiceUnavailableError, match="rejected the configured username or password"):
        QBittorrentClient(settings).login()


@pytest.mark.parametrize("status", [401, 403])
@responses.activate
def test_login_rejects_http_auth_errors(tmp_path, status):
    settings = qb_settings(tmp_path)
    responses.post(f"{settings.qb_base_url}/api/v2/auth/login", body="", status=status)

    with pytest.raises(UpstreamError, match="qBittorrent: login failed"):
        QBittorrentClient(settings).login()


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
    responses.get(f"{base}/api/v2/torrents/categories", json={settings.qb_movie_category: {}}, status=200)
    responses.post(f"{base}/api/v2/torrents/add", body="Ok.", status=200)
    client = QBittorrentClient(settings)
    PathSettingsRepository(settings).update({"download_movie_path": "/volume1/video/Incoming/Films"})
    result = client.add_download(MAGNET, settings.qb_movie_category)
    assert result["qb_task_id"] == "a" * 40
    added_payload = parse_qs(responses.calls[2].request.body)
    assert added_payload["savepath"] == ["/volume1/video/Incoming/Films"]
    assert added_payload["autoTMM"] == ["false"]

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
    responses.get(f"{base}/api/v2/torrents/categories", json={"sixv-tv": {}}, status=200)
    responses.post(f"{base}/api/v2/torrents/setCategory", body="", status=200)
    responses.post(f"{base}/api/v2/torrents/resume", body="", status=200)
    responses.post(f"{base}/api/v2/torrents/recheck", body="", status=200)
    responses.post(f"{base}/api/v2/torrents/setLocation", body="", status=200)
    client.rename_file("abc", "01.mp4", "剧名 - S01E01.mp4")
    client.rename_folder("abc", "第1季", "Season 01")
    client.rename_torrent("abc", "剧名")
    client.set_category("abc", "sixv-tv")
    client.resume("abc")
    client.recheck("abc")
    client.set_location("abc", "/Downloads/TV")


@responses.activate
def test_tasks_normalize_completed_state_when_qb_progress_is_stale(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        qb_host="qb.test",
        qb_password="secret",
        scheduler_enabled=False,
    )
    base = "http://qb.test:8080"
    responses.post(f"{base}/api/v2/auth/login", body="Ok.")
    responses.get(
        f"{base}/api/v2/torrents/info",
        json=[
            {
                "hash": "abc",
                "name": "Completed episode",
                "state": "pausedUP",
                "progress": 0,
                "size": 512,
                "downloaded": 0,
                "completion_on": 100,
            },
            {
                "hash": "missing",
                "name": "Missing episode",
                "state": "missingFiles",
                "progress": 0,
                "size": 512,
                "downloaded": 512,
                "completion_on": 100,
            },
        ],
    )

    completed, missing = QBittorrentClient(settings).tasks()

    assert completed["progress"] == 1.0
    assert completed["completed"] is True
    assert missing["progress"] == 0.0
    assert missing["completed"] is False


@responses.activate
def test_existing_category_is_reused_without_rewriting_qbittorrent_configuration(tmp_path):
    settings = Settings(data_dir=tmp_path, qb_host="qb.test", scheduler_enabled=False)
    client = QBittorrentClient(settings)
    PathSettingsRepository(settings).update({"download_tv_path": "/volume1/video/Incoming/Series"})
    base = "http://qb.test:8080"
    responses.get(f"{base}/api/v2/torrents/categories", json={settings.qb_tv_category: {"savePath": "/other"}})

    client.ensure_category(settings.qb_tv_category)

    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "GET"


@responses.activate
def test_missing_category_is_created_once_without_editing_existing_configuration(tmp_path):
    settings = Settings(data_dir=tmp_path, qb_host="qb.test", scheduler_enabled=False)
    client = QBittorrentClient(settings)
    base = "http://qb.test:8080"
    responses.get(f"{base}/api/v2/torrents/categories", json={})
    responses.post(f"{base}/api/v2/torrents/createCategory", body="Ok.")

    client.ensure_category(settings.qb_tv_category)

    assert [call.request.method for call in responses.calls] == ["GET", "POST"]
    payload = parse_qs(responses.calls[1].request.body)
    assert payload["category"] == [settings.qb_tv_category]


@responses.activate
def test_add_reconciles_ambiguous_qb_response_by_hash(tmp_path):
    settings = Settings(data_dir=tmp_path, qb_host="qb.test", qb_password="secret", scheduler_enabled=False)
    base = "http://qb.test:8080"
    responses.post(f"{base}/api/v2/auth/login", body="Ok.")
    responses.get(f"{base}/api/v2/torrents/categories", json={settings.qb_movie_category: {}})
    responses.post(f"{base}/api/v2/torrents/add", body="Fails.")
    responses.post(f"{base}/api/v2/auth/login", body="Ok.")
    responses.get(f"{base}/api/v2/torrents/info", json=[{"hash": "a" * 40, "name": "Movie"}])

    result = QBittorrentClient(settings).add_download(MAGNET, settings.qb_movie_category)

    assert result["qb_task_id"] == "a" * 40


@responses.activate
def test_qbittorrent_uploads_torrent_files(tmp_path):
    settings = Settings(data_dir=tmp_path, qb_host="qb.test", qb_password="secret", scheduler_enabled=False)
    base = "http://qb.test:8080"
    responses.post(f"{base}/api/v2/auth/login", body="Ok.")
    responses.get(f"{base}/api/v2/torrents/categories", json={settings.qb_movie_category: {}})
    responses.post(f"{base}/api/v2/torrents/add", body="Ok.")
    result = QBittorrentClient(settings).add_torrent_file(
        TORRENT,
        "movie.torrent",
        settings.qb_movie_category,
        save_path="/volume1/video/Incoming/Films",
    )
    assert result["torrent_name"] == "Movie.mkv"
    assert result["qb_task_id"]
    body = responses.calls[2].request.body
    assert b"movie.torrent" in body and b"Movie.mkv" in body
    assert b"/volume1/video/Incoming/Films" in body


@responses.activate
def test_qbittorrent_can_submit_paused_and_select_episode_files(tmp_path):
    settings = Settings(data_dir=tmp_path, qb_host="qb.test", qb_password="secret", scheduler_enabled=False)
    base = "http://qb.test:8080"
    responses.post(f"{base}/api/v2/auth/login", body="Ok.")
    responses.get(f"{base}/api/v2/torrents/categories", json={settings.qb_tv_category: {}})
    responses.post(f"{base}/api/v2/torrents/add", body="Ok.")
    responses.post(f"{base}/api/v2/torrents/filePrio", body="Ok.")
    responses.post(f"{base}/api/v2/torrents/filePrio", body="Ok.")

    client = QBittorrentClient(settings)
    client.add_download(MAGNET, settings.qb_tv_category, paused=True)
    client.set_file_priorities("a" * 40, selected_indices=[1, 3], skipped_indices=[0, 2])

    added = parse_qs(responses.calls[2].request.body)
    assert added["paused"] == ["true"]
    skipped = parse_qs(responses.calls[3].request.body)
    selected = parse_qs(responses.calls[4].request.body)
    assert skipped == {"hash": ["a" * 40], "id": ["0|2"], "priority": ["0"]}
    assert selected == {"hash": ["a" * 40], "id": ["1|3"], "priority": ["1"]}
