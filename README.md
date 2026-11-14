# stream-archive

通用直播录播处理工具：**录制监控 → 碎片清理 → FLV→MP4 → 弹幕压制 → B 站投稿 → 按月合集 → 归档 → 弹幕上云 → 按天清理**。

- **通用多主播**：主播、房间号、投稿账号、标题/简介模板全部在 `config.toml` 里配置，代码里不出现具体主播；
- **可恢复**：每场录播拆成 10 个幂等步骤，状态写入 JSON，失败后可以断点重试；
- **零第三方 Python 依赖**：只用 3.11+ 标准库；外部依赖是 ffmpeg、DanmakuFactory、biliup；
- **容器化**：基于 `xct258/docker-image-template` 构建多架构镜像，配置和启动逻辑与模板一致。

## 处理流程

### 1. 判断录制结束

`watch` 每 `watch.poll_seconds`（默认 300 秒）扫描一次监控目录：

- 找出直接包含 `*.flv` / `*.mp4` 的目录；
- 目录内所有文件的**最大 mtime** 超过 `watch.idle_minutes`（默认 20 分钟）没有更新，就判定为录制结束。

所以触发处理的时间是 **20～25 分钟**之后，不依赖录播姬 API。

### 2. 十个步骤

状态文件：`<data_dir>/jobs/<任务号>.json`。

| 步骤 | 作用 |
|---|---|
| `scan` | 把录制文件搬进缓存目录，删除 `<10MB` 碎片和孤儿 XML |
| `remux` | FLV → MP4 无损封装 |
| `duration_check` | 视频时长与最后一条弹幕时间差超过阈值则跳过压制 |
| `encode` | XML→ASS，ffmpeg 烧弹幕；有核显先试 QSV，失败回退 x264 |
| `cover` | 弹幕峰值处抽帧，自动避开过暗/过曝画面 |
| `upload` | biliup 投稿，自动标题、简介、封面、标签 |
| `append_monthly` | 同一主播同月的视频归到一个月稿件（首场新建，后续追加分P） |
| `archive` | 压制版 / 原文件按 `主播/年/月/日` 归档 |
| `upload_danmaku` | XML 弹幕上传 WebDAV |
| `cleanup_after` | 可选：投稿成功后删除本地文件 |

任一步失败即停止，状态里记录失败原因；`retry` 会从失败步骤继续，不会重复压制或重复投稿。

## 目录结构

```
.
├── stream_archive/           # Python 实现
├── tests/                    # 标准库 unittest
├── config.example.toml       # 配置示例
├── install/                  # 模板：构建期依赖与安装
│   ├── app.conf
│   ├── install.sh
│   └── custom-install.sh
├── startup/                  # 模板：运行期启动流程
│   ├── prepare.sh
│   ├── once.sh
│   ├── update.sh
│   ├── services.sh
│   └── main.sh
├── template/                 # 模板固定文件（Dockerfile/start/supervisor）
├── check.sh                  # push 前自查，CI 也调用
├── docker-compose.yml
└── .github/workflows/build-image.yml
```

## 快速开始

### 1. 构建镜像

镜像由 GitHub Actions 构建，本地不需要 Docker：

1. 在仓库 Settings → Secrets and variables → Actions 添加 `DOCKERACC`、`DOCKERPWD`；
2. Actions 页面选择 **Build and Push Docker Image** → Run workflow；
3. 镜像推送到 `install/app.conf` 里的 `IMAGE_REPO:IMAGE_TAG`。

### 2. 启动容器

```bash
docker compose up -d
```

首次启动会在 `/data/config.toml` 生成默认配置，同时创建数据目录。

### 3. 修改配置

```bash
vi data/config.toml
docker compose restart
```

至少需要改：

- `watch.source_folders`：录制源目录（映射到容器里的路径）；
- `[[streamers]]`：主播 `aliases`、`room_id`、投稿 cookies、直播间地址；
- WebDAV、按月投稿、清理策略按需开启。

录制目录通过 compose 挂载，例如：

```yaml
volumes:
  - ./data:/data
  - /path/to/recordings:/data/rec
```

## 配置多主播

全局 `[danmaku]`、`[encode]`、`[upload]`、`[archive]`、`[append]`、`[webdav]` 是默认值，每个 `[[streamers]]` 只写差异：

```toml
[[streamers]]
id = "demo"
name = "主播A"                       # 归档目录名
aliases = ["直播间昵称"]              # 匹配录制文件名
room_id = 123456

[streamers.upload]
cookies = "/data/cookies/bilibili/cookies-账号A.json"
source = "https://live.bilibili.com/123456"
tags = ["直播回放"]

[streamers.encode]
crf = 20
```

新增主播只需要追加一段 `[[streamers]]`，不需要改代码。

匹配规则：

1. 录制文件名里的主播名命中 `aliases` / `name` / `id`；
2. 或目录属于该主播的 `source_folders`，且只有一个主播声明了该目录。

匹配不到会跳过并提示在配置里补充。

## 按月投稿

```toml
[append]
enabled = true
source = "raw"              # raw=原文件，pressed=压制版
title_template = "{streamer} {month_cn} 直播回放合集"
desc_template = "本稿件为{streamer}{month_cn}原版直播回放合集，每次直播追加为一个分P。"
cover = ""                  # 可选封面
```

- 当月第一场：用第一个视频新建稿件（P1）；
- 之后：`biliup append --vid` 追加分P；
- 映射文件记录 `主播归档名|YYYY-MM=BV号`，重跑不会重复追加；
- 简介里可用 `{pan_url}`，开启 WebDAV 后自动替换成当月弹幕目录。

## 弹幕上传 WebDAV

```toml
[webdav]
enabled = true
url = "https://pan.example.com/dav"
username = "xxx"
password = "xxx"
path = "/danmaku/{streamer}/{yyyy}/{mm}/{date}"
public_url = "https://pan.example.com/danmaku"
only_appended = false
insecure = false
```

- 用标准库发 HTTP `MKCOL` + `PUT`，不需要 rclone；
- 路径模板支持 `{streamer} {yyyy} {mm} {dd} {month} {date}`；
- 每个 XML 按自己文件名里的时间归档；
- `only_appended = true` 只上传成功追加到月稿件的 XML；
- 支持 Basic Auth；自签名证书设 `insecure = true`。

## 命令

```bash
stream-archive --config /data/config.toml watch
stream-archive --config /data/config.toml once --dir /data/rec/主播/2026-11-14
stream-archive --config /data/config.toml status
stream-archive --config /data/config.toml retry <任务号>
stream-archive --config /data/config.toml cleanup
stream-archive --config /data/config.toml sync
stream-archive --config /data/config.toml check
```

容器里 `services.sh` 启动了两个进程：

- `watcher`：常驻监控；
- `sync`：启动时同步一次 cookies / 录播姬配置，之后每 24 小时一次。

## 状态与日志

- 任务状态：`/data/state/jobs/<主播id>-<时间>.json`
- 运行日志：`/data/logs/stream-archive.log`、`/data/logs/jobs/<任务号>.log`
- 进程日志：`/data/logs/service/watcher.log`、`sync.log`
- 归档：`/data/videos/<主播>/压制版|原文件/YYYY/MM/YYYY-MM-DD/`

## 本地开发与测试

```bash
python3 -m unittest discover -s tests -v
python3 -m stream_archive --help
```

测试全部离线运行：用假 ffmpeg / biliup 和本地 `http.server` 覆盖流水线、按月投稿、WebDAV，不需要真实视频和网络。

## 构建说明

`install/`、`startup/`、`template/` 遵循 `xct258/docker-image-template`：

- `install/app.conf` 配置镜像名、架构、apt 依赖；
- `install/custom-install.sh` 下载 DanmakuFactory、biliup，并用 `APP_REPO/APP_REF` 安装本项目；
- `startup/*.sh` 负责每次启动、首次初始化、服务声明；
- `template/` 是固定模板文件，构建时从当前仓库按提交 SHA 下载。

修改代码后推送到 `main`（或把 `APP_REF` 改成对应 tag/SHA），再手动触发 Actions 构建即可。

## 已知限制

- 只出投稿版，不含预览版 / 高能进度条；
- 不做 rclone 视频备份、音频提取、语音识别、在线切片；
- 录制端（录播姬）需要单独运行，本工具只消费它的输出目录；
- WebDAV 只支持 Basic Auth，不支持 Digest / 分块上传。
