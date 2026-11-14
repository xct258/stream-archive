#!/bin/bash
# ============================================================
# 启动前准备：每次启动容器都执行（docker restart 也会执行）
#   只做可重复执行的目录初始化。
# ============================================================
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/log.sh"

mkdir -p \
    /data/work \
    /data/videos \
    /data/state \
    /data/logs \
    /data/rec \
    /data/recorder \
    /data/asr/models \
    /data/cookies/bilibili

log info "数据目录就绪"
