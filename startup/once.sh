#!/bin/bash
# ============================================================
# 只执行一次：首次启动时执行，成功后写标记 /data/.startup-once.done
#   首次启动生成 /data/config.toml。
#   想重新生成：删掉 /data/.startup-once.done 和 /data/config.toml 后重启。
# ============================================================
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/log.sh"

if [ ! -f /data/config.toml ]; then
    cp /opt/stream-archive/config.example.toml /data/config.toml
    log -f warn "已生成默认配置 /data/config.toml，请按需修改后执行 docker compose restart"
fi

log info "初始化完成"
