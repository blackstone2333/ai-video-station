# 6v 电影电视剧自动下载服务

这是一个可直接部署到 NAS 的 Python 服务：接收片名，搜索 6v 当前可用镜像，过滤 TS/TC/CAM/抢先版，按画质排序，并把所选资源加入 qBittorrent。电影无结果时自动监听；电视剧可首次下载已有集数，之后只追新集；下载完成后按 Emby 规范命名并直接硬链接到媒体库，不再依赖 nastool。

## 已实现

- 优先使用 `6vw.cc`，并自动切换 `6vdyy.com`、`6v520.cc`、`xb6v.com` 等镜像；全部失效时从 `6v123.net` 发现新域名
- 兼容 GBK/GB18030 页面、相对链接、HTML 实体及简单 JavaScript 转义/`atob` 链接
- 自动过滤 TS、TC、CAM、HDTS、HDTC、HQCAM、枪版和抢先版
- 按分辨率、体积、语言、HDR、片源排序
- qBittorrent 登录、分类创建、添加下载、连接状态和任务查询
- JSON 监听列表原子持久化；电影定时复查，电视剧按 `SxxExx`/`第xx集` 去重追更
- 读取磁力链接名称和 qBittorrent 真实文件清单，自动修正 `DHF`、`大H蜂`、`01.mp4` 等规避或混乱命名
- Emby 规范：电影 `片名 (年份).ext`；电视剧 `剧名 - S01E01.ext`；外置字幕同步对齐文件名
- 下载达到 100% 后使用 `os.link` 创建同盘硬链接；电影创建独立目录，电视剧保留或补全 `Season XX` 结构
- 内置管理后台，可查看监听、下载进度、下载记录、命名状态和失败原因
- 可选 API Key、限流、请求编号、安全响应头、明确的错误格式
- Docker 单容器部署；单 worker 避免定时任务重复运行

## NAS 上部署

1. 把整个 `sixv-crawler` 目录放到 NAS，例如 `/volume1/docker/sixv-crawler`。
2. 在目录中复制配置模板：

   ```bash
   cp .env.example .env
   ```

3. 编辑 `.env`，至少修改：

   ```env
   QB_HOST=192.168.31.10
   QB_PORT=8080
   QB_USERNAME=admin
   QB_PASSWORD=你的密码
   API_KEY=一个足够长的随机字符串
   PUID=你的NAS用户ID
   PGID=你的NAS用户组ID
   MEDIALIB_BASE_PATH=/volume1/video
   MEDIALIB_MOVIE_PATH=/volume1/video/video/movies
   MEDIALIB_TV_PATH=/volume1/video/video/tv
   MEDIALIB_HARDLINK_ENABLED=true
   ```

   群晖可用 `id 用户名` 查看 PUID/PGID。若 qBittorrent 使用 HTTPS，自签证书环境可设 `QB_USE_HTTPS=true`、`QB_VERIFY_SSL=false`。

4. 启动：

   ```bash
   docker compose up -d --build
   ```

5. 检查：

   ```bash
   curl http://NAS_IP:16666/health
   curl http://NAS_IP:16666/ready
   ```

打开 `http://NAS_IP:16666/admin` 即可进入管理后台。首次访问输入 `.env` 中的 `API_KEY`；密钥只保存在当前浏览器标签页。

`data/watchlist.json` 和 `data/naming_jobs.json` 映射到持久卷，重建或重启容器不会丢失监听或命名记录。生产镜像使用 Gunicorn；不要把 Flask 自带开发服务器用于长期运行。

## API 使用

如果设置了 `API_KEY`，所有 `/api/*` 请求需带下面任一请求头：

```text
X-Api-Key: 你的API_KEY
Authorization: Bearer 你的API_KEY
```

搜索电影：

```bash
curl -X POST http://NAS_IP:16666/api/search \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: 你的API_KEY' \
  -d '{"keyword":"奥本海默","type":"movie"}'
```

选择返回结果后下载：

```bash
curl -X POST http://NAS_IP:16666/api/download \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: 你的API_KEY' \
  -d '{
    "result_id":"搜索结果中的id",
    "download_link":"搜索结果中的download_link",
    "title":"搜索结果中的title",
    "type":"movie"
  }'
```

无结果时，`/api/search` 默认会自动加入监听，并返回 `watchlisted: true` 与 `watchlist_id`。

常用接口：

| 接口 | 用途 |
|---|---|
| `POST /api/search` | 搜索电影/电视剧 |
| `POST /api/download` | 下载一个所选版本 |
| `GET /api/qb/status` | qBittorrent 连接状态 |
| `GET /api/qb/tasks` | 下载任务列表 |
| `POST /api/watchlist/add` | 添加监听 |
| `GET /api/watchlist` | 查看监听 |
| `DELETE /api/watchlist/{id}` | 删除监听 |
| `POST /api/watchlist/check` | 立即检查全部或指定监听 |
| `GET /api/naming/jobs` | 查看自动命名记录 |
| `POST /api/naming/jobs/check` | 立即重试待处理的命名任务 |

完整契约在 [static/openapi.yaml](static/openapi.yaml)。

## QClaw / 微秘书流程

电影：

1. 用户说“下《奥本海默》”，QClaw 调用 `/api/search`，`type` 传 `movie`。
2. 把 `results` 格式化为编号列表；显示分辨率、片源、HDR、大小、语言。
3. 用户回复序号后，把该项原样传给 `/api/download`。
4. 如果 `count` 为 0，服务已自动加入监听，无需再重复调用添加接口。

电视剧：

1. 用户说“追《龙之家族》”，调用 `/api/search`，`type` 传 `tv`，展示已发现集数。
2. 用户确认全下后，调用 `/api/watchlist/add`，保存返回的项目 `id`。
3. 立即调用 `/api/watchlist/check`，请求体为 `{"item_id":"上一步的id"}`。服务会下载每集的最佳版本并记录集号。
4. 后续定时任务只下载未记录的新集。不要逐集调用 `/api/download` 后再添加监听，否则监听表不知道这些集已经手动下载。

建议 QClaw 保存“编号 → 完整搜索结果”的会话映射，用户选中后直接回传对应字段，不要自行拼接或修改磁力链接。

## 本地开发与测试

目标运行环境为 Python 3.11+：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
SCHEDULER_ENABLED=false DATA_DIR=./data python main.py
```

测试覆盖页面解析、枪版过滤、质量排序、镜像切换、qBittorrent API、监听去重和 REST 接口。当前套件要求总覆盖率不低于 80%。

## qBittorrent 与媒体库硬链接

- 新下载会先进入 `sixv-naming` 暂存分类；真实文件名改好后自动切换到 `sixv-movie` 或 `sixv-tv`。
- 下载完成后服务直接硬链接到 Emby 媒体库，因此可以关闭 nastool 对这两个目录的整理任务。
- 确保 qBittorrent Web UI 允许 NAS 内该容器访问。
- qBittorrent 4.6+ 首次启动可能生成临时密码；连接失败时先查看 qBittorrent 日志。
- 6v 老资源偶尔只有 ed2k/迅雷协议；标准 qBittorrent 对此兼容性有限，优先选择搜索结果中的磁力链接。

自动命名示例：

```text
DHF.2018.1080p.mp4       → 大黄蜂 (2018).mp4
大H蜂.chs.srt             → 大黄蜂 (2018).zh-CN.srt
第2季/01.mp4             → Season 02/剧名 - S02E01.mp4
第2季/02.mkv             → Season 02/剧名 - S02E02.mkv
```

命名依赖磁力元数据，因此刚添加时后台可能显示“等待元数据”。服务默认每分钟重试；达到失败上限会把任务释放到最终分类，避免下载永久卡在暂存区，同时在后台保留失败原因供手动处理。

硬链接目录映射：

```text
/volume1/video/Downloads/sixv-movie → /volume1/video/video/movies/片名 (年份)/
/volume1/video/Downloads/sixv-tv    → /volume1/video/video/tv/剧名/Season XX/
```

Docker 只挂载一次 `${MEDIALIB_BASE_PATH}:/medialib`，容器内对应目标为 `/medialib/video/movies` 和 `/medialib/video/tv`。不要把下载目录和媒体库拆成两个 Docker 挂载点，否则即使位于同一个存储池，Linux 也可能返回跨设备硬链接错误。目标同名文件存在时会跳过，不覆盖任何已有媒体。

## Emby 中文海报墙

此项与服务独立：

1. Emby 控制台把首选元数据语言设为“简体中文”，国家/地区设为“中国”。
2. 安装与你的 Emby 版本兼容的 MetaTube 插件，并把元数据优先级设为“豆瓣 > TMDB > IMDB”。
3. 在媒体库中选择“刷新元数据”，勾选替换已有元数据。

操作前建议备份 Emby 配置与元数据目录；插件路径和兼容版本以你的 NAS/Emby 安装方式为准。
