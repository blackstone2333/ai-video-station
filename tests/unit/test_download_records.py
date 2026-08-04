from ainas.download_records import DismissedDownloadRepository


def test_dismissed_downloads_are_persistent_and_reversible(tmp_path):
    path = tmp_path / "dismissed-downloads.json"
    repository = DismissedDownloadRepository(path)

    item = repository.dismiss(
        {"hash": "ABC123", "name": "Missing episode", "state": "missingFiles"}
    )

    assert item["hash"] == "abc123"
    assert DismissedDownloadRepository(path).contains("ABC123") is True
    assert repository.visible([{"hash": "abc123"}, {"hash": "other"}]) == [{"hash": "other"}]

    restored = repository.restore("ABC123")
    assert restored["name"] == "Missing episode"
    assert repository.list() == []
