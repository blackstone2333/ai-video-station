from __future__ import annotations

import responses

from ainas.config import Settings
from ainas.path_settings import PathSettingsRepository
from ainas.transmission import TransmissionClient


RPC = "http://transmission.test:9091/transmission/rpc"


def transmission_settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        scheduler_enabled=False,
        downloader_type="transmission",
        transmission_host="transmission.test",
        download_movie_path="/volume1/video/Downloads/Movie",
    )


@responses.activate
def test_transmission_negotiates_session_and_reports_status(tmp_path):
    responses.post(RPC, status=409, headers={"X-Transmission-Session-Id": "session-1"})
    responses.post(RPC, json={"result": "success", "arguments": {"version": "4.0.6"}}, status=200)
    status = TransmissionClient(transmission_settings(tmp_path)).status()
    assert status == {
        "client": "transmission",
        "configured": True,
        "connected": True,
        "version": "4.0.6",
        "error": None,
    }
    assert responses.calls[1].request.headers["X-Transmission-Session-Id"] == "session-1"


@responses.activate
def test_transmission_adds_to_media_directory_and_maps_tasks(tmp_path):
    settings = transmission_settings(tmp_path)
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    responses.post(
        RPC,
        json={
            "result": "success",
            "arguments": {"torrent-added": {"hashString": "a" * 40, "name": "Movie"}},
        },
    )
    client = TransmissionClient(settings)
    PathSettingsRepository(settings).update({"download_movie_path": "/volume1/video/Incoming/Films"})
    added = client.add_download(magnet, settings.qb_movie_category)
    assert added["qb_task_id"] == "a" * 40
    request = responses.calls[0].request.body.decode() if isinstance(responses.calls[0].request.body, bytes) else responses.calls[0].request.body
    assert "/volume1/video/Incoming/Films" in request

    responses.post(
        RPC,
        json={
            "result": "success",
            "arguments": {
                "torrents": [
                    {
                        "hashString": "a" * 40,
                        "name": "Movie",
                        "status": 4,
                        "percentDone": 0.5,
                        "totalSize": 100,
                        "downloadedEver": 50,
                        "rateDownload": 20,
                        "eta": 5,
                        "labels": ["Movie"],
                        "downloadDir": "/volume1/video/Downloads/Movie",
                        "addedDate": 1,
                        "doneDate": 0,
                    }
                ]
            },
        },
    )
    task = client.tasks()[0]
    assert task["state"] == "downloading"
    assert task["category"] == "Movie"


@responses.activate
def test_transmission_maps_files_and_moves_category(tmp_path):
    settings = transmission_settings(tmp_path)
    client = TransmissionClient(settings)
    responses.post(
        RPC,
        json={
            "result": "success",
            "arguments": {
                "torrents": [
                    {
                        "files": [{"name": "Season 01/E01.mkv", "length": 10, "bytesCompleted": 10}],
                        "fileStats": [{"wanted": True}],
                    }
                ]
            },
        },
    )
    assert client.files("hash")[0]["progress"] == 1

    responses.post(RPC, json={"result": "success", "arguments": {}})
    responses.post(RPC, json={"result": "success", "arguments": {}})
    client.set_category("hash", settings.qb_tv_category)
    assert len(responses.calls) == 3


@responses.activate
def test_transmission_maps_torrent_info_and_rename_resume(tmp_path):
    client = TransmissionClient(transmission_settings(tmp_path))
    responses.post(
        RPC,
        json={
            "result": "success",
            "arguments": {
                "torrents": [
                    {
                        "hashString": "hash",
                        "name": "Show",
                        "percentDone": 1,
                        "downloadDir": "/volume1/video/Downloads/TV",
                        "status": 6,
                    }
                ]
            },
        },
    )
    info = client.torrent_info("hash")
    assert info["content_path"] == "/volume1/video/Downloads/TV/Show"

    responses.post(RPC, json={"result": "success", "arguments": {}})
    client.rename_file("hash", "Season 1/01.mkv", "Season 1/Show - S01E01.mkv")
    responses.post(RPC, json={"result": "success", "arguments": {}})
    client.resume("hash")
    assert len(responses.calls) == 3
