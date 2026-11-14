# stream-archive

通用直播录播处理工具：**录制监控 → 碎片清理 → FLV→MP4 → 弹幕压制 → B 站投稿 → 按月合集 → 归档 → 弹幕上云 → 按天清理**，附带一个只读监控界面。

- **通用多主播**：主播、房间号、投稿账号、标题/简介模板全部在 `config.toml` 里配置，代码里不出现具体主播；
- **可恢复**：每场录播拆成 11 个幂等步骤，状态写入 JSON，失败后可以断点重试；
- **核心流程零第三方 Python 依赖**：只用 3.11+ 标准库；外部依赖是 ffmpeg、DanmakuFactory、biliup；监控界面可选 FastAPI/uvicorn；
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
| `encode` | XML→ASS，ffmpeg 烧弹幕；有核显先试 QSV，失败回退 x264；可选高能进度条 |
| `cover` | 弹幕峰值处抽帧，自动避开过暗/过曝画面 |
| `upload` | biliup 投稿，自动标题、简介、封面、标签 |
| `append_monthly` | 同一主播同月的视频归到一个月稿件（首场新建，后续追加分P） |
| `archive` | 压制版 / 原文件按 `主播/年/月/日` 归档 |
| `transcribe` | 从原视频抽音频，启动本地 ASR worker 生成 txt/srt |
| `upload_danmaku` | XML 弹幕上传 WebDAV |
| `cleanup_after` | 可选：投稿成功后删除本地文件 |

任一步失败即停止，状态里记录失败原因；`retry` 会从失败步骤继续，不会重复压制或重复投稿。

## 目录结构

```
.
├── stream_archive/           # Python 实现
│   └── webui/                # 监控界面（原生 HTML/CSS/JS）
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

## 高能进度条（可选）

在画面底部叠加弹幕密度曲线：

- 已播放区域保持高亮，未播放区域压暗；
- 白色竖线是播放头，随时间移动；
- 曲线按秒统计弹幕数，开平方抑制刷屏尖峰，再做滑动平均；
- 纯 Python 生成三张小图（曲线、遮罩、播放头），不需要 matplotlib/Pillow；
- 生成失败会自动退化为普通投稿版，不影响任务。

```toml
[encode]
progress_bar = true
progress_bar_height = 64      # 像素，1080p 建议 48-80
progress_bar_opacity = 0.6    # 0-1
progress_bar_color = "0x2ED6FF"
progress_bar_smooth = 60      # 平滑窗口（秒）
```

单个主播可以单独开启：

```toml
[streamers.encode]
progress_bar = true
```

## 语音转文本（可选）

从归档的原视频抽音频，**启动一次性本地 worker** 转写，完成后进程退出、模型释放：

```toml
[asr]
enabled = true
model = "large-v3-turbo"
language = "zh"
device = "auto"           # auto / cpu / cuda
compute_type = "auto"     # auto 时 CUDA 用 float16，CPU 用 int8
model_dir = "/data/asr/models"
beam_size = 5
hf_endpoint = ""          # 国内可填 https://hf-mirror.com
timeout = 14400           # worker 单次最长秒数
```

说明：

- 需要镜像构建时 `INSTALL_ASR=true`（默认），安装 faster-whisper；
- **不启动常驻 HTTP 服务，也不提交到外部服务**；
- 每场转写启动一个 `stream-archive-asr` 子进程，处理完所有音频后退出，内存/显存自动释放；
- 音频提取为 16kHz 单声道 AAC，4 小时约 115MB；
- 结果写同名 `.txt` / `.srt`，保存在「原文件」目录；
- 模型不打进镜像，首次转写时下载到 `model_dir`；也可以提前放进去；
- CPU 转写较慢，建议用 GPU 或 `small` / `base` 模型；
- 失败会让任务停在 `transcribe`，`retry` 会复用已提取的音频；
- 开启语音转文本后，`delete_after_upload` 会跳过删除，避免把 txt/srt 一起删掉。

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

容器里 `services.sh` 启动以下进程：

- `recorder`：录播姬（默认安装，启动前自动生成/更新配置）；
- `watcher`：常驻监控录制目录；
- `sync`：启动时同步一次 cookies / 录播姬配置，之后每 24 小时一次；
- `web`：监控界面（配置 `[web].enabled=true` 且镜像装了 FastAPI 时启动）。

镜像内置 `fonts-noto-cjk`，默认 `[danmaku].font_name = "Noto Sans CJK SC"`；
如果直接在宿主机运行，需要自行安装中文字体，或在配置里换成已有字体。

## 录播姬（默认安装）

镜像默认安装 BililiveRecorder。第一次启动时 `stream-archive-recorder` 会：

- 根据 `[[streamers]].room_id` 生成/更新 `<recorder.work_dir>/config.json`；
- 写入录制 cookies；
- 打开自动录制、弹幕/礼物录制；
- 校正文件名模板；
- 配置留空时生成管理面板密码到 `<work_dir>/.http_password`。

```toml
[recorder]
enabled = true
work_dir = "/data/rec"
http_bind = "http://127.0.0.1:2356"
http_user = "admin"
http_password = ""          # 留空自动生成
record_cookie_json = "/data/cookies/bilibili/cookies-录播账号.json"
```

- 录制输出：`/data/rec/video/<录播姬昵称>/<日期>/`
- `watch.source_folders` 默认 `/data/rec`，会递归找到上面的目录；
- 管理面板默认只绑 `127.0.0.1:2356`，建议用 nginx 反代并加认证；
- 修改房间号后，`sync` 会更新 `config.json`，但录播姬进程需要重启才会加载新房间（`docker compose restart`）；
- 镜像构建时 `INSTALL_RECORDER=true`（默认）会下载录播姬；`RECORDER_VERSION` 可固定版本。

## 监控界面（M1）

FastAPI + 原生 JS，不经过前端构建。默认只读，操作类功能在后续阶段加入。

```toml
[web]
enabled = true
host = "127.0.0.1"   # 对外暴露请改 0.0.0.0 并放在反向代理后
port = 8383
refresh_seconds = 5
```

页面：

- **总览**：watcher 心跳、磁盘、任务统计、录制目录、最近任务
- **任务**：按状态/主播/关键字筛选，点击进入详情
- **任务详情**：逐步状态、耗时、封面、产物列表、任务日志
- **日志**：主日志和 service 日志，支持 tail 和自动刷新

认证不在 Web 内做，推荐用 Nginx/Caddy 加 Basic Auth 或统一登录。注意 `network_mode: host` 下端口会直接暴露在宿主机。

构建时 `install/app.conf` 的 `INSTALL_WEB=true`（默认）会安装 FastAPI/uvicorn；设为 `false` 可减小镜像，但 `[web].enabled` 不要开启。

本地运行：

```bash
stream-archive-web --config /data/config.toml
```

## 状态与日志

- 任务状态：`/data/state/jobs/<主播id>-<时间>.json`
- 运行日志：`/data/logs/stream-archive.log`、`/data/logs/jobs/<任务号>.log`
- 进程日志：`/data/logs/service/watcher.log`、`sync.log`、`web.log`
- 归档：`/data/videos/<主播>/压制版|原文件/YYYY/MM/YYYY-MM-DD/`

## 本地安装（不用 Docker）

```bash
git clone https://github.com/xct258/stream-archive.git
cd stream-archive

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[web,asr]"
```

- `[web]`：FastAPI + uvicorn，监控界面；
- `[asr]`：faster-whisper + 固定 `av==18.1.0`（av 19 移除了 faster-whisper 1.2.1 使用的参数）；
- 只查看界面可以只装 `.[web]`；
- ffmpeg 用系统包；DanmakuFactory / biliup 放到 `bin/` 或 PATH，在 config 里指向；
- ASR 模型不打进镜像/仓库，首次转写时下载到 `[asr].model_dir`。

启动：

```bash
stream-archive-web --config config.toml
```

## 本地开发与测试

```bash
python3 -m unittest discover -s tests -v
python3 -m stream_archive --help
```

测试全部离线运行：用假 ffmpeg / biliup 和本地 `http.server` 覆盖流水线、按月投稿、WebDAV，不需要真实视频和网络。

生成模拟录播（真实视频 + 弹幕 XML）：

```bash
python3 scripts/make_test_media.py --output /tmp/demo-media
```

默认生成两段（60 秒 + 30 秒）、1280x720@30 的 FLV 和对应的中文弹幕 XML，可以直接用 `once --dir` 跑完整流水线。

## 构建说明

`install/`、`startup/`、`template/` 遵循 `xct258/docker-image-template`：

- `install/app.conf` 配置镜像名、架构、apt 依赖；
- `install/custom-install.sh` 下载 BililiveRecorder、DanmakuFactory、biliup，并用 `APP_REPO/APP_REF` 安装本项目；
- `INSTALL_RECORDER=true`（默认）安装录播姬；录播姬启动时由本项目生成/更新配置；
- `INSTALL_ASR=true`（默认）时额外安装 faster-whisper，提供内置语音转文本；设为 `false` 可减小镜像；
- `INSTALL_WEB=true`（默认）时安装 FastAPI/uvicorn，提供监控界面；设为 `false` 可减小镜像；
- `startup/*.sh` 负责每次启动、首次初始化、服务声明；
- `template/` 是固定模板文件，构建时从当前仓库按提交 SHA 下载。

修改代码后推送到 `main`（或把 `APP_REF` 改成对应 tag/SHA），再手动触发 Actions 构建即可。

## 已知限制

- 只出投稿版；高能进度条是叠加在投稿版上的可选项，不做单独的预览版；
- 不做 rclone 视频备份、在线切片；
- 内置 ASR 的转写速度取决于 CPU/GPU 和模型大小，长录播可能较慢；
- 录播姬房间/账号变更后需要重启容器，才会让录制进程重新加载配置；
- WebDAV 只支持 Basic Auth，不支持 Digest / 分块上传；
- 监控界面 M1 为只读，重试/手动处理等操作在 M2 加入。
