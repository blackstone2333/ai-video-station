# AI Video Station MCP Adapter

This is a thin, local stdio adapter for an existing AI Video Station (AVS) service. It uses the official Python MCP SDK v2 and only forwards a small, allow-listed tool surface to AVS's REST API. It never reads `data/sites.json`, connects to qBittorrent, opens an HTTP port, or reimplements AVS search, download, naming, path, or hardlink logic.

## Install and run

Use a dedicated environment; this project intentionally does not alter AVS's root dependencies.

```sh
cd mcp_adapter
python3 -m venv .venv
.venv/bin/pip install '.[test]'
export AVS_URL='http://127.0.0.1:16666'
export AVS_TOKEN='avs_agent_...'
.venv/bin/avs-mcp
```

`AVS_URL` and `AVS_TOKEN` are required. `AVS_TIMEOUT_SECONDS` is optional (default: 30; allowed range: 0–300). A token beginning with `avs_agent_` is sent as `Authorization: Bearer ...`; any other token is sent as the administrator-only `X-Api-Key` header. Prefer a scoped Agent token.

The process uses the MCP SDK's default stdio transport, so its stdout is reserved for protocol messages. It does not bind a network listener.

## Client configurations

Point the client at the installed executable and provide the credentials as environment variables. Do not paste tokens into source-controlled configuration.

Codex configuration example:

```toml
[mcp_servers.avs]
command = "/absolute/path/to/ai-video-station/mcp_adapter/.venv/bin/avs-mcp"
[mcp_servers.avs.env]
AVS_URL = "http://127.0.0.1:16666"
AVS_TOKEN = "${AVS_TOKEN}"
```

Claude Desktop configuration example:

```json
{
  "mcpServers": {
    "avs": {
      "command": "/absolute/path/to/ai-video-station/mcp_adapter/.venv/bin/avs-mcp",
      "env": {
        "AVS_URL": "http://127.0.0.1:16666",
        "AVS_TOKEN": "replace-with-a-scoped-agent-token"
      }
    }
  }
}
```

For Claude Desktop, use its operating-system secret/environment mechanism rather than committing the shown token. The executable inherits no AVS configuration besides the three documented environment variables.

## Tool scope

| MCP tool | AVS REST endpoint | Scope | MCP annotations |
| --- | --- | --- | --- |
| `avs_search_media` | `POST /api/search` with `add_to_watchlist: false` | search | read-only, idempotent, open-world |
| `avs_add_download` | `POST /api/download` | download | non-destructive, non-idempotent, open-world |
| `avs_preview_manual_download` | `POST /api/download/manual/preview` | download | read-only, idempotent, open-world |
| `avs_add_manual_download` | `POST /api/download/manual` | download | non-destructive, non-idempotent, open-world |
| `avs_list_downloads` | `GET /api/downloader/tasks` | read | read-only, idempotent |
| `avs_list_naming_jobs` | `GET /api/naming/jobs` | read | read-only, idempotent |
| `avs_retry_naming_job` | `POST /api/naming/jobs/{id}/retry` | naming | non-destructive, non-idempotent |
| `avs_list_hardlinks` | `GET /api/hardlinks` | read | read-only, idempotent |
| `avs_list_watchlist` | `GET /api/watchlist` | read | read-only, idempotent |
| `avs_add_watchlist` | `POST /api/watchlist/add` | watchlist | non-destructive, idempotent |
| `avs_update_watchlist` | `PATCH /api/watchlist/{id}` | watchlist | non-destructive, idempotent |
| `avs_check_watchlist` | `POST /api/watchlist/check` | watchlist | non-destructive, non-idempotent, open-world |

`list_naming_jobs` and `list_hardlinks` accept `page` / `per_page` (1–100) and their corresponding status filter. A REST problem response is returned as structured tool data containing `status`, `title`, `detail`, `request_id`, and `errors`, so callers can give an actionable error or quote the request ID to an AVS administrator.

Not exposed: settings, site configuration, path rules, deletion endpoints, torrent-file uploads, qBittorrent relocation/recovery, naming-plan edits, or arbitrary REST paths.

## Verification

```sh
cd mcp_adapter
python -m pytest
python -m pip wheel --no-deps --wheel-dir /tmp/avs-mcp-wheel .
```
