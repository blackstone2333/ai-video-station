"""Small Agent-friendly CLI for the AI Video Station REST API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


COMMANDS = {
    "health": ("GET", "/health"),
    "status": ("GET", "/api/downloader/status"),
    "tasks": ("GET", "/api/downloader/tasks"),
    "watchlist": ("GET", "/api/watchlist"),
    "naming": ("GET", "/api/naming/jobs?per_page=100"),
    "hardlinks": ("GET", "/api/hardlinks?status=all&per_page=100"),
    "cleanup-plans": ("GET", "/api/cleanup/plans?per_page=100"),
    "logs": ("GET", "/api/logs?limit=200"),
}


class CliError(Exception):
    def __init__(
        self,
        detail: str,
        *,
        status: int | None = None,
        title: str | None = None,
        errors: Any = None,
        request_id: str | None = None,
        problem_type: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.title = title
        self.errors = errors
        self.request_id = request_id
        self.problem_type = problem_type

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"detail": self.detail}
        if self.status is not None:
            value["status"] = self.status
        if self.title:
            value["title"] = self.title
        if self.problem_type:
            value["type"] = self.problem_type
        if self.errors is not None:
            value["errors"] = self.errors
        if self.request_id is not None:
            value["request_id"] = self.request_id
        return value


def _origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(value)
    default_port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return parsed.scheme, parsed.hostname, parsed.port or default_port


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow AVS redirects only when credentials remain on the same origin."""

    def __init__(self, initial_url: str) -> None:
        super().__init__()
        self.allowed_origin = _origin(initial_url)

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        target = urljoin(req.full_url, newurl)
        if _origin(target) != self.allowed_origin:
            raise CliError("拒绝把 AVS 凭据随重定向发送到其他地址")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _open_same_origin(request: Request, timeout: int) -> Any:
    return build_opener(_SameOriginRedirectHandler(request.full_url)).open(request, timeout=timeout)


def _same_origin_url(base_url: str, path: str) -> str:
    """Resolve an AVS path without allowing credentials to cross origins."""
    base = urlsplit(base_url.strip())
    supplied = urlsplit(path)
    if base.scheme not in {"http", "https"} or not base.netloc or base.username or base.password:
        raise CliError("AVS_URL 必须是不含账号密码的绝对 HTTP(S) 地址")
    if not path.startswith("/") or supplied.scheme or supplied.netloc:
        raise CliError("API 路径必须是当前 AVS 服务内以 / 开头的相对路径")
    target = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    resolved = urlsplit(target)
    if (resolved.scheme, resolved.hostname, resolved.port) != (base.scheme, base.hostname, base.port):
        raise CliError("拒绝把 AVS 凭据发送到其他地址")
    return target


def _request(
    base_url: str,
    token: str,
    method: str,
    path: str,
    data: Any = None,
    *,
    opener: Callable[..., Any] | None = None,
) -> Any:
    target_url = _same_origin_url(base_url, path)
    if path.startswith("/api/") and not token:
        raise CliError("请通过 AVS_TOKEN 提供后台 API Key 或 Agent 专用令牌")
    headers = {"Accept": "application/json"}
    if token.startswith("avs_agent_"):
        headers["Authorization"] = f"Bearer {token}"
    elif token:
        headers["X-Api-Key"] = token
    payload = None
    if data is not None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        target_url,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with (opener or _open_same_origin)(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            body = None
        if isinstance(body, dict):
            detail = body.get("detail")
            raise CliError(
                str(detail or f"API 请求失败：HTTP {exc.code}"),
                status=body.get("status", exc.code),
                title=body.get("title"),
                errors=body.get("errors"),
                request_id=body.get("request_id"),
                problem_type=body.get("type"),
            ) from exc
        raise CliError(f"API 请求失败：HTTP {exc.code}", status=exc.code) from exc
    except URLError as exc:
        raise CliError(f"无法连接 AI Video Station：{exc.reason}") from exc
    if not raw:
        return {"success": True}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("服务返回了无法解析的响应") from exc


def _parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="avsctl", description="AI Video Station 操作命令行")
    parser.add_argument("--url", default=environment.get("AVS_URL", "http://127.0.0.1:16666"))
    parser.add_argument("--compact", action="store_true", help="输出单行 JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        subparsers.add_parser(command)
    connect = subparsers.add_parser("connect", help="登记当前 Agent 名称和能力")
    connect.add_argument("--name", default="CLI Agent")
    connect.add_argument("--capabilities", default="search,download,watchlist,naming,hardlink,logs")
    search = subparsers.add_parser("search", help="搜索已启用站点（默认不创建订阅）")
    search.add_argument("keyword")
    search.add_argument("--type", choices=("auto", "movie", "tv", "anime", "custom"), default="auto")
    search.add_argument("--add-to-watchlist", action="store_true")
    search.add_argument("--viewing-mode", choices=("daily", "collection", "compact"), default="daily")
    download = subparsers.add_parser("download", help="添加搜索结果到下载器")
    download.add_argument("result_id")
    download.add_argument("download_link")
    download.add_argument("title")
    download.add_argument("--type", choices=("auto", "movie", "tv", "anime", "custom"), default="auto")
    download.add_argument("--path-rule-id")
    manual = subparsers.add_parser("manual-download", help="添加磁力或下载链接")
    manual.add_argument("download_link")
    manual.add_argument("--title")
    manual.add_argument("--type", choices=("auto", "movie", "tv", "anime", "custom"), default="auto")
    manual.add_argument("--original-title")
    manual.add_argument("--edition")
    manual.add_argument("--episode-title")
    manual.add_argument("--path-rule-id")
    manual.add_argument("--preview", action="store_true", help="只识别并预览，不添加下载")
    manual.add_argument("--subscribe", action="store_true", help="电视剧或动漫下载后同时订阅")
    manual.add_argument("--viewing-mode", choices=("daily", "collection", "compact"), default="daily")
    watchlist_add = subparsers.add_parser("watchlist-add", help="添加订阅")
    watchlist_add.add_argument("keyword")
    watchlist_add.add_argument("--type", choices=("auto", "movie", "tv", "anime", "custom"), default="auto")
    watchlist_add.add_argument("--path-rule-id")
    watchlist_add.add_argument("--viewing-mode", choices=("daily", "collection", "compact"), default="daily")
    watchlist_update = subparsers.add_parser("watchlist-update", help="修改订阅观看模式")
    watchlist_update.add_argument("item_id")
    watchlist_update.add_argument("--viewing-mode", choices=("daily", "collection", "compact"), required=True)
    watchlist_check = subparsers.add_parser("watchlist-check", help="检查一个或全部订阅")
    watchlist_check.add_argument("--item-id")
    naming_retry = subparsers.add_parser("naming-retry", help="重试单个命名任务")
    naming_retry.add_argument("job_id")
    naming_check = subparsers.add_parser("naming-check", help="检查一个或全部命名任务")
    naming_check.add_argument("--job-id")
    cleanup_scan = subparsers.add_parser("cleanup-scan", help="只读扫描重复版本并生成清理计划")
    cleanup_scan.add_argument("--policy", choices=("quality_first", "space_first"), default="quality_first")
    cleanup_scan.add_argument("--type", choices=("movie", "tv", "anime", "custom"))
    cleanup_show = subparsers.add_parser("cleanup-show", help="查看一个清理计划")
    cleanup_show.add_argument("plan_id")
    cleanup_execute = subparsers.add_parser("cleanup-execute", help="执行清理计划中的明确选择")
    cleanup_execute.add_argument("plan_id")
    cleanup_execute.add_argument("--selections", required=True, help='JSON 数组，例如 [{"group_id":"...","delete_version_ids":["..."]}]')
    cleanup_execute.add_argument("--delete-source", action="store_true", help="同时删除下载器源数据；默认仅移除媒体库硬链接")
    cleanup_execute.add_argument("--confirm", required=True, choices=("DELETE_SELECTED_DUPLICATES",), help="不可逆操作确认字符串")
    cleanup_retry = subparsers.add_parser("cleanup-retry", help="重试清理计划中的失败项")
    cleanup_retry.add_argument("plan_id")
    request = subparsers.add_parser("request", help="调用任意 OpenAPI 路径")
    request.add_argument("method", choices=("GET", "POST", "PATCH", "DELETE"))
    request.add_argument("path")
    request.add_argument("--data", help="JSON 请求体")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    output: Callable[[str], Any] = print,
) -> int:
    environment = os.environ if environment is None else environment
    args = _parser(environment).parse_args(argv)
    token = environment.get("AVS_TOKEN", "").strip()
    try:
        if args.command in COMMANDS:
            method, path = COMMANDS[args.command]
            data = None
        elif args.command == "connect":
            method, path = "POST", "/api/agents/connect"
            data = {
                "name": args.name,
                "capabilities": [item.strip() for item in args.capabilities.split(",") if item.strip()],
            }
        elif args.command == "search":
            method, path = "POST", "/api/search"
            data = {"keyword": args.keyword, "type": args.type, "add_to_watchlist": args.add_to_watchlist}
            if args.add_to_watchlist:
                data["viewing_mode"] = args.viewing_mode
        elif args.command == "download":
            method, path = "POST", "/api/download"
            data = {
                "result_id": args.result_id,
                "download_link": args.download_link,
                "title": args.title,
                "type": args.type,
            }
            if args.path_rule_id:
                data["path_rule_id"] = args.path_rule_id
        elif args.command == "manual-download":
            method, path = "POST", "/api/download/manual/preview" if args.preview else "/api/download/manual"
            data = {"download_link": args.download_link, "type": args.type}
            for key in ("title", "original_title", "edition", "episode_title", "path_rule_id"):
                value = getattr(args, key)
                if value is not None:
                    data[key] = value
            if args.subscribe:
                data["subscribe"] = True
                data["viewing_mode"] = args.viewing_mode
        elif args.command == "watchlist-add":
            method, path = "POST", "/api/watchlist/add"
            data = {"keyword": args.keyword, "type": args.type, "viewing_mode": args.viewing_mode}
            if args.path_rule_id:
                data["path_rule_id"] = args.path_rule_id
        elif args.command == "watchlist-update":
            method, path = "PATCH", f"/api/watchlist/{args.item_id}"
            data = {"viewing_mode": args.viewing_mode}
        elif args.command == "watchlist-check":
            method, path = "POST", "/api/watchlist/check"
            data = {"item_id": args.item_id} if args.item_id else {}
        elif args.command == "naming-retry":
            method, path = "POST", f"/api/naming/jobs/{args.job_id}/retry"
            data = None
        elif args.command == "naming-check":
            method, path = "POST", "/api/naming/jobs/check"
            data = {"job_id": args.job_id} if args.job_id else {}
        elif args.command == "cleanup-scan":
            method, path = "POST", "/api/cleanup/scan"
            data = {"policy": args.policy}
            if args.type:
                data["media_type"] = args.type
        elif args.command == "cleanup-show":
            method, path = "GET", f"/api/cleanup/plans/{quote(args.plan_id, safe='')}"
            data = None
        elif args.command == "cleanup-execute":
            method, path = "POST", f"/api/cleanup/plans/{quote(args.plan_id, safe='')}/execute"
            try:
                selections = json.loads(args.selections)
            except json.JSONDecodeError as exc:
                raise CliError("--selections 必须是有效 JSON") from exc
            if not isinstance(selections, list) or not selections:
                raise CliError("--selections 必须是非空 JSON 数组")
            data = {
                "selections": selections,
                "delete_source": args.delete_source,
                "confirmation": args.confirm,
            }
        elif args.command == "cleanup-retry":
            method, path = "POST", f"/api/cleanup/plans/{quote(args.plan_id, safe='')}/retry"
            data = {}
        else:
            method, path = args.method, args.path
            try:
                data = json.loads(args.data) if args.data is not None else None
            except json.JSONDecodeError as exc:
                raise CliError("--data 必须是有效 JSON") from exc
        result = _request(args.url, token, method, path, data, opener=opener)
    except CliError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    output(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
