# AI Video Station 飞牛 fnOS 应用包

这个目录是 AI Video Station 的官方 fnOS Docker FPK 包装。它不包含媒体播放器，也不依赖某一个播放器品牌；AVS 将内容整理到标准媒体目录后，飞牛影视、Emby、Jellyfin 等媒体软件都可以扫描对应的 `Library` 子目录。

## 目录布局

安装时只申请一个 `ai-video-station/media` 共享根目录，并自动创建：

```text
Downloads/
├── Movie/
├── TV/
├── Anime/
└── Custom/
Library/
├── Movies/
├── TV/
├── Anime/
└── Custom/
```

下载区与媒体库区位于同一文件系统，满足硬链接要求。下载器仍需在 AVS 管理后台连接，并根据下载器自身的容器挂载填写“下载器容器目录”。如果下载器或媒体软件由其他容器运行，还需要把同一共享根挂载给它们，并在 fnOS 中给下载器授予 `Downloads` 写权限、给媒体软件授予 `Library` 读权限；FPK 不会修改其他应用的权限或配置。

## 构建 FPK

从[飞牛应用开放平台](https://developer.fnnas.com/docs/cli/fnpack/)安装官方 `fnpack` 后，在仓库根目录执行：

```bash
./fnos/build.sh
```

默认产物为 `dist/fnos/ai-video-station-2.2.0.fpk`。也可以通过 `FNPACK_BIN=/path/to/fnpack` 指定工具位置，或给脚本传入自定义输出目录。

## 发布前验收

至少在 x86_64 和 ARM64 各验证一次：

- 能安装、打开 `/admin`、停止、启动、升级和卸载。
- 后台密钥有效，配置轮换后容器能使用新密钥，日志中不出现密钥。
- `state.db`、日志与设置在重启和升级后保留。
- qBittorrent 与 Transmission 均能从 AVS 后台连接。
- 四类目录可读写，下载源与对应 `Library` 目标的设备号一致，硬链接成功且不复制媒体数据。
- 飞牛影视、Emby 或 Jellyfin 扫描 `Library` 后能正确识别电影、电视剧和动漫。
- 外部站点或下载器不可用时有明确错误，不影响 AVS 管理后台启动。

## 上架材料

按照[官方上架说明](https://developer.fnnas.com/docs/quick-started/publish-application/)准备：

- `fnpack` 生成的最终 `.fpk`；
- 应用图标；
- 展示真实管理后台与核心流程的截图；
- x86_64、ARM64 安装与核心链路测试记录；
- manifest 中对应版本的更新说明。

当前官方流程是在飞牛粉丝群联系社区主理人，加入“应用中心开发者先锋交流群”，再按工作人员要求提交应用信息、应用包和测试材料；开发者后台正式上线后，以新平台流程为准。
