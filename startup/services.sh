#!/bin/bash
# ============================================================
# 项目专属：声明要启动的进程
#
#   recorder：录播姬（镜像默认安装，启动前自动生成/更新 config.json）
#   watcher ：常驻监控录制目录，录制结束自动处理
#   sync    ：启动时同步一次 cookies / 录播姬配置，之后每 24 小时一次
#   web     ：监控界面（可选）
#
# 语音转文本不常驻：转写时由 stream-archive 启动一次性 worker，完成即退出。
#
# 每个服务的 stdout/stderr 由 supervisor 写到 /data/logs/service/<名称>.log。
# ============================================================

SERVICE_NAMES=(watcher sync)
SERVICE_STOP_TIMEOUT=([watcher]=60 [sync]=5)

start_watcher() {
    run_service watcher stream-archive --config /data/config.toml watch
}

start_sync() {
    # 先同步一次（容器启动时），之后每 24 小时刷新一次 cookies。
    # 用 bash 循环而不是额外定时器，进程由 supervisor 统一管理。
    run_service sync bash -c '
        while :; do
            stream-archive --config /data/config.toml sync || true
            sleep 86400
        done
    '
}

# ------------------------------------------------------------
# 录播姬：镜像默认安装，配置 [recorder].enabled=true 时启动。
# 启动器会先按 config.toml 生成/更新录播姬的 config.json。
# ------------------------------------------------------------
recorder_enabled() {
    [ -x /opt/bililive/recorder/BililiveRecorder.Cli ] || return 1
    python3 - <<'PY' 2>/dev/null
import sys
import tomllib
from pathlib import Path

try:
    config = tomllib.loads(Path("/data/config.toml").read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

sys.exit(0 if config.get("recorder", {}).get("enabled", True) else 1)
PY
}

if recorder_enabled; then
    SERVICE_NAMES=(recorder "${SERVICE_NAMES[@]}")
    SERVICE_STOP_TIMEOUT[recorder]=15

    start_recorder() {
        run_service recorder stream-archive-recorder --config /data/config.toml
    }
fi

# ------------------------------------------------------------
# 监控界面：配置 [web].enabled=true 且镜像里装了 FastAPI 才启动。
# ------------------------------------------------------------
web_enabled() {
    /opt/stream-archive/venv/bin/python -c "import fastapi, uvicorn" >/dev/null 2>&1 || return 1
    python3 - <<'PY' 2>/dev/null
import sys
import tomllib
from pathlib import Path

try:
    config = tomllib.loads(Path("/data/config.toml").read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

sys.exit(0 if config.get("web", {}).get("enabled") else 1)
PY
}

if web_enabled; then
    SERVICE_NAMES+=(web)
    SERVICE_STOP_TIMEOUT[web]=5

    start_web() {
        run_service web stream-archive-web --config /data/config.toml
    }
fi
