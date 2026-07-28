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
from .errors import AppError, ServiceUnavailableError, ValidationAppError
from .logging_config import configure_logging
from .middleware import install_middleware
from .models import DownloadRequest, NamingCheckRequest, SearchRequest, WatchlistAddRequest, WatchlistCheckRequest
from .services import AppServices, build_services


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


def _problem(exc: AppError):
    body: Dict[str, Any] = {
        "type": f"https://sixv.local/errors/{exc.code}",
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
    configure_logging(settings.log_level)
    app = Flask(__name__, static_folder="../static", static_url_path="/static")
    app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=64 * 1024)
    services = services or build_services(settings)
    app.extensions["sixv_settings"] = settings
    app.extensions["sixv_services"] = services
    install_middleware(app, settings)

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
            checks["sixv"] = {"status": "ok", "domain": services.crawler.probe()}
        except AppError as exc:
            checks["sixv"] = {"status": "error", "detail": exc.detail}
        qb_status = services.qb.status()
        checks["qbittorrent"] = {
            "status": "ok" if qb_status["connected"] else ("optional" if not qb_status["configured"] else "error"),
            **qb_status,
        }
        ready_state = checks["sixv"]["status"] == "ok" and checks["qbittorrent"]["status"] != "error"
        return jsonify({"status": "ok" if ready_state else "degraded", "checks": checks}), 200 if ready_state else 503

    @app.route("/api/search", methods=["POST", "OPTIONS"])
    def search():
        if request.method == "OPTIONS":
            return "", 204
        body = _parse_json(SearchRequest)
        outcome = services.search.search(body.keyword, body.media_type)
        watch_item = None
        if not outcome.releases and settings.auto_watch_on_empty:
            watch_type = outcome.media_type if outcome.media_type != "auto" else body.media_type
            watch_item = services.watchlist.add(body.keyword, watch_type)
        response = {
            "success": True,
            "keyword": outcome.keyword,
            "type": outcome.media_type,
            "results": [item.to_api() for item in outcome.releases],
            "count": len(outcome.releases),
            "watchlisted": watch_item is not None,
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

    @app.get("/api/qb/status")
    def qb_status():
        return jsonify({"success": True, **services.qb.status()})

    @app.get("/api/qb/tasks")
    def qb_tasks():
        tasks = services.qb.tasks()
        return jsonify({"success": True, "tasks": tasks, "count": len(tasks)})

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

    @app.post("/api/naming/jobs/check")
    def naming_check():
        if not services.naming:
            raise ServiceUnavailableError("automatic naming is not initialized")
        body = _parse_json(NamingCheckRequest)
        return jsonify({"success": True, **services.naming.check(body.job_id)})

    scheduler_allowed = settings.scheduler_enabled if start_scheduler is None else start_scheduler
    if scheduler_allowed:
        app.extensions["sixv_scheduler"] = _start_scheduler(app, settings, services)
    return app
