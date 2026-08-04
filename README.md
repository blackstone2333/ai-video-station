# AI Video Station

AI Video Station 是一个可独立部署、也可由 AI Agent 调用的轻量媒体下载与整理服务。它负责站点搜索、订阅监听、下载、Emby 规范命名和同盘硬链接入库，不承担播放器或完整媒体中心的职责。

当前内置 6v 适配器，也能在后台添加没有复杂反爬的普通 HTML 资源站。下载器支持 qBittorrent 与 Transmission，后台端口默认是 `16666`。

## v1.5.5 更新说明

- 下载记录可从 AVS 页面隐藏并随时恢复显示；此操作不删除、不暂停下载器任务，也不删除任何文件。
- `missingFiles` 和下载器错误任务不再占用总览的“当前下载”位置。
- 文件缺失任务提供显式“校验并恢复”操作：要求下载器重新校验并恢复缺失的数据块，不删除或重新添加 torrent。

## v1.5.4 更新说明

- 规范命名和硬链接失败后，可在对应记录上直接查看完整任务状态、错误、尝试次数和原始任务数据，并按任务单独重试。
- 失败记录可从后台删除；此操作只清理 AVS 记录，不会删除下载器任务、下载文件或媒体库文件。
- 下载记录在页面可见时每 15 秒只读同步一次，也可手动刷新；这不是 Agent 心跳，不会修改 qBittorrent/Transmission。
- 下载进度会结合 `progress`、已下载字节、完成时间和完成状态归一化，避免下载器已完成但 AVS 因单一陈旧字段显示 `0%`。

## v1.5.3 更新说明

- 下载接收与后处理解耦：qB 接收任务后，AVS 立即登记订阅链接和集数；规范命名改由后台任务执行，后续暂时性错误不会让订阅记录回退为“未下载”。
- 规范命名只在 torrent 及选中文件全部下载完成后执行，并在标记完成前验证规范路径确实存在，避免下载过程中改名造成 qB 元数据与实体文件分离。
- 电影、电视剧、动漫和自定义资源都会在所选下载目录下创建独立的规范资源根目录，电视剧/动漫继续保留 `Season XX` 层级。
- AVS 复用已存在的 qB 分类，不再自动改写分类保存路径；只有分类不存在时才创建，并始终用本次任务的显式下载目录关闭自动路径管理。
- qB 返回模糊结果时，AVS 会按 torrent hash 只读确认任务是否已经登记，避免“实际已添加、订阅却记录失败”。
- 硬链接源文件定位支持规范路径、原始路径、文件夹部分改名和“qB 保存目录本身已是 Season 目录”等情况；目标文件仍使用 Emby 规范名称。
- 已确认源文件缺失的历史任务不会凭空恢复；应先确认下载器重新具备完整源文件，再在 AVS 中处理命名和硬链接。

## v1.5.2 更新说明

- 修复硬链接路径过期：命名完成后，媒体库模块不再使用下载器返回的旧文件名，而是读取 `naming_job.result.operations[].new_path` 作为实际源路径。
- 同时应用目录改名结果：电视剧和动漫会把 `folder_operations` 中的目录变化一并合成，例如 `第1季/旧文件名.mp4` 最终解析为 `Season 01/规范文件名.mp4`。
- 源文件仍然存在的历史任务无需重新下载；升级后在“规范命名”页面点击“立即处理”，或调用 `POST /api/naming/jobs/check` 并传入 `job_id`，即可按新路径规则重新执行硬链接。
- 标题栏左上角增加主题快捷切换，仍支持深色、浅色、跟随系统和按时间切换。
- 增加“系统日志”页面和 `GET /api/logs`。日志以 JSON 格式保存在 `data/ai-video-station.log`，自动轮转，并在写入与返回时隐藏 Agent Token、Authorization 和 API Key。
- 取消管理页每 20 秒自动刷新；需要最新状态时点击标题栏刷新按钮，日志页也提供独立刷新按钮。
- Agent 改为按需连接：不需要发送定时心跳，任何有效 API 请求都会更新“最近使用”时间。旧 `/api/agents/heartbeat` 只为兼容旧 Agent 保留，已在 OpenAPI 中标记为废弃。
- 增加 `python -m ainas.cli` 命令行入口，Agent 可通过同一套 REST/OpenAPI 权限查看下载、订阅、命名、硬链接和日志，也能调用任意开放接口。

升级后重新构建容器即可生效：

```bash
docker compose up -d --build --remove-orphans
```

## 核心能力

- 多站点搜索：内置 6v，可通过 CSS 选择器添加普通 HTML 站点
- 手动下载：导入磁力链接、HTTP `.torrent` 链接或上传 BT 种子
- 自动分类：电影、电视剧、动漫、自定义四类；结合栏目、季集标记和关键词判断
- 规范命名：从资源页/链接名称还原片名，避免 `DHF`、`大H蜂`、`01.mkv` 被 Emby 误识别
- 多目录映射：每类可配置多个下载源目录，每个源目录独立对应一个硬链接目标目录
- 下载器切换：在后台配置并切换 qBittorrent / Transmission
- 同盘硬链接：基于 `os.link` 入库，不复制媒体内容，不覆盖目标同名文件
- 管理后台：标题栏主题切换、订阅周期、下载记录、命名任务、硬链接记录、系统日志、站点、目录和下载器设置
- Agent 连接：顶部一键生成独立、可撤销、只显示一次的 Agent 令牌
- Agent 操作面：REST/OpenAPI 与 `python -m ainas.cli` 命令行，按需连接，无需心跳

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

## 让 AI 读取 README 一键安装

项目公开仓库：`https://github.com/blackstone2333/ai-video-station`

把下面这段话发给能够操作 NAS 终端的 AI Agent 即可。如果项目已经下载到 NAS，把“克隆仓库”改成“使用当前目录”：

```text
请在这台 NAS 上安装并配置 AI Video Station：
https://github.com/blackstone2333/ai-video-station

请先完整阅读仓库 README 中的“AI Agent 安装协议”，严格按顺序执行。缺少的配置一次性向我询问；不要猜密码、删除媒体、覆盖已有 .env、停止其他服务，或在回复和日志中显示 API_KEY、Agent Token、下载器密码。安装后完成健康检查、下载器检查、目录挂载检查，并按 README 的验收格式汇报。
```

### AI Agent 安装协议

以下内容是给安装 Agent 的执行契约。除非用户另有明确要求，必须按顺序执行。

#### 1. 只读预检

先检查并记录，不做修改：

- NAS 操作系统、CPU 架构、当前用户、Docker 与 Docker Compose 是否可用。
- `16666` 端口是否被占用；如果占用，只报告进程和可选端口，不得停止现有服务。
- 项目目录是否已有 `.env`、`data/` 或正在运行的 `ai-video-station` 容器。
- NAS 媒体根目录、下载目录和媒体库目标目录是否存在，以及容器运行用户是否有读写权限。
- qBittorrent 或 Transmission 的地址是否能从 NAS 访问。

如果 Docker 或 Docker Compose 不存在，停止安装并告诉用户需要先安装什么，不要自动修改 NAS 系统软件源。

#### 2. 一次性收集必要配置

只询问缺失项，尽量一次问完：

```text
NAS 地址或主机名：
管理后台端口（默认 16666）：
MEDIALIB_BASE_PATH（例如 /volume1/video）：
容器运行 PUID / PGID：
下载器类型（qbittorrent / transmission）：
下载器地址、端口、用户名：
下载器密码（使用安全输入，不在聊天总结中重复）：

电影：下载目录 → 媒体库目录
电视剧：下载目录 → 媒体库目录
动漫：下载目录 → 媒体库目录
自定义：下载目录 → 媒体库目录
```

用户不需要四类全部启用，但每个启用目录都必须位于 `MEDIALIB_BASE_PATH` 内。没有特殊要求时，可以建议使用本 README 的默认目录，但必须让用户确认后再写入配置。

#### 3. 获取项目并保护现有配置

- 没有项目目录时，执行下面的命令克隆 `main` 分支：

  ```bash
  git clone --branch main --single-branch https://github.com/blackstone2333/ai-video-station.git
  cd ai-video-station
  ```

- 已有项目目录时，先查看当前分支、远端和未提交修改；不得覆盖用户修改。
- `.env` 不存在时才从 `.env.example` 创建；已存在时先备份到仓库外的私密目录，再只更新用户确认的字段。
- 自动生成至少 32 字节的随机 `API_KEY`，直接写入 `.env`，不得打印、复制到聊天或写入普通日志。
- 下载器密码只写入 `.env`；最终汇报不得重复密码。
- 不得提交或上传 `.env`、`data/*.json`、运行日志或任何真实令牌。

#### 4. 写入并校验 `.env`

至少确认这些字段已设置：

```env
PORT=16666
PUID=1000
PGID=1000
API_KEY=<随机长密钥>
DATA_DIR=/data

DOWNLOADER_TYPE=qbittorrent
QB_HOST=<下载器地址>
QB_PORT=8080
QB_USERNAME=<用户名>
QB_PASSWORD=<密码>

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

使用 Transmission 时改写为对应的 `TRANSMISSION_*` 字段。保留 `.env.example` 中其他默认项，不要为了“精简”删除未知配置。

#### 5. 校验硬链接前提

- 规范化每个源目录与目标目录，确认它们都在 `MEDIALIB_BASE_PATH` 下，拒绝 `..`、相对路径和符号链接逃逸。
- 只创建用户确认的缺失目录，不移动、不重命名、不删除已有媒体。
- 检查源目录和目标目录的文件系统设备号；不同文件系统时必须关闭自动硬链接并向用户报告，不能退化为复制文件。
- 确认 Compose 只把 `${MEDIALIB_BASE_PATH}` 挂载一次到 `/medialib`，避免同一宿主机文件通过两个容器挂载点导致 `os.link` 判断跨设备。

#### 6. 构建与启动

先验证 Compose 配置，再构建启动：

```bash
docker compose config
docker compose up -d --build --remove-orphans
docker compose ps
```

不得执行 `docker system prune`、删除其他容器、删除卷或清理用户镜像。升级已有实例时保留 `.env` 与 `data/`。

#### 7. 必须完成的验收

逐项验证：

1. `GET http://127.0.0.1:16666/health` 返回 `status: ok`。
2. `/ready` 返回 `ok`；如果是 `degraded`，读取具体依赖错误并修复后重试。
3. 管理后台 `/admin` 可以打开，标题栏左上角能切换主题。
4. 使用 API Key 调用 `/api/downloader/status`，确认所选下载器 `connected: true`。
5. `/api/settings/path-rules` 中的每条下载目录与硬链接目标都和用户确认值一致。
6. `/api/logs?level=error` 没有新的启动错误，且响应中不包含真实密码或令牌。
7. 不创建测试下载、不移动现有媒体；除非用户明确授权，否则只验证目录和文件系统条件。

#### 8. 安装完成后的 Agent 接入

打开后台右上角“连接 Agent”，生成独立的 `avs_agent_*` 令牌，把自动复制的连接方案发给需要接入的 Agent。Agent Token 与管理员 API Key 分离，可随时撤销。

Agent 不需要定时发送心跳。首次调用 `/api/agents/connect` 登记名称和能力后，只在需要查看或操作时调用 REST/OpenAPI 或 CLI；任何有效请求都会更新后台显示的“最近使用”时间。

#### 9. Agent 最终汇报格式

安装 Agent 最终只汇报下面这些内容，不显示任何密钥或密码：

```text
AI Video Station：安装成功 / 未完成
版本：
后台地址：
健康状态：
下载器：类型、地址、连接状态
NAS 根挂载：宿主机路径 → /medialib
已启用目录映射：
硬链接前提：同一文件系统 / 未满足
日志检查：正常 / 错误摘要
仍需用户处理：
```

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
| `POST /api/downloader/tasks/{hash}/dismiss` | 仅从 AVS 隐藏下载记录 |
| `DELETE /api/downloader/tasks/{hash}/dismiss` | 恢复显示隐藏记录 |
| `POST /api/downloader/tasks/{hash}/recover` | 重新校验并恢复指定下载任务 |
| `GET /api/watchlist` | 订阅监听列表 |
| `POST /api/watchlist/check` | 立即检查订阅 |
| `GET /api/naming/jobs` | 命名任务列表 |
| `POST /api/naming/jobs/check` | 重试命名或硬链接 |
| `POST /api/naming/jobs/{job_id}/retry` | 单独重试一个命名或硬链接任务 |
| `DELETE /api/naming/jobs/{job_id}` | 只删除失败的 AVS 记录，不删除下载或文件 |
| `GET /api/hardlinks` | 硬链接入库记录 |
| `GET /api/logs` | 脱敏后的结构化运行日志 |
| `GET /api/settings/path-rules` | 多目录映射 |
| `GET /api/settings/downloader` | 下载器运行时设置 |
| `GET /api/settings/system` | 订阅周期设置 |
| `GET /api/agents` | Agent 连接状态 |

`POST /api/search` 默认是纯读取操作，不会自动创建订阅。响应中的 `watchlist_exists` 和 `watchlist_id` 只提示是否已经存在匹配订阅。新增订阅应调用 `POST /api/watchlist/add`；确需在一次搜索中显式订阅时，也可传入 `"add_to_watchlist": true`。Agent 多轮搜索时应保持该值为 `false` 或直接省略。

旧的 `/api/qb/status` 和 `/api/qb/tasks` 保留为兼容别名。完整契约见 `static/openapi.yaml`。

## Agent CLI

项目内置零额外依赖的命令行客户端。令牌只从环境变量读取，避免出现在命令参数和进程列表中：

```bash
export AVS_URL=http://NAS_IP:16666
export AVS_TOKEN='后台生成的 Agent 令牌或 API Key'

python -m ainas.cli status
python -m ainas.cli tasks
python -m ainas.cli watchlist
python -m ainas.cli naming
python -m ainas.cli hardlinks
python -m ainas.cli logs
```

Agent 也可以通过通用命令调用 OpenAPI 中的任意路径：

```bash
python -m ainas.cli request POST /api/naming/jobs/check --data '{"job_id":"任务 ID"}'
```

生产容器内同样可执行 `python -m ainas.cli`。CLI 与网页后台共用 REST 权限规则：Agent 可以搜索、下载、查看任务和日志，修改系统设置仍要求管理员 API Key。

## 升级现有安装

升级前先备份 `.env` 和 `data/`。然后执行：

```bash
docker compose up -d --build --remove-orphans
```

旧版 `data/path_settings.json` 会继续生效；首次运行新版时会根据它生成四条默认目录映射。运行时数据还包括 `path_rules.json`、`downloader_settings.json`、`system_settings.json`、`agents.json` 和自动轮转的 `ai-video-station.log`。

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
