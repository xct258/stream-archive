#!/bin/bash
# ============================================================
# 项目专属：声明要启动的进程
#
#   watcher：常驻监控录制目录，录制结束自动处理
#   sync   ：启动时同步一次 cookies / 录播姬配置，之后每 24 小时一次
#   asr    ：可选，内置 faster-whisper 服务（需构建时 INSTALL_ASR=true，
#            且运行时 [asr].enabled=true、[asr].embedded=true）
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
# 内置 ASR：镜像里装了 faster-whisper，且配置要求嵌入式才启动。
# 读 /data/config.toml；文件不存在时返回失败，不影响 watcher/sync。
# ------------------------------------------------------------
asr_enabled() {
    [ -f /opt/stream-archive/.asr-installed ] || return 1
    python3 - <<'PY' 2>/dev/null
import sys
import tomllib
from pathlib import Path

try:
    config = tomllib.loads(Path("/data/config.toml").read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

asr = config.get("asr", {})
sys.exit(0 if asr.get("enabled") and asr.get("embedded") else 1)
PY
}

if asr_enabled; then
    SERVICE_NAMES+=(asr)
    SERVICE_STOP_TIMEOUT[asr]=10

    start_asr() {
        run_service asr stream-archive-asr --config /data/config.toml
    }
fi
