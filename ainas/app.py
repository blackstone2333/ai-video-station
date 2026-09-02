from __future__ import annotations

import atexit
import logging
from typing import Any, Dict, Optional, Type, TypeVar

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, jsonify, redirect, render_template, request
from pydantic import BaseModel, ValidationError
from werkzeug.exceptions import BadRequest, HTTPException

from . import __version__
from .config import Settings
from .agent_access import AgentAccessRepository
from .errors import AppError, ConflictError, ForbiddenError, NotFoundError, ServiceUnavailableError, ValidationAppError
from .logging_config import configure_logging, read_log_entries
from .middleware import install_middleware
from .permissions import CLEANUP_SCOPE, require_scope
from .models import (
    DownloadRequest,
    DownloaderRelocateRequest,
    DownloaderSettingsPatchRequest,
    ManualDownloadRequest,
    ManualTorrentRequest,
    NamingCheckRequest,
    NamingPlanOverrideRequest,
    AgentBootstrapRequest,
    AgentConnectRequest,
    AgentScopesUpdateRequest,
    PathRuleDeleteRequest,
    PathRulePatchRequest,
    PathSettingsPatchRequest,
    ProviderPreviewRequest,
    SearchRequest,
    SitePatchRequest,
    SystemSettingsPatchRequest,
    DirectorySyncScanRequest,
    CleanupScanRequest,
    CleanupExecuteRequest,
    WatchlistAddRequest,
    WatchlistCheckRequest,
    WatchlistPatchRequest,
)
from .path_rules import PathRuleInput, PathRuleRepository
from .path_settings import PathSettingsRepository
from .runtime_settings import DownloaderSettingsRepository, SystemSettingsRepository
from .services import AppServices, build_services
from .sites import SiteConfig
from .state import StateStoreError
from .watchlist import utc_now_iso


logger = logging.getLogger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)


def _validation_errors(exc: ValidationError) -> list[Dict[str, Any]]:
    return [
        {
            "field": ".".join(str(part) for part in item["loc"]),
            "message": item["msg"],
            "code": item["type"],
        }
        for item in exc.errors()
    ]


def _parse_json(model: Type[ModelT]) -> ModelT:
    if not request.is_json:
        raise ValidationAppError(
            "Content-Type must be application/json",
            [{"field": "body", "message": "JSON body required", "code": "JSON_REQUIRED"}],
        )
    try:
        payload = request.get_json()
    except BadRequest as exc:
        raise ValidationAppError("Malformed JSON body") from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ValidationAppError("Request validation failed", _validation_errors(exc)) from exc


def _parse_value(model: Type[ModelT], payload: Any) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ValidationAppError("Request validation failed", _validation_errors(exc)) from exc


def _problem(exc: AppError):
    body: Dict[str, Any] = {
        "type": f"https://ai-video-station.local/errors/{exc.code}",
        "title": exc.title,
        "status": exc.status_code,
        "detail": exc.detail,
        "instance": request.path,
        "request_id": getattr(g, "request_id", None),
    }
    if exc.errors:
        body["errors"] = exc.errors
    response = jsonify(body)
    response.status_code = exc.status_code
    if hasattr(exc, "retry_after"):
        response.headers["Retry-After"] = str(exc.retry_after)
    response.content_type = "application/problem+json"
    return response


def _start_scheduler(app: Flask, settings: Settings, services: AppServices) -> Optional[BackgroundScheduler]:
    if not settings.scheduler_enabled:
        return None
    scheduler = BackgroundScheduler(timezone=settings.timezone, daemon=True)

    def check_watchlist() -> None:
        with app.app_context():
            report = services.watchlist_service.check()
            logger.info(
                "scheduled_watchlist_check_completed",
                extra={"checked": report["checked"], "downloaded": report["downloaded"]},
            )

    scheduler.add_job(
        check_watchlist,
        "interval",
        hours=settings.watchlist_check_hours,
        id="watchlist-check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    cleanup_last_run = {"at": None}
    def check_cleanup() -> None:
        if not services.cleanup or not settings.cleanup_auto_scan_enabled:
            return
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        previous = cleanup_last_run["at"]
        if previous and (now - previous).total_seconds() < settings.cleanup_scan_hours * 3600:
            return
        cleanup_last_run["at"] = now
        plan = services.cleanup.scan(settings.cleanup_policy)
        if settings.cleanup_auto_execute_enabled:
            selections = services.cleanup.automatic_selections(plan)
            if selections:
                services.cleanup.execute(plan["id"], selections, settings.cleanup_auto_delete_source, "DELETE_SELECTED_DUPLICATES")
    scheduler.add_job(check_cleanup, "interval", hours=1, id="cleanup-check", replace_existing=True, max_instances=1, coalesce=True)
    if services.naming:
        def check_naming_jobs() -> None:
            with app.app_context():
                report = services.naming.check()
                if report["checked"]:
                    logger.info(
                        "scheduled_naming_check_completed",
                        extra={"checked": report["checked"], "completed": report["completed"]},
                    )

        scheduler.add_job(
            check_naming_jobs,
            "interval",
            minutes=settings.naming_check_minutes,
            id="naming-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False) if scheduler.running else None)
    return scheduler


def create_app(
    settings: Optional[Settings] = None,
    services: Optional[AppServices] = None,
    start_scheduler: Optional[bool] = None,
) -> Flask:
    settings = settings or Settings()
    configure_logging(
        settings.log_level,
        settings.logs_path,
        settings.log_file_max_bytes,
        settings.log_file_backup_count,
    )
    app = Flask(__name__, static_folder="../static", static_url_path="/static")
    app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=settings.max_torrent_upload_bytes + 64 * 1024)
    services = services or build_services(settings)
    if services.path_settings is None:
        services.path_settings = PathSettingsRepository(settings)
    if services.downloader_settings is None:
        services.downloader_settings = DownloaderSettingsRepository(settings)
    if services.system_settings is None:
        services.system_settings = SystemSettingsRepository(settings)
    if services.path_rules is None:
        services.path_rules = PathRuleRepository(settings)
    if services.agents is None:
        services.agents = AgentAccessRepository(settings)
    app.extensions["video_station_settings"] = settings
    app.extensions["video_station_services"] = services
    install_middleware(app, settings, services.agents)

    def require_admin() -> None:
        if getattr(g, "auth_kind", None) != "admin":
            raise ForbiddenError("此操作仅允许后台管理员执行")

    agent_endpoint_scopes = {
        "search": "search",
        "download": "download",
        "manual_download": "download",
        "manual_download_preview": "download",
        "downloader_task_dismiss": "download",
        "downloader_task_restore": "download",
        "downloader_task_recover": "download",
        "watchlist_add": "watchlist",
        "watchlist_delete": "watchlist",
        "watchlist_update": "watchlist",
        "watchlist_check": "watchlist",
        "naming_retry": "naming",
        "naming_job_delete": "naming",
        "naming_check": "naming",
        "naming_plan_preview": "naming",
        "naming_plan_update": "naming",
        "directory_sync_scan": "naming",
        "sites_preview": "search",
        "sites_add": "settings",
        "sites_update": "settings",
        "sites_delete": "settings",
        "path_settings_update": "settings",
        "path_rules_add": "settings",
        "path_rules_update": "settings",
        "path_rules_delete": "settings",
        "path_rule_preflight": "settings",
        "downloader_settings_update": "settings",
        "downloader_settings_test": "settings",
        "system_settings_update": "settings",
        "cleanup_scan": CLEANUP_SCOPE,
        "cleanup_execute": CLEANUP_SCOPE,
        "cleanup_retry": CLEANUP_SCOPE,
    }
    agent_scope_exempt = {"agent_connect", "agent_heartbeat"}

    @app.before_request
    def authorize_agent_scope() -> None:
        if (
            not request.path.startswith("/api")
            or request.method == "OPTIONS"
            or getattr(g, "auth_kind", None) != "agent"
            or request.endpoint in agent_scope_exempt
        ):
            return
        required = agent_endpoint_scopes.get(request.endpoint)
        if required is None:
            required = "read" if request.method in {"GET", "HEAD"} else "settings"
        require_scope(required)

    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError):
        logger.warning(
            "operational_error",
            extra={"error_code": exc.code, "request_id": getattr(g, "request_id", None)},
        )
        return _problem(exc)

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        wrapped = AppError(exc.name, exc.description, f"http-{exc.code}", exc.code or 500)
        return _problem(wrapped)

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        logger.exception("unexpected_error", extra={"request_id": getattr(g, "request_id", None)})
        return _problem(AppError("Internal Server Error", "An unexpected error occurred", "internal-error", 500))

    @app.get("/")
    def index():
        return redirect("/admin")

    @app.get("/admin")
    def admin():
        return render_template("admin.html", version=__version__)

    @app.get("/openapi.yaml")
    def openapi():
        return app.send_static_file("openapi.yaml")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "version": __version__})

    @app.get("/ready")
    def ready():
        checks: Dict[str, Any] = {}
        if services.state_store:
            try:
                checks["state"] = services.state_store.integrity_check()
            except StateStoreError:
                logger.exception("state_readiness_failed")
                checks["state"] = {"status": "error", "detail": "persistent state is unavailable"}
        else:
            checks["state"] = {"status": "optional", "detail": "external state store is not configured"}
        try:
            checks["providers"] = {"status": "ok", "domain": services.crawler.probe()}
        except AppError as exc:
            checks["providers"] = {"status": "error", "detail": exc.detail}
        qb_status = services.qb.status()
        checks["downloader"] = {
            "status": "ok" if qb_status["connected"] else ("optional" if not qb_status["configured"] else "error"),
            **qb_status,
        }
        ready_state = (
            checks["state"]["status"] != "error"
            and checks["providers"]["status"] == "ok"
            and checks["downloader"]["status"] != "error"
        )
        return jsonify({"status": "ok" if ready_state else "degraded", "checks": checks}), 200 if ready_state else 503

    @app.route("/api/search", methods=["POST", "OPTIONS"])
    def search():
        if request.method == "OPTIONS":
            return "", 204
        body = _parse_json(SearchRequest)
        outcome = services.search.search(body.keyword, body.media_type)
        watch_type = outcome.media_type if outcome.media_type != "auto" else body.media_type
        watch_item = services.watchlist.find(body.keyword, watch_type)
        watchlist_added = False
        if body.add_to_watchlist and watch_item is None:
            require_scope("watchlist")
            watch_item = services.watchlist_service.add(
                body.keyword,
                watch_type,
                viewing_mode=body.viewing_mode,
            )
            watchlist_added = True
        response = {
            "success": True,
            "keyword": outcome.keyword,
            "type": outcome.media_type,
            "results": [item.to_api() for item in outcome.releases],
            "count": len(outcome.releases),
            "watchlist_exists": watch_item is not None,
            "watchlist_added": watchlist_added,
            "watchlist_id": watch_item["id"] if watch_item else None,
        }
        return jsonify(response)

    @app.route("/api/download", methods=["POST", "OPTIONS"])
    def download():
        if request.method == "OPTIONS":
            return "", 204
        body = _parse_json(DownloadRequest)
        result = services.download.download(
            body.result_id,
            body.download_link,
            body.title,
            body.media_type,
            body.path_rule_id,
        )
        return jsonify({"success": True, "message": "已添加到下载队列", **result})

    @app.post("/api/download/manual")
    def manual_download():
        watchlist_item = None
        if request.is_json:
            body = _parse_json(ManualDownloadRequest)
            result = services.download.manual_link(
                body.download_link,
                body.title,
                body.media_type,
                body.original_title,
                body.edition,
                body.episode_title,
                body.path_rule_id,
            )
            subscribe = body.subscribe
            viewing_mode = body.viewing_mode
        else:
            upload = request.files.get("torrent")
            if not upload or not upload.filename:
                raise ValidationAppError(
                    "请选择 BT 种子文件",
                    [{"field": "torrent", "message": "torrent file is required", "code": "TORRENT_REQUIRED"}],
                )
            if not upload.filename.casefold().endswith(".torrent"):
                raise ValidationAppError(
                    "仅支持 .torrent 文件",
                    [{"field": "torrent", "message": "file extension must be .torrent", "code": "INVALID_TORRENT_FILE"}],
                )
            content = upload.stream.read(settings.max_torrent_upload_bytes + 1)
            if len(content) > settings.max_torrent_upload_bytes:
                raise ValidationAppError("BT 种子文件过大")
            form = _parse_value(ManualTorrentRequest, request.form.to_dict())
            result = services.download.manual_torrent(
                content,
                upload.filename,
                form.title,
                form.media_type,
                form.original_title,
                form.edition,
                form.episode_title,
                form.path_rule_id,
            )
            subscribe = form.subscribe
            viewing_mode = form.viewing_mode
        if subscribe and result.get("type") in {"tv", "anime"}:
            watchlist_item = services.watchlist_service.add(
                str(result.get("title") or result.get("source_name") or "").strip(),
                str(result["type"]),
                result.get("path_rule_id"),
                viewing_mode=viewing_mode,
            )
        return jsonify(
            {
                "success": True,
                "message": "已识别并添加到下载队列",
                **result,
                "watchlist": watchlist_item,
            }
        )

    @app.post("/api/download/manual/preview")
    def manual_download_preview():
        if request.is_json:
            body = _parse_json(ManualDownloadRequest)
            result = services.download.preview_link(
                body.download_link,
                body.title,
                body.media_type,
                body.original_title,
                body.edition,
                body.episode_title,
                body.path_rule_id,
            )
        else:
            upload = request.files.get("torrent")
            if not upload or not upload.filename:
                raise ValidationAppError(
                    "请选择 BT 种子文件",
                    [{"field": "torrent", "message": "torrent file is required", "code": "TORRENT_REQUIRED"}],
                )
            if not upload.filename.casefold().endswith(".torrent"):
                raise ValidationAppError(
                    "仅支持 .torrent 文件",
                    [{"field": "torrent", "message": "file extension must be .torrent", "code": "INVALID_TORRENT_FILE"}],
                )
            content = upload.stream.read(settings.max_torrent_upload_bytes + 1)
            if len(content) > settings.max_torrent_upload_bytes:
                raise ValidationAppError("BT 种子文件过大")
            form = _parse_value(ManualTorrentRequest, request.form.to_dict())
            result = services.download.preview_torrent(
                content,
                upload.filename,
                form.title,
                form.media_type,
                form.original_title,
                form.edition,
                form.episode_title,
                form.path_rule_id,
            )
        message = "识别完成，请确认命名和目录" if result.get("ready") else "请选择媒体类型后继续"
        return jsonify({"success": True, "message": message, **result})

    @app.get("/api/downloader/status")
    @app.get("/api/qb/status")
    def qb_status():
        return jsonify({"success": True, **services.qb.status()})

    @app.get("/api/downloader/tasks")
    @app.get("/api/qb/tasks")
    def qb_tasks():
        all_tasks = services.qb.tasks()
        hidden_tasks = services.dismissed_downloads.list() if services.dismissed_downloads else []
        tasks = services.dismissed_downloads.visible(all_tasks) if services.dismissed_downloads else all_tasks
        return jsonify(
            {
                "success": True,
                "tasks": tasks,
                "count": len(tasks),
                "hidden_count": len(hidden_tasks),
                "hidden_tasks": hidden_tasks,
                "synced_at": utc_now_iso(),
            }
        )

    @app.post("/api/downloader/tasks/<task_hash>/dismiss")
    def downloader_task_dismiss(task_hash: str):
        if not services.dismissed_downloads:
            raise ServiceUnavailableError("dismissed download records are not initialized")
        task = next(
            (item for item in services.qb.tasks() if str(item.get("hash") or "").lower() == task_hash.lower()),
            None,
        )
        if not task:
            raise NotFoundError("downloader task", task_hash)
        item = services.dismissed_downloads.dismiss(task)
        logger.info("downloader_task_dismissed", extra={"task_hash": item["hash"]})
        response = jsonify({"success": True, "item": item})
        response.status_code = 201
        return response

    @app.delete("/api/downloader/tasks/<task_hash>/dismiss")
    def downloader_task_restore(task_hash: str):
        if not services.dismissed_downloads:
            raise ServiceUnavailableError("dismissed download records are not initialized")
        services.dismissed_downloads.restore(task_hash)
        logger.info("downloader_task_restored", extra={"task_hash": task_hash.lower()})
        return "", 204

    @app.post("/api/downloader/tasks/<task_hash>/recover")
    def downloader_task_recover(task_hash: str):
        torrent = services.qb.torrent_info(task_hash)
        if not torrent:
            raise NotFoundError("downloader task", task_hash)
        services.qb.recheck(task_hash)
        services.qb.resume(task_hash)
        logger.info("downloader_task_recovery_started", extra={"task_hash": task_hash.lower()})
        return jsonify({"success": True, "task_hash": task_hash.lower(), "status": "verification_started"}), 202

    @app.post("/api/downloader/tasks/<task_hash>/relocate")
    def downloader_task_relocate(task_hash: str):
        require_admin()
        body = _parse_json(DownloaderRelocateRequest)
        torrent = services.qb.torrent_info(task_hash)
        if not torrent:
            raise NotFoundError("downloader task", task_hash)
        services.qb.set_location(task_hash, body.location)
        logger.info(
            "downloader_task_relocation_started",
            extra={"task_hash": task_hash.lower(), "location": str(body.location)},
        )
        return jsonify(
            {"success": True, "task_hash": task_hash.lower(), "location": str(body.location), "status": "moving"}
        ), 202

    @app.route("/api/watchlist/add", methods=["POST", "OPTIONS"])
    def watchlist_add():
        if request.method == "OPTIONS":
            return "", 204
        body = _parse_json(WatchlistAddRequest)
        item = services.watchlist_service.add(
            body.keyword,
            body.media_type,
            body.path_rule_id,
            body.viewing_mode,
            body.resource_preferences,
        )
        response = jsonify({"success": True, "item": item})
        response.status_code = 201
        response.headers["Location"] = f"/api/watchlist/{item['id']}"
        return response

    @app.get("/api/watchlist")
    def watchlist_list():
        items = services.watchlist.list()
        return jsonify({"success": True, "items": items, "count": len(items)})

    @app.delete("/api/watchlist/<item_id>")
    def watchlist_delete(item_id: str):
        services.watchlist.delete(item_id)
        return "", 204

    @app.patch("/api/watchlist/<item_id>")
    def watchlist_update(item_id: str):
        body = _parse_json(WatchlistPatchRequest)
        item = services.watchlist_service.update(
            item_id,
            body.viewing_mode,
            body.resource_preferences,
        )
        return jsonify({"success": True, "item": item})

    @app.post("/api/watchlist/check")
    def watchlist_check():
        body = _parse_json(WatchlistCheckRequest)
        report = services.watchlist_service.check(body.item_id)
        return jsonify({"success": True, **report})

    @app.post("/api/cleanup/scan")
    def cleanup_scan():
        if not services.cleanup: raise ServiceUnavailableError("cleanup is not initialized")
        body = _parse_json(CleanupScanRequest)
        item = services.cleanup.scan(body.policy, body.media_type)
        return jsonify({"success": True, "item": item}), 201

    @app.get("/api/cleanup/plans")
    def cleanup_plans():
        if not services.cleanup: raise ServiceUnavailableError("cleanup is not initialized")
        try:
            page=max(1,int(request.args.get("page","1"))); per_page=min(100,max(1,int(request.args.get("per_page","20"))))
        except ValueError as exc: raise ValidationAppError("page and per_page must be integers") from exc
        items=services.cleanup.repository.list(); status=request.args.get("status")
        if status: items=[x for x in items if x.get("status")==status]
        items.sort(key=lambda x:x.get("created_at",""),reverse=True); total=len(items)
        return jsonify({"success":True,"items":items[(page-1)*per_page:page*per_page],"total":total,"page":page,"per_page":per_page})

    @app.get("/api/cleanup/plans/<plan_id>")
    def cleanup_plan(plan_id: str):
        if not services.cleanup: raise ServiceUnavailableError("cleanup is not initialized")
        return jsonify({"success":True,"item":services.cleanup.repository.get(plan_id)})

    @app.post("/api/cleanup/plans/<plan_id>/execute")
    def cleanup_execute(plan_id: str):
        if not services.cleanup: raise ServiceUnavailableError("cleanup is not initialized")
        body=_parse_json(CleanupExecuteRequest)
        item, results=services.cleanup.execute(plan_id, [item.model_dump() for item in body.selections], body.delete_source, body.confirmation)
        return jsonify({"success":True,"item":item,"results":results})

    @app.post("/api/cleanup/plans/<plan_id>/retry")
    def cleanup_retry(plan_id: str):
        if not services.cleanup: raise ServiceUnavailableError("cleanup is not initialized")
        item, results=services.cleanup.retry(plan_id)
        return jsonify({"success":True,"item":item,"results":results})

    @app.get("/api/naming/jobs")
    def naming_jobs():
        if not services.naming_jobs:
            raise ServiceUnavailableError("automatic naming is not initialized")
        try:
            page = max(1, int(request.args.get("page", "1")))
            per_page = min(100, max(1, int(request.args.get("per_page", "20"))))
        except ValueError as exc:
            raise ValidationAppError("page and per_page must be integers") from exc
        status = request.args.get("status")
        items = services.naming_jobs.list()
        if status:
            items = [item for item in items if item["status"] == status]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        start = (page - 1) * per_page
        selected = items[start : start + per_page]
        return jsonify(
            {
                "success": True,
                "items": selected,
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": len(items),
                    "total_pages": (len(items) + per_page - 1) // per_page,
                },
            }
        )

    @app.get("/api/naming/jobs/<job_id>")
    def naming_job(job_id: str):
        if not services.naming_jobs:
            raise ServiceUnavailableError("automatic naming is not initialized")
        return jsonify({"success": True, "item": services.naming_jobs.get(job_id)})

    @app.post("/api/naming/jobs/<job_id>/preview")
    def naming_plan_preview(job_id: str):
        if not services.naming:
            raise ServiceUnavailableError("automatic naming is not initialized")
        body = _parse_json(NamingPlanOverrideRequest)
        preview = services.naming.preview_corrected_plan(
            job_id,
            body.model_dump(exclude_unset=True),
        )
        return jsonify({"success": True, **preview})

    @app.patch("/api/naming/jobs/<job_id>/plan")
    def naming_plan_update(job_id: str):
        if not services.naming:
            raise ServiceUnavailableError("automatic naming is not initialized")
        body = _parse_json(NamingPlanOverrideRequest)
        preview = services.naming.preview_corrected_plan(
            job_id,
            body.model_dump(exclude_unset=True),
        )
        item = services.naming.apply_corrected_plan(job_id, preview["plan"])
        logger.info("naming_plan_corrected", extra={"job_id": job_id})
        return jsonify({"success": True, "item": item, "preview": preview.get("preview")})

    @app.post("/api/naming/jobs/<job_id>/retry")
    def naming_retry(job_id: str):
        if not services.naming or not services.naming_jobs:
            raise ServiceUnavailableError("automatic naming is not initialized")
        services.naming_jobs.get(job_id)
        report = services.naming.check(job_id)
        logger.info("naming_job_manual_retry", extra={"job_id": job_id, "running": report["running"]})
        return jsonify({"success": True, **report})

    @app.delete("/api/naming/jobs/<job_id>")
    def naming_job_delete(job_id: str):
        if not services.naming or not services.naming_jobs:
            raise ServiceUnavailableError("automatic naming is not initialized")
        services.naming.discard_record(job_id)
        logger.info("naming_job_record_discarded", extra={"job_id": job_id})
        return "", 204

    @app.post("/api/naming/jobs/check")
    def naming_check():
        if not services.naming:
            raise ServiceUnavailableError("automatic naming is not initialized")
        body = _parse_json(NamingCheckRequest)
        return jsonify({"success": True, **services.naming.check(body.job_id)})

    @app.get("/api/hardlinks")
    def hardlinks():
        if not services.naming_jobs and not services.directory_sync:
            raise ServiceUnavailableError("hardlink history is not initialized")
        try:
            page = max(1, int(request.args.get("page", "1")))
            per_page = min(100, max(1, int(request.args.get("per_page", "20"))))
        except ValueError as exc:
            raise ValidationAppError("page and per_page must be integers") from exc
        status = request.args.get("status", "done")
        jobs = [
            item for item in (services.naming_jobs.list() if services.naming_jobs else [])
            if item.get("hardlink_status")
        ]
        values = [
            {
                "id": item["id"],
                "source_kind": "naming",
                "name": item.get("plan", {}).get("root_name"),
                "type": item.get("plan", {}).get("media_type"),
                "status": item.get("hardlink_status"),
                "target": (item.get("hardlink_result") or {}).get("target"),
                "linked": (item.get("hardlink_result") or {}).get("linked", 0),
                "skipped": (item.get("hardlink_result") or {}).get("skipped", 0),
                "files": (item.get("hardlink_result") or {}).get("files", []),
                "error": item.get("hardlink_error"),
                "attempts": item.get("hardlink_attempts", 0),
                "last_check": item.get("last_check"),
                "torrent_hash": item.get("torrent_hash"),
                "completed_at": item.get("updated_at"),
            }
            for item in jobs
        ]
        if services.directory_sync:
            values.extend(services.directory_sync.repository.list())
        if status != "all":
            values = [item for item in values if item.get("status") == status]
        values.sort(key=lambda item: item.get("completed_at", ""), reverse=True)
        start = (page - 1) * per_page
        return jsonify(
            {
                "success": True,
                "items": values[start : start + per_page],
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": len(values),
                    "total_pages": (len(values) + per_page - 1) // per_page,
                },
            }
        )

    @app.post("/api/directory-sync/scan")
    def directory_sync_scan():
        if not services.directory_sync:
            raise ServiceUnavailableError("directory synchronization is not initialized")
        body = _parse_json(DirectorySyncScanRequest)
        report = services.directory_sync.scan(body.path_rule_id)
        logger.info(
            "directory_sync_requested",
            extra={
                "path_rule_id": body.path_rule_id,
                "linked": report.get("linked", 0),
                "waiting": report.get("waiting", 0),
                "conflicts": report.get("conflicts", 0),
            },
        )
        return jsonify({"success": True, **report})

    @app.get("/api/logs")
    def logs():
        try:
            limit = min(1000, max(1, int(request.args.get("limit", "200"))))
        except ValueError as exc:
            raise ValidationAppError("limit must be an integer") from exc
        level = request.args.get("level") or None
        if level and level.casefold() not in {"debug", "info", "warning", "error", "critical"}:
            raise ValidationAppError("unsupported log level")
        query = (request.args.get("query") or "").strip() or None
        if query and len(query) > 200:
            raise ValidationAppError("log query cannot exceed 200 characters")
        items = read_log_entries(settings.logs_path, limit=limit, level=level, query=query)
        return jsonify({"success": True, "items": items, "count": len(items)})

    @app.get("/api/settings/sites")
    def sites_list():
        if not services.sites:
            raise ServiceUnavailableError("site settings are not initialized")
        items = services.sites.list()
        return jsonify({"success": True, "items": items, "count": len(items)})

    @app.post("/api/settings/sites/preview")
    def sites_preview():
        body = _parse_json(ProviderPreviewRequest)
        preview = getattr(services.crawler, "preview", None)
        if not callable(preview):
            raise ServiceUnavailableError("provider preview is not available")
        return jsonify(
            {
                "success": True,
                **preview(body.keyword, body.media_type, body.site_id),
            }
        )

    @app.post("/api/settings/sites")
    def sites_add():
        require_scope("settings")
        if not services.sites:
            raise ServiceUnavailableError("site settings are not initialized")
        body = _parse_json(SiteConfig)
        item = services.sites.add(body.model_dump())
        response = jsonify({"success": True, "item": item})
        response.status_code = 201
        response.headers["Location"] = f"/api/settings/sites/{item['id']}"
        return response

    @app.patch("/api/settings/sites/<site_id>")
    def sites_update(site_id: str):
        require_scope("settings")
        if not services.sites:
            raise ServiceUnavailableError("site settings are not initialized")
        body = _parse_json(SitePatchRequest)
        item = services.sites.update(site_id, body.model_dump(exclude_none=True))
        return jsonify({"success": True, "item": item})

    @app.delete("/api/settings/sites/<site_id>")
    def sites_delete(site_id: str):
        require_scope("settings")
        if not services.sites:
            raise ServiceUnavailableError("site settings are not initialized")
        services.sites.delete(site_id)
        return "", 204

    @app.get("/api/settings/paths")
    def path_settings_get():
        if not services.path_settings:
            raise ServiceUnavailableError("path settings are not initialized")
        return jsonify({"success": True, "settings": services.path_settings.get()})

    @app.patch("/api/settings/paths")
    def path_settings_update():
        require_scope("settings")
        if not services.path_settings:
            raise ServiceUnavailableError("path settings are not initialized")
        body = _parse_json(PathSettingsPatchRequest)
        values = services.path_settings.update(body.model_dump(exclude_none=True))
        if services.directory_watcher:
            services.directory_watcher.reload()
        return jsonify({"success": True, "settings": values, "message": "目录设置已保存并立即生效"})

    @app.get("/api/settings/path-rules")
    def path_rules_list():
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        media_type = request.args.get("type")
        items = services.path_rules.list(media_type)
        return jsonify({"success": True, "items": items, "count": len(items)})

    @app.post("/api/settings/path-rules")
    def path_rules_add():
        require_scope("settings")
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        body = _parse_json(PathRuleInput)
        item = services.path_rules.add(body.model_dump())
        if services.directory_watcher:
            services.directory_watcher.reload()
        response = jsonify({"success": True, "item": item})
        response.status_code = 201
        response.headers["Location"] = f"/api/settings/path-rules/{item['id']}"
        return response

    @app.patch("/api/settings/path-rules/<rule_id>")
    def path_rules_update(rule_id: str):
        require_scope("settings")
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        body = _parse_json(PathRulePatchRequest)
        item = services.path_rules.update(rule_id, body.model_dump(exclude_none=True))
        if services.directory_watcher:
            services.directory_watcher.reload()
        return jsonify({"success": True, "item": item})

    @app.post("/api/settings/path-rules/<rule_id>/check")
    def path_rule_preflight(rule_id: str):
        require_scope("settings")
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        return jsonify({"success": True, "result": services.path_rules.preflight(rule_id)})

    @app.delete("/api/settings/path-rules/<rule_id>")
    def path_rules_delete(rule_id: str):
        require_scope("settings")
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        if request.is_json:
            body = _parse_json(PathRuleDeleteRequest)
            disable_media_path = body.disable_media_path
        else:
            disable_media_path = request.args.get("disable_media_path", "false").casefold() in {"1", "true", "yes"}
        result = services.path_rules.delete(rule_id, disable_media_path=disable_media_path)
        if services.directory_watcher:
            services.directory_watcher.reload()
        return jsonify({"success": True, **result})

    @app.get("/api/settings/downloader")
    def downloader_settings_get():
        if not services.downloader_settings:
            raise ServiceUnavailableError("downloader settings are not initialized")
        return jsonify({"success": True, "settings": services.downloader_settings.get()})

    @app.patch("/api/settings/downloader")
    def downloader_settings_update():
        require_scope("settings")
        if not services.downloader_settings:
            raise ServiceUnavailableError("downloader settings are not initialized")
        body = _parse_json(DownloaderSettingsPatchRequest)
        values = services.downloader_settings.update(body.model_dump(exclude_none=True))
        return jsonify({"success": True, "settings": values, "message": "下载器配置已保存并立即生效"})

    @app.post("/api/settings/downloader/test")
    def downloader_settings_test():
        status = services.qb.status()
        return jsonify({"success": bool(status.get("connected")), **status}), 200 if status.get("connected") else 503

    @app.get("/api/settings/system")
    def system_settings_get():
        if not services.system_settings:
            raise ServiceUnavailableError("system settings are not initialized")
        return jsonify({"success": True, "settings": services.system_settings.get()})

    @app.patch("/api/settings/system")
    def system_settings_update():
        require_scope("settings")
        if not services.system_settings:
            raise ServiceUnavailableError("system settings are not initialized")
        body = _parse_json(SystemSettingsPatchRequest)
        values = services.system_settings.update(body.model_dump(exclude_none=True))
        scheduler = app.extensions.get("video_station_scheduler")
        if scheduler and scheduler.get_job("watchlist-check"):
            scheduler.reschedule_job("watchlist-check", trigger="interval", hours=values["watchlist_check_hours"])
        if services.directory_watcher:
            services.directory_watcher.reload()
        return jsonify({"success": True, "settings": values, "message": "系统定时设置已更新"})

    @app.get("/api/agents")
    def agents_list():
        if not services.agents:
            raise ServiceUnavailableError("agent access is not initialized")
        items = services.agents.list()
        return jsonify(
            {
                "success": True,
                "items": items,
                "count": len(items),
                "online": sum(item["online"] for item in items),
            }
        )

    @app.post("/api/agents/bootstrap")
    def agent_bootstrap():
        require_admin()
        if not services.agents:
            raise ServiceUnavailableError("agent access is not initialized")
        body = _parse_json(AgentBootstrapRequest)
        item = services.agents.create(body.name, body.scopes)
        return jsonify({"success": True, "agent": item}), 201

    @app.patch("/api/agents/<agent_id>/scopes")
    def agent_scopes_update(agent_id: str):
        require_admin()
        if not services.agents:
            raise ServiceUnavailableError("agent access is not initialized")
        body = _parse_json(AgentScopesUpdateRequest)
        item = services.agents.update_scopes(agent_id, body.scopes)
        return jsonify({"success": True, "agent": item})

    @app.post("/api/agents/connect")
    def agent_connect():
        if getattr(g, "auth_kind", None) != "agent" or not getattr(g, "agent", None):
            raise ForbiddenError("请使用 Agent 专用令牌连接")
        body = _parse_json(AgentConnectRequest)
        item = services.agents.connect(g.agent["id"], body.name, body.capabilities)
        return jsonify({"success": True, "agent": item})

    @app.post("/api/agents/heartbeat")
    def agent_heartbeat():
        if getattr(g, "auth_kind", None) != "agent" or not getattr(g, "agent", None):
            raise ForbiddenError("请使用 Agent 专用令牌发送心跳")
        return jsonify({"success": True, "agent": g.agent})

    @app.delete("/api/agents/<agent_id>")
    def agent_revoke(agent_id: str):
        require_admin()
        if not services.agents:
            raise ServiceUnavailableError("agent access is not initialized")
        services.agents.revoke(agent_id)
        return "", 204

    scheduler_allowed = settings.scheduler_enabled if start_scheduler is None else start_scheduler
    if scheduler_allowed:
        app.extensions["video_station_scheduler"] = _start_scheduler(app, settings, services)
        if services.directory_watcher:
            services.directory_watcher.start()
            atexit.register(services.directory_watcher.stop)
    return app
