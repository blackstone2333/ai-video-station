"""Small Agent-friendly CLI for the AI Video Station REST API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


COMMANDS = {
    "health": ("GET", "/health"),
    "status": ("GET", "/api/downloader/status"),
    "tasks": ("GET", "/api/downloader/tasks"),
    "watchlist": ("GET", "/api/watchlist"),
    "naming": ("GET", "/api/naming/jobs?per_page=100"),
    "hardlinks": ("GET", "/api/hardlinks?status=all&per_page=100"),
    "logs": ("GET", "/api/logs?limit=200"),
}


class CliError(Exception):
    pass


def _request(
    base_url: str,
    token: str,
    method: str,
    path: str,
    data: Any = None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Any:
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
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with opener(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw.decode("utf-8")).get("detail")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            detail = None
        raise CliError(detail or f"API 请求失败：HTTP {exc.code}") from exc
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
    request = subparsers.add_parser("request", help="调用任意 OpenAPI 路径")
    request.add_argument("method", choices=("GET", "POST", "PATCH", "DELETE"))
    request.add_argument("path")
    request.add_argument("--data", help="JSON 请求体")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    opener: Callable[..., Any] = urlopen,
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
        else:
            method, path = args.method, args.path
            try:
                data = json.loads(args.data) if args.data is not None else None
            except json.JSONDecodeError as exc:
                raise CliError("--data 必须是有效 JSON") from exc
        result = _request(args.url, token, method, path, data, opener=opener)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    output(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
