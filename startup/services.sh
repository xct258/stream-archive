#!/bin/bash
# ============================================================
# 项目专属：声明要启动的进程
#
#   watcher：常驻监控录制目录，录制结束自动处理
#   sync   ：启动时同步一次 cookies / 录播姬配置，之后每 24 小时一次
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
