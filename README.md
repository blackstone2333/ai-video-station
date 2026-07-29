# AI Video Station

AI Video Station 是一个可独立部署、也可由 AI Agent 调用的轻量媒体下载与整理服务。它负责站点搜索、订阅监听、下载、Emby 规范命名和同盘硬链接入库，不承担播放器或完整媒体中心的职责。

当前内置 6v 适配器，也能在后台添加没有复杂反爬的普通 HTML 资源站。下载器支持 qBittorrent 与 Transmission，后台端口默认是 `16666`。

## 核心能力

- 多站点搜索：内置 6v，可通过 CSS 选择器添加普通 HTML 站点
- 手动下载：导入磁力链接、HTTP `.torrent` 链接或上传 BT 种子
- 自动分类：电影、电视剧、动漫、自定义四类；结合栏目、季集标记和关键词判断
- 规范命名：从资源页/链接名称还原片名，避免 `DHF`、`大H蜂`、`01.mkv` 被 Emby 误识别
- 多目录映射：每类可配置多个下载源目录，每个源目录独立对应一个硬链接目标目录
- 下载器切换：在后台配置并切换 qBittorrent / Transmission
- 同盘硬链接：基于 `os.link` 入库，不复制媒体内容，不覆盖目标同名文件
- 管理后台：主题、订阅周期、下载记录、命名任务、硬链接记录、站点、目录和下载器设置
- Agent 连接：顶部一键生成独立、可撤销、只显示一次的 Agent 令牌
- OpenAPI：统一错误结构、请求编号、限流，适合 Codex 等 Agent 调用

## 命名规范

电影目录与文件：

```text
{title} ({year})/
{title}.{original_title}-{part} ({year}) - {edition} - {videoFormat}.ext
```

电视剧与动漫：

```text
{title} ({year})/
Season {season}/
{title}.{original_title}-{part} - {season_episode} - {episode_title} - {videoFormat}.ext
```

`original_title`、`part`、`year`、`edition`、`episode_title` 和 `videoFormat` 缺失时，会连同对应的点、横线或括号整段省略，不会留下空占位符。电视剧/动漫只有文件名明确含有 `CD`、`Disc`、`Part` 时才增加分段，不会把 01、02 两集误写成 Part1、Part2。每个视频优先采用自身文件名中的 2160p/1080p 等画质信息。

自定义内容不重命名内部文件；入库根目录使用资源链接名称或 BT 种子名称，而不是下载器返回的混乱名称。

## Docker / 群晖部署

复制配置：

```bash
cp .env.example .env
```

至少修改 API Key、NAS 用户权限、下载器连接和 NAS 根目录：

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
DOWNLOAD_CUSTOM_PATH=/volume1/video/Downloads/Custom
MEDIALIB_MOVIE_PATH=/volume1/video/video/movies
MEDIALIB_TV_PATH=/volume1/video/video/tv
MEDIALIB_ANIME_PATH=/volume1/video/video/anime
MEDIALIB_CUSTOM_PATH=/volume1/video/video/custom
MEDIALIB_HARDLINK_ENABLED=true
```

Transmission 配置示例：

```env
DOWNLOADER_TYPE=transmission
TRANSMISSION_HOST=192.168.31.10
TRANSMISSION_PORT=9091
TRANSMISSION_USERNAME=你的账号
TRANSMISSION_PASSWORD=你的密码
```

启动并检查：

```bash
docker compose up -d --build --remove-orphans
curl http://NAS_IP:16666/health
curl http://NAS_IP:16666/ready
```

管理后台：`http://NAS_IP:16666/admin`。

Docker 只把 `${MEDIALIB_BASE_PATH}` 挂载到容器 `/medialib` 一次。下载目录与媒体库目标目录必须位于该根目录内、处在同一个文件系统并允许容器用户读写，才能通过硬链接实现零额外媒体空间占用。

首次启动会生成电影、电视剧、动漫、自定义四条默认映射。之后可在“路径设置”中给每类增加任意数量的映射，并为每条映射设置：

- 下载源目录
- 硬链接目标目录
- 是否启用
- 是否规范命名
- 是否作为该类型的默认下载目录

运行时配置保存在 `data/`，重建容器不会丢失。`MEDIALIB_BASE_PATH` 是 Docker 根挂载，不能在后台运行时修改；更换它需要修改 `.env` 后重建容器。

## 让 AI 一键安装

把项目文件夹或压缩包交给能连接 NAS 终端的 AI Agent，然后发送下面这段提示词：

```text
请帮我在这台 NAS 上安装当前目录中的 AI Video Station。

要求：
1. 先检查 Docker、Docker Compose、当前项目目录和 NAS 媒体根目录，不删除或覆盖现有媒体文件。
2. 从 .env.example 创建 .env；自动生成随机长 API_KEY，但不要在公开日志中输出。
3. 向我确认 NAS 媒体根目录、容器运行 PUID/PGID、下载器类型与地址、账号；密码使用安全输入，不写进聊天总结。
4. 确保下载源与硬链接目标都位于同一个 MEDIALIB_BASE_PATH 挂载和同一文件系统。
5. 使用 16666 端口执行 docker compose up -d --build，随后检查 /health、/ready、容器日志和后台页面。
6. 如果 16666 被占用，先告诉我冲突进程，不要擅自停止其他服务。
7. 安装成功后只汇报访问地址、健康状态、实际挂载和我还需要在后台完成的设置，不显示 API_KEY 或下载器密码。
```

安装后打开后台右上角“连接 Agent”，点击“生成并复制连接方案”，把复制内容发给你的 Agent。它获得的是单独的 `avs_agent_*` 令牌，不是后台 API Key；令牌可在同一弹窗中随时撤销。

## 目录与入库规则

默认示例：

```text
/volume1/video/Downloads/Movie  → /volume1/video/video/movies/片名 (年份)/
/volume1/video/Downloads/TV     → /volume1/video/video/tv/剧名 (年份)/Season XX/
/volume1/video/Downloads/Anime  → /volume1/video/video/anime/剧名 (年份)/Season XX/
/volume1/video/Downloads/Custom → /volume1/video/video/custom/资源链接名称/原内部结构/
```

电影保留资源内的附加子目录；电视剧和动漫保留 `Season XX`，根目录中的 `SxxExx` 文件会自动进入对应 Season。视频、字幕、NFO、海报和自定义资料会一起入库；torrent padding 文件和未下载文件会被忽略。目标已存在同名文件时安全跳过。

硬链接记录中的目标路径可点击复制，然后粘贴到群晖 File Station 或其他 NAS 文件管理器打开。浏览器安全策略通常不允许 HTTP 页面直接打开服务器本地目录。

## 站点与类型识别

后台“站点设置”可添加普通 HTML 站点，需要填写站点首页、搜索地址模板、结果/标题/详情/下载链接的 CSS 选择器，以及默认媒体类型和栏目关键词。

通用模板适用于无登录、无复杂 JavaScript 验证、无强反爬的网站。需要登录、签名、Cloudflare 挑战或特殊编码的网站，应实现独立 provider 适配器。

识别优先考虑站点栏目路径和动漫关键词，其次是 `SxxExx`、`第 xx 集`、`Season xx` 等季集证据，最后使用 API 明确传入的类型。手动下载无法可靠判断时会进入“自定义”，避免误入电影库。

## 常用 API

设置 `API_KEY` 后，后台使用 `X-Api-Key`；Agent 使用 `Authorization: Bearer avs_agent_*`。

| 接口 | 用途 |
|---|---|
| `POST /api/search` | 跨已启用站点搜索 |
| `POST /api/download` | 下载搜索结果 |
| `POST /api/download/manual` | 导入磁力/种子链接或上传 BT 文件 |
| `GET /api/downloader/status` | 当前下载器状态 |
| `GET /api/downloader/tasks` | 下载任务列表 |
| `GET /api/watchlist` | 订阅监听列表 |
| `POST /api/watchlist/check` | 立即检查订阅 |
| `GET /api/naming/jobs` | 命名任务列表 |
| `POST /api/naming/jobs/check` | 重试命名或硬链接 |
| `GET /api/hardlinks` | 硬链接入库记录 |
| `GET /api/settings/path-rules` | 多目录映射 |
| `GET /api/settings/downloader` | 下载器运行时设置 |
| `GET /api/settings/system` | 订阅周期设置 |
| `GET /api/agents` | Agent 连接状态 |

旧的 `/api/qb/status` 和 `/api/qb/tasks` 保留为兼容别名。完整契约见 `static/openapi.yaml`。

## 升级现有安装

升级前先备份 `.env` 和 `data/`。然后执行：

```bash
docker compose up -d --build --remove-orphans
```

旧版 `data/path_settings.json` 会继续生效；首次运行新版时会根据它生成四条默认目录映射。新增加的运行时文件包括 `path_rules.json`、`downloader_settings.json`、`system_settings.json` 和 `agents.json`。

## 是否加入刮削

结论是：刮削有价值，但不应该直接塞进当前核心链路。它能补全 TMDB/Bangumi 标题、年份、集标题、海报和 NFO，但也会引入 API Key、匹配歧义、区域语言差异、限流和重命名误判。更合适的方向是后续做成可插拔的“元数据增强器”，识别置信度不足时允许人工确认，失败也不阻塞下载与硬链接。

完整权衡见 [docs/scraping-analysis.md](docs/scraping-analysis.md)。

## 本地测试

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
SCHEDULER_ENABLED=false DATA_DIR=./data python main.py
```

生产镜像使用 Gunicorn 单 worker，避免同一容器内重复执行定时任务。
