from __future__ import annotations

import json
from pathlib import Path

from ainas.config import Settings
from ainas.medialib import MediaLibraryService
from ainas.naming import NamingJobRepository, NamingService, safe_name
from ainas.quality import build_release
from ainas.sites import ProviderRegistry, SiteRepository
from ainas.state import StateStore


HASH = "a" * 40


def _movie(link: str = "magnet:?xt=urn:btih:" + HASH + "&dn=Movie.2024.mkv"):
    return build_release("测试电影", "Movie.2024.mkv", "https://provider.test/item", link, "movie", 2024)


class BindingQB:
    """Downloader double for unknown HTTP-torrent task ids."""

    def __init__(self, matches: list[dict | None]) -> None:
        self.matches = list(matches)
        self.reconcile_calls: list[dict] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []

    def add_download(self, *args, **kwargs):
        return {"qb_task_id": None, "category": args[1]}

    def reconcile_submission(self, **kwargs):
        self.reconcile_calls.append(kwargs)
        return self.matches.pop(0) if self.matches else None

    def torrent_info(self, hash_value):
        return {"hash": hash_value, "progress": 1.0}

    def files(self, hash_value):
        return [{"name": "Movie.2024.mkv", "size": 1, "progress": 1.0, "priority": 1}]

    def rename_file(self, hash_value, old_path, new_path):
        self.rename_calls.append((old_path, new_path))

    def rename_folder(self, *args):
        pass

    def rename_torrent(self, *args):
        pass

    def set_category(self, hash_value, category):
        self.released.append(("category", category))

    def resume(self, hash_value):
        self.released.append(("resume", hash_value))


def _naming_service(tmp_path: Path, qb: BindingQB, **overrides) -> tuple[NamingService, NamingJobRepository]:
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False, naming_max_attempts=2, **overrides)
    repository = NamingJobRepository(settings.naming_jobs_path)
    return NamingService(settings, repository, qb), repository


def test_http_torrent_submission_stays_pending_until_a_unique_binding_is_found(tmp_path):
    qb = BindingQB([None, {"hash": "b" * 40}])
    service, repository = _naming_service(tmp_path, qb)

    result = service.add_download(_movie("https://downloads.test/movie.torrent"), "sixv-movie")
    assert result["naming_status"] == "awaiting_binding"
    job = repository.get(result["naming_job_id"])
    assert job["torrent_hash"].startswith("pending:")

    report = service.check(job["id"])
    updated = repository.get(job["id"])
    assert report["completed"] == 1
    assert updated["torrent_hash"] == "b" * 40
    assert updated["status"] == "completed"
    assert len(qb.reconcile_calls) == 2
    assert qb.reconcile_calls[-1]["category"] == service.settings.qb_naming_category
    assert qb.reconcile_calls[-1]["root_name"] == "测试电影 (2024)"


def test_unbound_http_torrent_becomes_missing_in_downloader_after_bounded_checks(tmp_path):
    qb = BindingQB([None, None, None])
    service, repository = _naming_service(tmp_path, qb)
    result = service.add_download(_movie("https://downloads.test/movie.torrent"), "sixv-movie")

    service.check(result["naming_job_id"])
    service.check(result["naming_job_id"])

    job = repository.get(result["naming_job_id"])
    assert job["status"] == "missing_in_downloader"
    assert job["last_error"] == "accepted submission could not yet be bound to a downloader task"


class RetryRenameQB(BindingQB):
    def __init__(self) -> None:
        super().__init__([])
        self.file_values = [
            {"name": "first.mkv", "size": 1, "progress": 1.0, "priority": 1},
            {"name": "second.mkv", "size": 1, "progress": 1.0, "priority": 1},
        ]
        self.failed = False

    def add_download(self, *args, **kwargs):
        return {"qb_task_id": HASH, "category": args[1]}

    def torrent_info(self, hash_value):
        return {"hash": hash_value, "progress": 1.0}

    def files(self, hash_value):
        return self.file_values

    def rename_file(self, hash_value, old_path, new_path):
        self.rename_calls.append((old_path, new_path))
        if old_path == "second.mkv" and not self.failed:
            self.failed = True
            from ainas.errors import UpstreamError
            raise UpstreamError("qBittorrent", "temporary rename failure")


def test_partial_rename_retries_from_persisted_checkpoint_without_repeating_prior_operations(tmp_path):
    qb = RetryRenameQB()
    service, repository = _naming_service(tmp_path, qb)
    result = service.add_download(_movie(), "sixv-movie")

    service.check(result["naming_job_id"])
    failed = repository.get(result["naming_job_id"])
    assert failed["status"] == "retrying"
    assert failed["rename_checkpoint"]["files"] == 1

    service.check(result["naming_job_id"])
    finished = repository.get(result["naming_job_id"])
    assert finished["status"] == "completed"
    assert [old for old, _ in qb.rename_calls].count("first.mkv") == 1
    assert [old for old, _ in qb.rename_calls].count("second.mkv") == 2


def test_hardlink_conflict_never_replaces_an_existing_different_file(tmp_path):
    host = tmp_path / "nas"
    mount = tmp_path / "medialib"
    mount.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data", scheduler_enabled=False, medialib_hardlink_enabled=True,
        medialib_base_path=host, medialib_mount_path=mount,
        downloads_base_path=host / "Downloads", download_movie_path=host / "Downloads" / "Movie",
        download_tv_path=host / "Downloads" / "TV", download_anime_path=host / "Downloads" / "Anime",
        download_custom_path=host / "Downloads" / "Custom", medialib_movie_path=host / "video" / "movies",
        medialib_tv_path=host / "video" / "tv", medialib_anime_path=host / "video" / "anime",
        medialib_custom_path=host / "video" / "custom",
    )
    source = mount / "Downloads" / "Movie" / "Film.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    target = mount / "video" / "movies" / "Film (2024)" / source.name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"operator-file")

    result = MediaLibraryService(settings).link_completed(
        {"save_path": str(host / "Downloads" / "Movie")},
        [{"name": source.name, "size": 6, "priority": 1}],
        {"media_type": "movie", "media_name": "Film", "root_name": "Film (2024)"},
    )

    assert result["status"] == "conflict"
    assert result["conflicts"] == 1
    assert result["files"] == [{"status": "conflict", "source": str(source), "target": str(target)}]
    assert target.read_bytes() == b"operator-file"


def test_safe_name_limits_utf8_bytes_without_cutting_a_multibyte_character_or_extension():
    name = safe_name("影" * 100 + ".mkv", max_length=31)
    assert name.endswith(".mkv")
    assert len(name.encode("utf-8")) <= 31
    assert name.removesuffix(".mkv") == "影" * 9


def test_sqlite_imports_utf8_json_records_and_documents_without_rewriting_legacy_files(tmp_path):
    record_legacy = tmp_path / "agents.json"
    document_legacy = tmp_path / "settings.json"
    records = {"items": [{"id": "代理-1", "name": "家庭 Agent", "scopes": ["read"]}]}
    document = {"name": "命名设置", "enabled": True}
    record_legacy.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    document_legacy.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    original_records = record_legacy.read_bytes()
    original_document = document_legacy.read_bytes()

    store = StateStore(tmp_path / "state.db")
    store.ensure_records("agents", legacy_path=record_legacy)
    store.ensure_document("settings", legacy_path=document_legacy)

    assert store.list_records("agents") == records["items"]
    assert store.read_document("settings") == document
    assert record_legacy.read_bytes() == original_records
    assert document_legacy.read_bytes() == original_document


def test_provider_preview_reports_per_provider_success_and_sanitized_failure(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False)
    sites = SiteRepository(tmp_path / "providers.json", {"id": "site", "name": "One", "adapter": "sixv", "enabled": True, "base_urls": ["https://provider.test"]})
    registry = ProviderRegistry(settings, sites)

    class Healthy:
        def probe(self): return "https://provider.test"
        def search(self, keyword, media_type): return [_movie()]

    class Broken:
        def probe(self): raise RuntimeError("internal DNS detail")
        def search(self, keyword, media_type): return []

    monkeypatch.setattr(registry, "_providers", lambda: [
        ({"id": "healthy", "name": "Healthy"}, Healthy()),
        ({"id": "broken", "name": "Broken"}, Broken()),
    ])
    report = registry.preview("电影", "movie")

    assert report["ok"] is False
    assert report["providers"][0]["sample_count"] == 1
    assert report["providers"][1]["error"] == {"code": "provider-error", "detail": "provider check failed"}
