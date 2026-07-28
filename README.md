# AI NAS Core

AI NAS Core 是一个可独立部署、可由 AI 调用的精简媒体 NAS 核心。它把“媒体站点”“下载器”和“媒体库”拆成可替换边界，负责搜索、订阅监听、下载、Emby 规范命名以及同盘硬链接入库。

当前内置 6v 适配器，同时支持后台添加普通 HTML 资源站；下载器支持 qBittorrent 和 Transmission。现有 6v API 调用方式继续兼容。

## 核心能力

- 多站点搜索：内置 6v，也可用 CSS 选择器添加无复杂反爬的 HTML 站点
- 下载器适配：qBittorrent / Transmission 二选一
- 三类媒体：电影、电视剧、动漫
- 自动纠正类型：结合栏目 URL、动漫关键词、`SxxExx`、季/集信息判断，不再把未知栏目直接当电影
- Emby 命名：电影 `片名 (年份)`；电视剧和动漫 `剧名 - SxxExx`
- 独立下载目录：`Downloads/Movie`、`Downloads/TV`、`Downloads/Anime`
- 同盘硬链接：自动进入 movies、tv、anime 媒体库，不复制文件、不覆盖同名文件
- 管理后台：订阅、下载、命名、硬链接入库记录、站点设置、下载与媒体库路径设置
- AI 友好 REST API：API Key、OpenAPI、统一错误结构、请求编号和限流

## NAS 部署

```bash
cp .env.example .env
```

至少修改下载器地址、账号密码、API Key 和 NAS 用户权限：

```env
PORT=16666
PUID=1000
PGID=1000
API_KEY=替换成随机长字符串

DOWNLOADER_TYPE=qbittorrent
QB_HOST=192.168.31.10
QB_PORT=8080
QB_USERNAME=admin
QB_PASSWORD=你的密码

MEDIALIB_BASE_PATH=/volume1/video
DOWNLOADS_BASE_PATH=/volume1/video/Downloads
DOWNLOAD_MOVIE_PATH=/volume1/video/Downloads/Movie
DOWNLOAD_TV_PATH=/volume1/video/Downloads/TV
DOWNLOAD_ANIME_PATH=/volume1/video/Downloads/Anime
MEDIALIB_MOVIE_PATH=/volume1/video/video/movies
MEDIALIB_TV_PATH=/volume1/video/video/tv
MEDIALIB_ANIME_PATH=/volume1/video/video/anime
MEDIALIB_HARDLINK_ENABLED=true
```

使用 Transmission 时改为：

```env
DOWNLOADER_TYPE=transmission
TRANSMISSION_HOST=192.168.31.10
TRANSMISSION_PORT=9091
TRANSMISSION_USERNAME=你的账号
TRANSMISSION_PASSWORD=你的密码
```

启动并检查：

```bash
docker compose up -d --build
curl http://NAS_IP:16666/health
curl http://NAS_IP:16666/ready
```

后台地址：`http://NAS_IP:16666/admin`。

Docker 只把 `${MEDIALIB_BASE_PATH}` 挂载到容器 `/medialib` 一次。下载目录和媒体库必须位于同一文件系统，才能通过 `os.link` 创建不额外占空间的硬链接。

首次启动后可以在后台“路径设置”中修改电影、电视剧、动漫的下载目录和硬链接目录，保存后立即生效，无需重启服务。后台设置保存在 `data/path_settings.json`，优先于 `.env` 中对应的目录值。

`MEDIALIB_BASE_PATH` 是 Docker 的宿主机根挂载，后台只读显示，不能在运行中修改。需要更换根挂载时，请修改 `.env` 后重建或重启容器。后台填写的所有下载目录和媒体库目录都必须是这个根挂载内的绝对路径。

## 目录规则

```text
/volume1/video/Downloads/Movie  → /volume1/video/video/movies/片名 (年份)/
/volume1/video/Downloads/TV     → /volume1/video/video/tv/剧名/Season XX/
/volume1/video/Downloads/Anime  → /volume1/video/video/anime/剧名/Season XX/
```

电影会保留资源内部子目录；电视剧和动漫保留已有 `Season XX`，根目录的 `SxxExx` 文件会自动放入对应 Season。目标同名文件存在时跳过，不执行覆盖。视频、字幕、NFO 和海报等已下载文件会一起入库，torrent padding 文件与未下载文件除外。

## 配置媒体站点

后台“站点设置”可添加普通 HTML 站点，需要提供：

- 站点首页
- 搜索地址模板，例如 `{base_url}/search?q={keyword}`
- 搜索结果、标题、详情链接、下载链接的 CSS 选择器
- 默认媒体类型和动漫/电视剧路径规则

这套模板适用于无登录、无复杂 JavaScript 验证、无强反爬的网站。需要登录、签名、Cloudflare 挑战或特殊编码的网站，应新增一个独立 provider 适配器，不应把站点专用逻辑写进通用抓取器。

站点配置保存在 `data/sites.json`，重建容器不会丢失。

## 类型识别

识别优先级：

1. 站点栏目路径规则，例如 `/dm/`、`/anime/` → 动漫
2. 标题中的动漫/动画/番剧关键词
3. `SxxExx`、`第 xx 集`、`Season xx` 等季集证据
4. AI 或 API 明确传入的 `movie`、`tv`、`anime`

仍无法判断时保留 `auto`，下载接口会要求明确选择类型，不再静默放进电影目录。

## 常用 API

设置 `API_KEY` 后，所有 `/api/*` 请求需携带 `X-Api-Key` 或 `Authorization: Bearer`。

| 接口 | 用途 |
|---|---|
| `POST /api/search` | 跨已启用站点搜索 |
| `POST /api/download` | 下载选中资源 |
| `GET /api/downloader/status` | 当前下载器状态 |
| `GET /api/downloader/tasks` | 下载任务列表 |
| `GET /api/watchlist` | 订阅监听列表 |
| `POST /api/watchlist/check` | 立即检查订阅 |
| `GET /api/naming/jobs` | 命名任务 |
| `POST /api/naming/jobs/check` | 重试命名/硬链接 |
| `GET /api/hardlinks` | 硬链接入库记录 |
| `GET /api/settings/sites` | 站点配置列表 |
| `POST /api/settings/sites` | 添加通用站点 |
| `PATCH /api/settings/sites/{id}` | 更新或停用站点 |
| `DELETE /api/settings/sites/{id}` | 删除站点 |
| `GET /api/settings/paths` | 查看下载与媒体库目录设置 |
| `PATCH /api/settings/paths` | 保存目录设置并立即生效 |

旧的 `/api/qb/status` 和 `/api/qb/tasks` 继续作为兼容别名。完整契约见 `static/openapi.yaml`。

## AI 调用示例

```bash
curl -X POST http://NAS_IP:16666/api/search \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: 你的API_KEY' \
  -d '{"keyword":"Rick and Morty","type":"anime"}'
```

AI 应保留搜索结果的 `id`、`download_link`、`title` 和 `type`，用户选中后原样提交到 `/api/download`，不要自行改写磁力链接。

## 本地测试

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
SCHEDULER_ENABLED=false DATA_DIR=./data python main.py
```

生产镜像使用 Gunicorn 单 worker，避免同一容器内重复运行定时任务。运行数据保存在 `data/watchlist.json`、`data/naming_jobs.json`、`data/sites.json` 和 `data/path_settings.json`。
