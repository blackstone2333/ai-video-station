# AI Video Station

AI Video Station 是一个可独立部署、也可由 AI Agent 调用的轻量媒体下载与整理服务。它负责站点搜索、订阅监听、下载、Emby 规范命名和同盘硬链接入库，不承担播放器或完整媒体中心的职责。

当前内置 6v 适配器，也能在后台添加没有复杂反爬的普通 HTML 资源站。下载器支持 qBittorrent 与 Transmission，后台端口默认是 `16666`。

## v2.2.0 更新说明

- 新增“重复与洗版”中心：扫描已启用的下载目录和媒体库目录，先按设备号与 inode 合并正常硬链接，再识别同一电影或同一集的不同物理版本，避免把 AVS 自己创建的硬链接误报为重复占用。
- 支持质量优先、空间优先和逐项手动选择。质量优先默认保留综合质量更好的版本；空间优先可以保留满足观看要求的较小版本并清理 4K 等大体积版本，执行前会显示预计真实释放空间。
- 清理采用“生成计划 → 明确确认 → 执行”的两段式流程；执行前重新核对路径、inode、文件大小、下载器状态和保留版本，陈旧计划、符号链接、路径逃逸及多文件 torrent 的部分源删除会在任何文件变化前被拒绝。
- 整个 torrent 都可淘汰时，AVS 只通过下载器正常 API 暂停并删除任务与源数据；下载器任务已不存在或文件不受下载器管理时，AVS 可以在已配置下载根目录内直接删除。媒体库硬链接先移除，源数据后处理，不修改 qBittorrent 源码、系统或全局配置。
- 后台、REST/OpenAPI、CLI 和 MCP 均可扫描、查看、执行与重试洗版计划。Agent 需要单独授予 `cleanup` 权限；自动扫描、自动执行和自动删源分级控制，升级后全部默认关闭。

## v2.1.2 更新说明

- 多文件 torrent 改为逐文件后处理：单集下载完成后立即执行规范命名和硬链接，不再等待整包全部完成。
- 增量任务按 qB 文件索引保存检查点，定时检查和失败重试不会重复改名或重复创建硬链接。
- Season 文件夹、torrent 显示名和最终分类仍在全部选中文件完成后统一收尾，避免移动仍在写入的目录。
- 已开始增量处理的任务会锁定命名方案，防止中途修改标题导致同一资源分散到不同媒体目录。

## v2.1.1 更新说明

- 修复国产电视剧全集包中 `01.mkv`、`01.2160p.HD.mkv` 等纯数字前缀文件无法识别集号的问题，同时避免把 `720p`、`1080p` 等分辨率误当成集号。
- 全集标题会去掉 `[全集]` 等发布标记并按 `片名 (年份)/Season 01/片名 - S01E01 - 画质.ext` 规范整理；无法可靠识别集号的电视剧或动漫会停止入库并显示可诊断错误，避免污染 Emby 媒体库。
- 未开始改名的旧任务和硬链接重试都会重新执行安全检查；可识别的全集包缺集选择支持纯数字前缀文件名。

## v2.1.0 更新说明

- 订阅增加“日常观看、收藏、省空间”三种观看模式。默认日常观看按 `1080p > 4K > 720p` 排序，同分辨率优先 `Remux/BluRay > WEB-DL > HDTV`，音轨优先“多语言 > 原始语言 > 中文 > 英语 > 其他”；偏好只负责排序，有可用资源时仍会自动兜底。
- 枪版、TS、TC、CAM 等低质量来源继续硬性排除，不会因为分辨率标成 1080p 而进入下载。
- 电视剧和动漫遇到全集包时，会先暂停新建任务并读取文件清单；能够逐集识别时只下载尚缺的集数及对应字幕，不能安全拆分时保持暂停并显示“全集待确认”，不会覆盖已有集数。
- 手动磁力和 BT 种子改为“先识别预览、再确认下载”，明确显示媒体类型、下载目录和规范命名方案；电影、电视剧、动漫确认后完整进入命名与硬链接流程，电视剧/动漫可同时订阅后续更新。
- 后台可直接新增订阅、为搜索或订阅选择观看模式，并随时修改已有订阅；CLI 与 MCP 同步支持手动预览、手动下载和订阅偏好更新。

v2.1 不执行洗版清理，也不会删除旧下载、下载器任务或媒体库文件。无法安全拆分的全集包需要后续人工确认。

## v2.0.2 更新说明

- 6V 搜索与订阅改为名称匹配优先：同名内容即使被发布到错误栏目，也不会再被站点分类提前过滤。
- 媒体判型依次参考内容特征、用户在 AVS 选择的类型和站点栏目；6V 栏目只作辅助，不再覆盖电影、电视剧、动漫的入库分类。
- 动漫订阅可以命中误放在电视剧栏目的资源并继续进入动漫下载、命名和媒体库路径；明显的剧集编号仍能纠正误选的电影类型。

## v2.0.1 更新说明

- 手动磁力优先读取链接中的 `dn` 资源名；无资源名时明确要求填写标题，不再以“手动下载”占位词创建错误任务。
- 未开始改名的 AVS 任务和等待硬链接的孤儿记录可以安全放弃；该操作只删除 AVS 后处理记录，不会删除下载器任务或任何文件。
- 下载器中已不存在的任务会在有限次数检查后进入可诊断、可重试或可放弃的失败状态，不再永久占据处理中列表。

## v2.0.0 更新说明

- 搜索与订阅彻底解耦：搜索默认只返回结果，订阅使用独立接口，避免 Agent 多轮搜索产生重复监听。
- 命名和硬链接增加可恢复检查点、下载器任务延迟绑定、缺失终态、部分入库与冲突状态；失败、缺失、部分和冲突记录都可重试或只删除 AVS 记录。
- 目录映射成为下载路由：电影、电视剧、动漫、自定义每类可配置多条规则，手动下载、搜索结果和订阅都能明确选择规则并先做只读预检。
- Agent 改为后台授予最小权限，API 默认拒绝未鉴权访问；管理页可随时调整 `read/search/download/watchlist/naming/cleanup/settings` 范围。
- 运行状态迁移到 `data/state.db`。首次启动会一次性导入旧 JSON，但不会删除或改写旧文件；SQLite 此后成为权威状态源。
- 增加并发 Provider、受控适配器注册、私网/重定向/响应体保护、Provider 预览，以及显式下载器能力契约。
- 管理页补齐资源搜索、分页、命名修正预览、安全迁移、路径诊断、Agent 权限、完整错误与请求 ID 展示。
- 新增强类型 CLI 和独立的 REST-only MCP stdio 适配器；同时加入离线备份恢复、CI、多架构镜像发布与固定版本回滚流程。

这是一次涉及鉴权和状态存储的主版本升级。升级前请先停止服务并按“升级现有安装”执行离线备份；必须配置有效的随机 `API_KEY`，除非在隔离局域网中明确设置 `ALLOW_INSECURE_LAN=true`。

## v1.5.6 更新说明

- 每条目录映射新增“下载器容器目录”，明确区分 NAS 下载源路径、qB/Transmission 容器路径和媒体库目标路径。
- 新任务向下载器提交容器可见路径，避免把宿主机路径写进下载器容器私有层。
- 新增单任务安全迁移接口，由下载器把现有数据移动到正确挂载，不删除或重新添加 torrent。

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
- 规范命名和硬链接按文件进度增量执行：每个选中文件下载完成后立即处理，未完成文件继续等待；整包完成后再统一整理 Season 文件夹、恢复最终分类并校验全部规范路径。
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
- 手动下载：先预览识别磁力链接、HTTP `.torrent` 链接或上传的 BT 种子，再确认进入自动命名和硬链接流程
- 资源偏好：按日常观看、收藏、省空间三种模式排序版本；枪版始终禁止
- 全集缺集：能按文件识别集数时只下载缺失集，不能安全拆分时保持暂停待确认
- 自动分类：电影、电视剧、动漫、自定义四类；结合栏目、季集标记和关键词判断
- 规范命名：从资源页/链接名称还原片名，避免 `DHF`、`大H蜂`、`01.mkv` 被 Emby 误识别
- 多目录映射：每类可配置多个下载源目录，每个源目录独立对应一个硬链接目标目录
- 下载器切换：在后台配置并切换 qBittorrent / Transmission
- 同盘硬链接：基于 `os.link` 入库，不复制媒体内容，不覆盖目标同名文件
- 管理后台：标题栏主题切换、订阅周期、下载记录、命名任务、硬链接记录、重复与洗版、系统日志、站点、目录和下载器设置
- Agent 连接：顶部一键生成独立、可撤销、只显示一次的 Agent 令牌
- Agent 操作面：REST/OpenAPI 与 `python -m ainas.cli` 命令行，按需连接，无需心跳

手动磁力会优先读取链接中的 `dn` 资源名；如果链接本身不含名称，后台会要求先填写标题。自动识别不明确时必须选择电影、电视剧、动漫或自定义，不会静默归入错误媒体库。放弃未完成的 AVS 命名/硬链接记录只会停止 AVS 后处理，不会删除下载器任务或任何文件。

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
# 默认仅供本机反向代理访问；直连可信局域网时改为 NAS 的具体内网 IP
BIND_ADDRESS=127.0.0.1
PUID=1000
PGID=1000
API_KEY=替换成随机长字符串
ALLOW_INSECURE_LAN=false

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
curl http://127.0.0.1:16666/health
curl http://127.0.0.1:16666/ready
```

管理后台：`http://NAS_IP:16666/admin`。

默认 Compose 仅绑定 `127.0.0.1`，适合由 NAS 上的反向代理接入。若需要直接在可信局域网访问，请在 `.env` 中将 `BIND_ADDRESS` 设为 NAS 的**具体内网 IP**（不要使用 `0.0.0.0`），并在 NAS 防火墙中仅允许可信网段。公网访问必须放在 TLS 终止的反向代理之后，并配置访问控制；本服务自身不应直接暴露到互联网。

`/health` 是容器存活探针；监控和告警应使用 `/ready`，因为它会检查 SQLite 状态完整性，并反映站点与下载器依赖是否处于可用状态。

Docker 只把 `${MEDIALIB_BASE_PATH}` 挂载到容器 `/medialib` 一次。下载目录与媒体库目标目录必须位于该根目录内、处在同一个文件系统并允许容器用户读写，才能通过硬链接实现零额外媒体空间占用。

首次启动会生成电影、电视剧、动漫、自定义四条默认映射。之后可在“路径设置”中给每类增加任意数量的映射，并为每条映射设置：

- 下载源目录
- 硬链接目标目录
- 是否启用
- 是否规范命名
- 是否作为该类型的默认下载目录

运行时配置保存在 `data/state.db`，日志和兼容迁移文件也位于 `data/`，重建容器不会丢失。首次升级会一次性导入旧 JSON，导入后不会再用旧 JSON 覆盖 SQLite。`MEDIALIB_BASE_PATH` 是 Docker 根挂载，不能在后台运行时修改；更换它需要修改 `.env` 后重建容器。

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
BIND_ADDRESS=127.0.0.1
PUID=1000
PGID=1000
API_KEY=<随机长密钥>
ALLOW_INSECURE_LAN=false
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

识别以名称和内容特征为主：动漫关键词、`SxxExx`、`第 xx 集`、`Season xx` 等证据优先，用户明确选择的类型和站点栏目用于补充判断。手动下载无法可靠判断时会要求明确选择类型，不会静默进入“自定义”。

## 常用 API

设置 `API_KEY` 后，后台使用 `X-Api-Key`；Agent 使用 `Authorization: Bearer avs_agent_*`。

| 接口 | 用途 |
|---|---|
| `POST /api/search` | 跨已启用站点搜索 |
| `POST /api/download` | 下载搜索结果 |
| `POST /api/download/manual/preview` | 无副作用预览手动磁力/种子的识别与命名方案 |
| `POST /api/download/manual` | 导入磁力/种子链接或上传 BT 文件 |
| `GET /api/downloader/status` | 当前下载器状态 |
| `GET /api/downloader/tasks` | 下载任务列表 |
| `POST /api/downloader/tasks/{hash}/dismiss` | 仅从 AVS 隐藏下载记录 |
| `DELETE /api/downloader/tasks/{hash}/dismiss` | 恢复显示隐藏记录 |
| `POST /api/downloader/tasks/{hash}/recover` | 重新校验并恢复指定下载任务 |
| `POST /api/downloader/tasks/{hash}/relocate` | 将指定任务迁移到下载器容器目录 |
| `GET /api/watchlist` | 订阅监听列表 |
| `POST /api/watchlist/add` | 新增订阅并选择观看模式 |
| `PATCH /api/watchlist/{item_id}` | 修改订阅观看模式或资源偏好 |
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
python -m ainas.cli search 'Rick and Morty' --type anime
python -m ainas.cli manual-download 'magnet:?...' --type tv --preview
python -m ainas.cli manual-download 'magnet:?...' --type tv --subscribe --viewing-mode daily
python -m ainas.cli watchlist-add 'Rick and Morty' --type anime --viewing-mode daily
python -m ainas.cli watchlist-update <订阅ID> --viewing-mode collection
python -m ainas.cli naming-retry <任务ID>
```

Agent 也可以通过通用命令调用 OpenAPI 中的任意路径：

```bash
python -m ainas.cli request POST /api/naming/jobs/check --data '{"job_id":"任务 ID"}'
```

生产容器内同样可执行 `python -m ainas.cli`。CLI 与网页后台共用 REST 权限规则：Agent 可以搜索、下载、查看任务和日志，修改系统设置仍要求管理员 API Key。

## Agent MCP 适配器

[`mcp_adapter/`](mcp_adapter/) 是一个独立安装的 stdio MCP 服务，只调用 AVS REST，不直接读取状态库、媒体文件或下载器，也不新增网络端口。它提供搜索、手动预览与下载、查看下载/命名/硬链接、重试命名、订阅和重复清理等 17 个白名单工具。

```bash
cd mcp_adapter
python3.12 -m venv .venv
.venv/bin/pip install .

export AVS_URL=http://NAS_IP:16666
export AVS_TOKEN='后台生成的最小权限 Agent Token'
.venv/bin/avs-mcp
```

Codex、Claude Desktop 等客户端的 stdio 配置示例、工具与权限对照见 [`mcp_adapter/README.md`](mcp_adapter/README.md)。推荐为 MCP 单独创建 Agent Token，只授予实际使用的 `read/search/download/watchlist/naming/cleanup` 范围，不授予 `settings`。`cleanup` 含显式删除能力，不需要洗版的 Agent 不应获得该权限。

## 升级现有安装

升级前必须先做离线备份：停止服务后再复制数据，避免运行中写入造成不一致。运行时 `data/` 与备份都可能含有令牌和下载器凭据；仓库会忽略它们，切勿提交或上传到公开位置。

```bash
docker compose stop
python -m ainas.maintenance backup --data-dir ./data --backup-dir ./backups --include-env --env-file ./.env
python -m ainas.maintenance verify ./backups/avs-backup-v1-YYYYMMDDTHHMMSSZ
docker compose up -d
```

备份是带版本清单的离线目录：每个文件都有 SHA-256 校验，目录/文件权限分别收紧为仅备份所有者可访问。命令不会打印 `.env` 的内容。恢复同样必须先停止服务，且默认拒绝覆盖已有数据：

```bash
docker compose stop
python -m ainas.maintenance verify ./backups/avs-backup-v1-YYYYMMDDTHHMMSSZ
python -m ainas.maintenance restore ./backups/avs-backup-v1-YYYYMMDDTHHMMSSZ --data-dir ./data
# 仅在已人工确认目标与备份后，才显式允许覆盖：
# python -m ainas.maintenance restore ./backups/avs-backup-v1-YYYYMMDDTHHMMSSZ --data-dir ./data --overwrite
docker compose up -d
```

`.env` 不会在恢复时自动写回；如确有需要，额外指定 `--restore-env --env-file ./.env`。恢复会先验证清单，拒绝路径穿越和符号链接；数据先写入同一文件系统的暂存目录并执行 SQLite `integrity_check`，验证通过后再交换整个数据目录。提交失败会自动换回原目录，备份中没有的旧文件以及陈旧的 `state.db-wal` / `state.db-shm` 不会残留到恢复结果中。备份后再执行升级：

```bash
docker compose up -d --build --remove-orphans
```

## CI、发布与回滚

每个 PR 和 `main` 推送都会运行 Python 测试与覆盖率门槛、MCP 官方 SDK 测试和 wheel 构建、Compose 静态配置检查，并分别验证 `linux/amd64` 与 `linux/arm64` 镜像可构建。推送形如 `v2.2.0` 的 Git 标签会发布多架构镜像到 GHCR；工作流拒绝覆盖已发布的版本标签，并同时生成对应提交 SHA 标签，不发布 `latest`。

部署已发布镜像时，请固定一个版本，不要使用浮动标签：

```bash
IMAGE=ghcr.io/blackstone2333/ai-video-station:v2.2.0 docker compose pull
IMAGE=ghcr.io/blackstone2333/ai-video-station:v2.2.0 docker compose up -d --no-build --remove-orphans
```

回滚就是在完成并校验备份后，将上述版本替换为上一个已验证的 `v*` 标签并重新执行两条命令。若升级涉及运行时数据变化，先停止服务并按上一节恢复对应备份，再启动旧版本。

旧版 `watchlist.json`、`naming_jobs.json`、`path_settings.json`、`path_rules.json`、`downloader_settings.json`、`system_settings.json`、`agents.json` 和站点配置会在 `state.db` 对应命名空间尚未初始化时导入一次。旧文件会原样保留以便回滚，但此后以 SQLite 为准。运行时目录还包含自动轮转的 `ai-video-station.log`。

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
