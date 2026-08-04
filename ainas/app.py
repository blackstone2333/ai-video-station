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
from .models import (
    DownloadRequest,
    DownloaderSettingsPatchRequest,
    ManualDownloadRequest,
    ManualTorrentRequest,
    NamingCheckRequest,
    AgentBootstrapRequest,
    AgentConnectRequest,
    PathRulePatchRequest,
    PathSettingsPatchRequest,
    SearchRequest,
    SitePatchRequest,
    SystemSettingsPatchRequest,
    WatchlistAddRequest,
    WatchlistCheckRequest,
)
from .path_rules import PathRuleInput, PathRuleRepository
from .path_settings import PathSettingsRepository
from .runtime_settings import DownloaderSettingsRepository, SystemSettingsRepository
from .services import AppServices, build_services
from .sites import SiteConfig
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
        try:
            checks["providers"] = {"status": "ok", "domain": services.crawler.probe()}
        except AppError as exc:
            checks["providers"] = {"status": "error", "detail": exc.detail}
        qb_status = services.qb.status()
        checks["downloader"] = {
            "status": "ok" if qb_status["connected"] else ("optional" if not qb_status["configured"] else "error"),
            **qb_status,
        }
        ready_state = checks["providers"]["status"] == "ok" and checks["downloader"]["status"] != "error"
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
            watch_item = services.watchlist.add(body.keyword, watch_type)
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
        result = services.download.download(body.result_id, body.download_link, body.title, body.media_type)
        return jsonify({"success": True, "message": "已添加到下载队列", **result})

    @app.post("/api/download/manual")
    def manual_download():
        if request.is_json:
            body = _parse_json(ManualDownloadRequest)
            result = services.download.manual_link(
                body.download_link,
                body.title,
                body.media_type,
                body.original_title,
                body.edition,
                body.episode_title,
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
            result = services.download.manual_torrent(
                content,
                upload.filename,
                form.title,
                form.media_type,
                form.original_title,
                form.edition,
                form.episode_title,
            )
        return jsonify({"success": True, "message": "已识别并添加到下载队列", **result})

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

    @app.route("/api/watchlist/add", methods=["POST", "OPTIONS"])
    def watchlist_add():
        if request.method == "OPTIONS":
            return "", 204
        body = _parse_json(WatchlistAddRequest)
        item = services.watchlist.add(body.keyword, body.media_type)
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

    @app.post("/api/watchlist/check")
    def watchlist_check():
        body = _parse_json(WatchlistCheckRequest)
        report = services.watchlist_service.check(body.item_id)
        return jsonify({"success": True, **report})

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
        if not services.naming_jobs:
            raise ServiceUnavailableError("automatic naming is not initialized")
        item = services.naming_jobs.get(job_id)
        if item.get("status") != "failed" and item.get("hardlink_status") != "failed":
            raise ConflictError("only failed AVS records can be deleted")
        services.naming_jobs.delete(job_id)
        logger.info("naming_job_record_deleted", extra={"job_id": job_id})
        return "", 204

    @app.post("/api/naming/jobs/check")
    def naming_check():
        if not services.naming:
            raise ServiceUnavailableError("automatic naming is not initialized")
        body = _parse_json(NamingCheckRequest)
        return jsonify({"success": True, **services.naming.check(body.job_id)})

    @app.get("/api/hardlinks")
    def hardlinks():
        if not services.naming_jobs:
            raise ServiceUnavailableError("hardlink history is not initialized")
        try:
            page = max(1, int(request.args.get("page", "1")))
            per_page = min(100, max(1, int(request.args.get("per_page", "20"))))
        except ValueError as exc:
            raise ValidationAppError("page and per_page must be integers") from exc
        status = request.args.get("status", "done")
        jobs = [item for item in services.naming_jobs.list() if item.get("hardlink_status")]
        if status != "all":
            jobs = [item for item in jobs if item.get("hardlink_status") == status]
        jobs.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        values = [
            {
                "id": item["id"],
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

    @app.post("/api/settings/sites")
    def sites_add():
        require_admin()
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
        require_admin()
        if not services.sites:
            raise ServiceUnavailableError("site settings are not initialized")
        body = _parse_json(SitePatchRequest)
        item = services.sites.update(site_id, body.model_dump(exclude_none=True))
        return jsonify({"success": True, "item": item})

    @app.delete("/api/settings/sites/<site_id>")
    def sites_delete(site_id: str):
        require_admin()
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
        require_admin()
        if not services.path_settings:
            raise ServiceUnavailableError("path settings are not initialized")
        body = _parse_json(PathSettingsPatchRequest)
        values = services.path_settings.update(body.model_dump(exclude_none=True))
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
        require_admin()
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        body = _parse_json(PathRuleInput)
        item = services.path_rules.add(body.model_dump())
        response = jsonify({"success": True, "item": item})
        response.status_code = 201
        response.headers["Location"] = f"/api/settings/path-rules/{item['id']}"
        return response

    @app.patch("/api/settings/path-rules/<rule_id>")
    def path_rules_update(rule_id: str):
        require_admin()
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        body = _parse_json(PathRulePatchRequest)
        item = services.path_rules.update(rule_id, body.model_dump(exclude_none=True))
        return jsonify({"success": True, "item": item})

    @app.delete("/api/settings/path-rules/<rule_id>")
    def path_rules_delete(rule_id: str):
        require_admin()
        if not services.path_rules:
            raise ServiceUnavailableError("path rules are not initialized")
        services.path_rules.delete(rule_id)
        return "", 204

    @app.get("/api/settings/downloader")
    def downloader_settings_get():
        if not services.downloader_settings:
            raise ServiceUnavailableError("downloader settings are not initialized")
        return jsonify({"success": True, "settings": services.downloader_settings.get()})

    @app.patch("/api/settings/downloader")
    def downloader_settings_update():
        require_admin()
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
        require_admin()
        if not services.system_settings:
            raise ServiceUnavailableError("system settings are not initialized")
        body = _parse_json(SystemSettingsPatchRequest)
        values = services.system_settings.update(body.model_dump())
        scheduler = app.extensions.get("video_station_scheduler")
        if scheduler and scheduler.get_job("watchlist-check"):
            scheduler.reschedule_job("watchlist-check", trigger="interval", hours=values["watchlist_check_hours"])
        return jsonify({"success": True, "settings": values, "message": "订阅检查周期已更新"})

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
        item = services.agents.create(body.name)
        return jsonify({"success": True, "agent": item}), 201

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
    return app
