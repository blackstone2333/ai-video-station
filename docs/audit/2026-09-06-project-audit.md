# AI Video Station 项目审查报告

审查日期：2026-09-06  
审查方式：只读代码审查、现有单元/集成测试、隔离临时目录验证。  
本轮没有修改业务代码，也没有部署。

## 结论摘要

项目的主链路已经具备可用形态：站点搜索、订阅、qBittorrent/Transmission、逐文件命名、硬链接、重复检测、Agent API 和 fnOS 打包都有对应实现，状态也已迁移到 SQLite。当前最大风险不是功能数量，而是“异步状态与文件系统状态不同步”：任务可能被标记为已下载但没有入库，监听事件可能丢失，清理可能把未由 AVS 管理的文件当成可删除项。

建议先做 P0/P1 修复，再考虑新功能。特别是清理/洗版的自动执行，在以下保护补齐前应保持关闭。

## 验证结果

- 测试：233 项通过，1 项失败。
- 失败项：`tests/unit/test_directory_sync.py::test_directory_sync_watcher_events_and_debouncing`。
- 失败原因：当前本地 `.venv` 没有安装 `watchdog`；`requirements.txt` 已声明 `watchdog==6.0.0`，因此这是审查环境依赖缺失，不是已确认的业务回归。
- 覆盖率：79.66%，低于 `pyproject.toml` 要求的 80%。即使补装依赖，也应补充关键异常路径测试。

## P0：应立即修复

### P0-1 目录去重在重叠目标目录时可能误删保留项

位置：`ainas/directory_sync.py:397-428`。

`deduplicate_library_links()` 虽然去重了目标根目录，但在每个根目录内重复枚举文件。若规则为 `/Library` 和 `/Library/nested`，同一个 inode 会出现多次，排序后可能生成 `kept == removed` 的候选。隔离临时目录验证得到 `found=2`，其中一项就是同一路径自删候选；实际执行可能解除本应保留的硬链接。

影响：重复清理可能误删媒体库文件，属于数据安全问题。

建议：按规范化绝对路径和 `(st_dev, st_ino)` 双重去重；执行前拒绝 `kept == removed`；路径规则保存时直接拒绝目标目录互相嵌套。增加 dry-run 回归测试。

### P0-2 清理扫描把外部文件标成 AVS 管理文件

位置：`ainas/cleanup.py:218-248`、`171-186`。

扫描到的所有视频都写入 `managed=True`，即使文件来自 Ani-RSS、手工复制或其他下载器、没有对应 naming job。`automatic_selections()` 只检查 `managed/safe`，电影身份又默认 `reliable=True`。因此开启自动清理后，纯文件名推断的外部电影也可能被选中；开启删除源文件时，没有 `torrent_hash` 的文件会直接 `unlink`，无法确认外部下载器是否仍在管理它。

影响：可能删除 AVS 无法恢复或仍在下载器管理中的源文件。

建议：`managed` 只能在找到命名任务/明确来源元数据时为真；无任务的文件默认 `safe=False`，仅允许人工逐项确认；源文件删除前必须有来源所有权、任务哈希和硬链接计数校验。自动执行默认继续关闭。

### P0-3 BT 种子手动下载可绕过枪版/TS 拦截

位置：`ainas/services.py:327-336`，同类磁力兜底在 `services.py:244-248`。

`build_release()` 识别到 CAM/TS 会返回空，但手动流程随后调用 `_fallback_release()`，而兜底没有再次执行 `is_cam_release()`，最后仍会提交下载。也就是说同一资源在搜索流程会被拦截，在手动 `.torrent` 流程却可能进入 qBittorrent。

影响：违背“禁止下载版本”策略，且预览与实际提交结果可能不一致。

建议：抽出公共 `validate_release_blocked()`，在磁力、BT、预览和最终提交四处统一调用；命中时返回结构化原因，不创建下载任务。

## P1：可靠性与状态一致性

### P1-1 目录监听事件可能永久丢失

位置：`ainas/directory_sync.py:722-746`。

触发同步前会先从 `_pending_paths` 移除事件。如果扫描时下载器仍活动、文件未稳定或同步锁被占用，函数返回后没有重新排队；Ani-RSS 或网络盘随后不一定再产生新事件，文件会永久留在下载目录而不入库。

建议：失败/等待状态保留事件并采用有上限的指数退避；锁竞争不应丢事件；记录重试次数和最后检测路径。

### P1-2 服务重启期间新增文件没有补偿扫描

位置：`DirectorySyncWatcher.start()`，`ainas/directory_sync.py:605-620`。

启动只注册 watchdog，不检查监听开始前已经存在的新增文件。重启、容器升级或 watchdog 短暂不可用期间的文件只能靠人工全量扫描。

建议：每条规则启动时做一次增量补偿扫描（按 mtime/inode 游标），并持久化未完成事件；补偿扫描必须复用幂等去重逻辑。

### P1-3 命名任务的单个坏文件会阻塞整包

位置：`ainas/naming.py:1095` 附近的 `_require_safe_episode_plan()` 调用链。

整包校验发生在本轮 `ready` 文件处理前。33 集中只要有 1 个集号无法识别，已完成的 32 集也不会逐文件命名/入库，仍然违反“下好一个处理一个”。

建议：以文件为粒度校验和提交；可识别文件立即命名和硬链接；不可识别文件单独进入 `waiting_selection`，不能阻塞同一任务的其他文件。

### P1-4 手动 BT 的占位标题没有统一清洗

位置：`ainas/services.py:327-336`。

磁力流程会把“手动下载”当作空标题处理，但 multipart `.torrent` 流程没有同样归一化。表单默认值可能直接进入命名计划，形成“手动下载”目录或无法识别的媒体。

建议：磁力、BT 和两种 preview 共用同一个标题归一化函数，并在提交前拒绝空的不可识别标题。

### P1-5 硬链接状态读取未统一归一化下载进度

位置：`ainas/naming.py:1317-1335`，以及下载器 `torrent_info()` 实现。

部分下载器在 `completion_on` 已有值时仍返回 `progress=0`。任务列表使用了归一化进度，但 `_finish_hardlink()` 直接读取原始 `progress`，会把已完成任务继续留在 `waiting_download`。

建议：所有完成判断统一调用同一个 `normalized_task_progress()`/`_task_complete()`，并增加 `completion_on + progress=0` 回归测试。

### P1-6 已完成任务被“检查”后可能回退

位置：`ainas/naming.py:1389-1410`。

带 `job_id` 的检查对 `status=completed` 任务无条件调用 `_finish_hardlink()`。如果 QB 之后清理了任务或元数据短暂不可用，原本成功的记录会被降级为 `waiting_download`/`failed`。

建议：`hardlink_status=done/disabled` 时保持终态；需要重新验证时提供显式的 `revalidate` 操作。

### P1-7 订阅在下载任务真正成功前就记账

位置：`ainas/services.py:601-623`。

提交到下载器成功后立即写入 `downloaded_episodes`，命名失败、任务被删除或下载器拒绝后没有回滚。订阅下一轮会认为该集已下载，导致无法自动重试。

建议：拆分 `submitted` 与 `completed` 记录；只有下载/命名/入库完成后才写入已完成集数，或保存任务哈希并在失败时自动释放占位。

### P1-8 Transmission 丢失具体目录规则

位置：`ainas/transmission.py:206-214`。

完成命名后调用 `set_category()` 会按全局类别目录移动，忽略订阅选择的具体 `path_rule`，导致 Transmission 与 qBittorrent 的路径行为不一致。

建议：任务记录保存 `downloader_path`，完成时按任务级路径移动；加 qB/Transmission 对等测试。

### P1-9 Transmission 跨目录重命名能力不足

位置：`ainas/transmission.py:189-200`。

Transmission 明确拒绝跨目录 rename，而电影计划可能把种子内嵌套文件移动到规范目录。该组合会在下载完成后稳定进入失败/重试。

建议：提交前检查下载器能力；不支持时生成兼容计划或明确提示用户选择 qBittorrent，不能等到完成后才失败。

### P1-10 纯数字剧集的订阅识别仍可能漏集

位置：`ainas/quality.py:423-425`、`ainas/services.py:489-511`。

命名阶段支持 `01.mkv` 等数字前缀，但资源解析/订阅记账没有在所有入口统一启用 `allow_numeric_prefix`。国产剧按 `01、02、03` 分开发布时，可能只记录首条、不能准确累加已下载集数。

建议：在解析、命名、订阅记账使用同一集数解析器，并为“纯数字文件名 + 电视剧上下文”增加数据驱动测试。

## P1：权限与 API 契约

### P1-11 手动下载的“同时订阅”缺少 watchlist 权限校验

位置：`ainas/app.py:361-418`，端点权限映射在 `app.py:202-205`。

端点只要求 `download` scope，但 `subscribe=true` 时直接创建订阅；仅有下载权限的 Agent 可以越权写入 watchlist。并且若事后再补权限检查，下载任务已经提交。

建议：在任何下载副作用之前，根据 `subscribe` 预先要求 `watchlist` scope；授权失败时不得创建下载任务。

### P1-12 目录硬链接去重被错误归到 naming 权限

位置：`ainas/app.py:224`、`766-778`。

`dry_run=false` 会解除媒体库硬链接，但 Agent scope 映射为 `naming`，而 UI 将重复清理作为独立能力。只有 naming 权限的 Agent 可以执行破坏性清理。

建议：预览使用只读 scope；真正执行必须要求 `cleanup` scope、明确确认令牌和一次性计划版本。

### P1-13 搜索结果的显式类型选择可能被缓存类型覆盖

位置：`ainas/services.py` `DownloadService.download()`，约 `160` 行。

下载时只要缓存结果存在就采用 `cached.media_type`，忽略请求中用户明确选择的类型。缓存识别为电视剧时，即使 Agent/UI 选择动漫，也会走 TV 分类和路径。

建议：显式用户选择优先；若与缓存识别冲突，返回冲突提示让调用方确认，不要静默覆盖。

### P1-14 无 AVS 任务的同名不同年份电影可能被当成重复

位置：`ainas/cleanup.py:141-149`。

没有命名任务时，电影身份依赖 `plan.year`，通常为空；例如 `The Thing (1982)` 与 `The Thing (2011)` 可能归入同一个 `the thing:` 组，洗版计划会把一部当成另一部的低质量版本。

建议：从文件名可靠解析年份；解析不到时标记 `safe=False`，不进入自动选择。

### P1-15 硬链接重试会把已跳过文件重新纳入安全校验

位置：`ainas/naming.py:1347-1354`。

`_finish_hardlink()` 将下载器返回的全部文件传给 `_require_safe_episode_plan()`，没有沿用首次处理时的 selected/priority 过滤。全集中若包含花絮、padding 或无集号文件，首次命名可能成功，后续硬链接重试却因这些被跳过文件无法识别而整单失败。

建议：重试只校验待入库且 priority>0 的文件；被明确跳过的文件保留原因并不阻塞其他文件。

## P2：前端与运维体验

### P2-1 分页响应契约不匹配

后端 `ainas/app.py:630-640`、`736-746` 返回 `pagination` 嵌套对象；前端 `static/admin.js:652-653` 把整个响应当 meta，`renderPagination()` 在 `306-309` 读取顶层字段。超过一页后仍显示 1/1，旧失败记录无法从页面翻到。

### P2-2 async 事件处理器在 await 后使用 `event.currentTarget`

位置：`static/admin.js:992`、`1029`、`1036`、`1043`、`1068`、`1070`。

浏览器规范规定异步回调在 `await` 后 `currentTarget` 已不再可用。结果可能是实际保存成功却抛异常，表单不重置、列表不刷新或按钮保持禁用，用户容易重复提交。

建议：处理器入口立即缓存 `const form = event.currentTarget` / `const button = event.currentTarget`，后续只使用缓存引用。

### P2-3 局部接口失败会把页面已有数据清空

位置：`static/admin.js:645-657`。

`Promise.allSettled()` 对失败接口使用空数组 fallback，并覆盖当前 state。短暂网络或下载器故障会让页面显示“暂无记录”、统计归零；静默刷新还不会提示用户。

建议：失败时保留上一份数据，增加分区级 stale/error 标识；只在请求成功后替换对应 state。

### P2-4 日志时间筛选没有后端实现

前端 `static/admin.js:689` 发送 `since=<ISO>`，后端 `ainas/app.py:780-793` 只解析 `limit/level/query`，所以“从某时间起”筛选实际上无效。

### P2-5 硬链接记录不能直接打开目标子目录

位置：`static/admin.js:250-257`。

当前只能复制路径并提示用户去 NAS 文件管理器打开，没有受控的目录跳转/浏览动作。若保留此需求，应增加安全的目录浏览 API 或生成受控 deep-link，禁止任意路径读取。

## 建议升级路线

### 2.3（P0/P1，先保证不误删、不漏处理）

1. 修复重叠目录去重，并禁止嵌套规则。
2. 给目录事件增加持久化/重试队列、启动补偿扫描和统一锁。
3. 统一下载完成度、命名与逐文件处理语义。
4. 修复清理的 managed/ownership 保护，默认人工确认。
5. 修复手动 BT 的 CAM/TS 绕过、Transmission 路径和纯数字剧集识别。
6. 修复权限 scope、分页和 async `currentTarget` 契约。

验收标准：重启不漏文件；一个坏集不阻塞其他集；相同 inode 在重叠规则下只保留一份；无 AVS 任务的文件不会进入自动删除；下载器显示完成时 AVS 在一次检查内完成命名/入库；所有破坏性 Agent 操作需要 cleanup scope。

### 2.4（P2，降低维护成本）

- 孤儿硬链接检测、预览和可回收列表。
- 文件删除/移动/重命名事件的媒体库反向清理。
- 统一任务详情：失败原因、重试次数、最后检测路径、来源任务哈希。
- 完善 MCP/CLI：目录扫描、去重预览/执行、重试和日志查询。
- 将关键故障补成回归测试，使覆盖率稳定高于 80%。

### 暂不建议现在做

- 大规模刮削和自动替换媒体库名称：会扩大外部元数据依赖，并可能覆盖用户已有命名。
- 多进程 worker/分布式队列：当前状态模型明确按单主机、单 worker 设计，先把幂等和锁边界补齐。
- 自动删除未确认的源文件：在来源所有权元数据完成前保持关闭。

## 已具备的优点

- API 有统一 Problem Details 错误格式、请求 ID、基本安全响应头和限流。
- SQLite 状态写入使用 WAL、忙等待和完整同步，适合单 NAS 部署。
- 路径有容器/宿主机转换及 symlink 防护，硬链接前检查同一文件系统。
- 命名计划、硬链接结果和清理计划都有持久化记录，具备补偿和审计的基础。
- 搜索默认已与订阅解耦（`add_to_watchlist=false`），这是 Agent 多轮试探所需的正确方向。
