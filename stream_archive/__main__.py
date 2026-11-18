"""stream-archive：命令行入口。

用法示例：
    python -m stream_archive --config config.toml check
    python -m stream_archive --config config.toml once --dir /data/rec/某主播/2026-11-13
    python -m stream_archive --config config.toml watch
    python -m stream_archive --config config.toml status
    python -m stream_archive --config config.toml retry 2026-11-13_195657
    python -m stream_archive --config config.toml cleanup
    python -m stream_archive --config config.toml sync
    python -m stream_archive --config config.toml review --bvid BV1xx411c7mD
    python -m stream_archive --config config.toml review --job 2026-11-14_194808 --wait
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .log import setup_logging
from .pipeline import (
    cleanup_archives,
    discover_dirs,
    is_idle,
    process_source_dir,
    resume_incomplete_jobs,
    retry,
)
from .recorder import sync_all
from .repair import (
    RepairError,
    prepare_repair,
    repair_worker_loop,
    upload_daily_repair,
    upload_monthly_repair,
)
from .review import (
    fetch_archive_state,
    find_bvid,
    refresh_pending_reviews,
    wait_for_review,
)
from .state import (
    job_file,
    list_jobs,
    load_job,
    now_text,
    save_job,
    write_heartbeat,
)
from .util import ensure_dir

# 刷新投稿审核状态的间隔（秒）
REVIEW_INTERVAL = 900


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_watch(config: Config, config_path: Path, logger) -> int:
    """常驻监控：录制结束 -> 自动处理。"""
    _acquire_watch_lock(config, logger)

    logger.info("开始监控：%s", ", ".join(str(path) for path in config.watch.source_folders))
    logger.info("轮询间隔 %s 秒，静默判定 %s 分钟", config.watch.poll_seconds, config.watch.idle_minutes)

    # 心跳独立线程：即使正在压制几小时，网页也能看到 watcher 存活
    heartbeat_state = {"directories": 0, "last_scan": now_text(), "last_error": ""}
    threading.Thread(
        target=_heartbeat_loop,
        args=(config, heartbeat_state),
        daemon=True,
        name="watch-heartbeat",
    ).start()
    # 审核状态独立线程：即使正在压制几小时，也每 15 分钟查一次
    threading.Thread(
        target=_review_loop,
        args=(config_path, logger),
        daemon=True,
        name="review-loop",
    ).start()
    # 自动修复线程：被退回后切片段并重投（单场），月稿只生成修复稿
    threading.Thread(
        target=repair_worker_loop,
        args=(config_path, logger),
        daemon=True,
        name="repair-loop",
    ).start()

    # 只在 watcher 启动时恢复中断任务。
    # 不能每轮都调用：手动 once 正在压制几个小时时，会被 watcher
    # 当成“未完成任务”再次拉起，造成重复压制和状态覆盖。
    try:
        resume_incomplete_jobs(config, logger)
    except Exception:
        logger.exception("恢复未完成任务失败")

    config_mtime = config_path.stat().st_mtime

    while True:
        try:
            # ---- 配置热加载：改完 config.toml 不用重启 ----
            current_mtime = config_path.stat().st_mtime
            if current_mtime != config_mtime:
                config = load_config(config_path)
                config_mtime = current_mtime
                logger.info("配置已重新加载")

            # ---- 处理已下播的目录 ----
            directories = discover_dirs(config)
            ready: list[Path] = []
            for directory in directories:
                if not is_idle(directory, config.watch.idle_minutes):
                    logger.debug("仍在写入，跳过：%s", directory)
                    continue
                ready.append(directory)

            # 只有确实有录制任务要开始时才清理旧录播，平时不再每小时空跑
            if ready:
                deleted = cleanup_archives(config, logger)
                logger.info("旧录播清理完成，删除 %s 个日期目录", deleted)

            for directory in ready:
                process_source_dir(config, directory, logger)

            heartbeat_state["directories"] = len(directories)
            heartbeat_state["last_scan"] = now_text()
            heartbeat_state["last_error"] = ""

        except Exception as exc:  # 守护进程不能因为单次异常退出
            logger.exception("监控循环异常，%s 秒后重试", config.watch.poll_seconds)
            heartbeat_state["last_error"] = str(exc)

        time.sleep(config.watch.poll_seconds)


def cmd_once(config: Config, directory: Path, logger) -> int:
    """手动处理一个录制目录，不等待静默判定。"""
    if not directory.is_dir():
        logger.error("目录不存在：%s", directory)
        return 1
    if not is_idle(directory, config.watch.idle_minutes):
        logger.warning("目录里仍有近期写入，请确认录制已结束：%s", directory)

    ok = process_source_dir(config, directory, logger)
    return 0 if ok else 1


def cmd_status(config: Config, job_id: str | None, logger) -> int:
    """查看任务状态。"""
    if job_id:
        path = job_file(config.general.data_dir, job_id)
        if not path.is_file():
            logger.error("找不到任务：%s", job_id)
            return 1
        jobs = [load_job(path)]
    else:
        jobs = list_jobs(config.general.data_dir)
        if not jobs:
            logger.info("还没有任务记录")
            return 0

    for job in jobs:
        print(f"\n任务 {job.id}  主播={job.streamer}  标题={job.stream_title or '-'}")
        print(f"  开播时间：{job.start_time}")
        print(f"  缓存目录：{job.work_dir}")
        for step, info in job.steps.items():
            status = info.get("status", "pending")
            detail = info.get("detail", "")
            marker = {
                "done": "✅",
                "skipped": "⏭️",
                "failed": "❌",
                "running": "⏳",
            }.get(status, "·")
            print(f"  {marker} {step:<15} {status:<8} {detail}")

        upload_info = job.outputs.get("upload") or {}
        if upload_info:
            print(f"  投稿：{'成功' if upload_info.get('ok') else '失败'}")
            if upload_info.get("bvid"):
                print(f"  投稿 BV：{upload_info['bvid']}")
        review_info = job.outputs.get("review") or {}
        if review_info:
            print(
                f"  审核：{review_info.get('message', '-')}"
                f"（{review_info.get('checked_at', '-')}）"
            )
        repair_info = job.outputs.get("repair") or {}
        if repair_info:
            print(
                f"  自动修复：{repair_info.get('status', '-')}"
                f" {repair_info.get('message', '')}"
            )
            if repair_info.get("bvid"):
                print(f"  修复版 BV：{repair_info['bvid']}")
        repair_review = job.outputs.get("repair_review") or {}
        if repair_review:
            print(
                f"  修复版审核：{repair_review.get('message', '-')}"
                f"（{repair_review.get('checked_at', '-')}）"
            )
        monthly_repair = job.outputs.get("monthly_repair") or {}
        if monthly_repair:
            print(
                f"  月稿修复：{monthly_repair.get('status', '-')}"
                f" {monthly_repair.get('message', '')}"
            )
    return 0


def cmd_review(config: Config, args, logger) -> int:
    """查询稿件的 B 站审核状态；--wait 时持续轮询。"""
    job = None
    bvid = (args.bvid or "").strip()

    if args.job:
        path = job_file(config.general.data_dir, args.job)
        if not path.is_file():
            logger.error("找不到任务：%s", args.job)
            return 1
        job = load_job(path)
        upload_info = job.outputs.get("upload") or {}
        bvid = str(upload_info.get("bvid") or "").strip()
        if not bvid:
            bvid = find_bvid(str(upload_info.get("detail") or "")) or ""
        if not bvid:
            append_info = job.outputs.get("append") or {}
            bvid = str(append_info.get("vid") or "").strip()
        if not bvid:
            logger.error("任务 %s 里没有找到投稿 BV 号", args.job)
            return 1

    if not bvid:
        logger.error("请用 --bvid 或 --job 指定稿件")
        return 1

    cookies = args.cookies
    if cookies is None and job is not None:
        streamer = next(
            (
                item
                for item in config.enabled_streamers()
                if item.id == job.streamer_id
                or job.streamer in (item.id, item.name)
            ),
            None,
        )
        if streamer is not None:
            cookies = streamer.upload.cookies

    if cookies is None:
        # 没有指定任务时，优先用配置里第一个存在 cookies 文件的主播
        candidates = [
            item.upload.cookies
            for item in config.enabled_streamers()
        ]
        cookies = next((path for path in candidates if path.is_file()), None)
        if cookies is None and candidates:
            cookies = candidates[0]

    if cookies is None or not cookies.is_file():
        logger.error("投稿 cookies 不存在：%s", cookies or "配置里没有 upload.cookies")
        return 1

    def persist(state) -> None:
        if job is None:
            return
        job.outputs["review"] = state.as_dict()
        save_job(config.general.data_dir, job)

    if args.wait:
        state = wait_for_review(
            cookies,
            bvid,
            timeout=args.timeout,
            interval=args.interval,
            logger=logger,
            on_update=persist,
        )
    else:
        state = fetch_archive_state(cookies, bvid)
        persist(state)

    print(f"\nBV：{state.bvid}")
    print(f"标题：{state.title or '-'}")
    print(f"状态：{state.message}")
    if state.state_desc and state.state_desc not in state.message:
        print(f"状态说明：{state.state_desc}")
    if state.reject_reason:
        print(f"退回原因：{state.reject_reason}")
    if state.problem_title:
        print(f"问题类型：{state.problem_title}")
    if state.modify_advise:
        print(f"修改建议：{state.modify_advise}")
    if state.reject_reason_url:
        print(f"规则链接：{state.reject_reason_url}")
    if state.problem_details:
        print("详细问题：")
        for number, detail in enumerate(state.problem_details, 1):
            print(f"  {number}. {detail.get('reason') or '-'}")
            if detail.get("violation_time"):
                print(f"     时间：{detail['violation_time']}")
            if detail.get("violation_position"):
                print(f"     位置：{detail['violation_position']}")
            if detail.get("advice") and detail["advice"] != state.modify_advise:
                print(f"     建议：{detail['advice']}")
    elif state.videos:
        print("详细问题：")
        for video in state.videos:
            print(
                f"  P{video.get('index') or '-'}：{video.get('reason') or '-'}"
            )
    if state.problem_description:
        text = state.problem_description
        if len(text) > 600:
            text = text[:600] + "…"
        print(f"规则说明：{text}")
    print(f"查询时间：{state.checked_at}")

    if state.kind in ("published", "self_only"):
        return 0
    if state.kind == "rejected":
        return 1
    return 3


def cmd_repair(config: Config, args, logger) -> int:
    """生成修复稿，或用户删除旧稿后手动上传。"""
    path = job_file(config.general.data_dir, args.job)
    if not path.is_file():
        logger.error("找不到任务：%s", args.job)
        return 1

    try:
        if not args.upload:
            job = load_job(path)
            prepare_repair(config, job, logger, manual=True)
            job = load_job(path)
            repair = job.outputs.get("repair") or {}
            monthly = job.outputs.get("monthly_repair") or {}
            if repair:
                print(f"单场修复：{repair.get('status')} {repair.get('message', '')}")
                print(f"  旧 BV：{repair.get('source_bvid') or '-'}")
                print(f"  P：{repair.get('pindex') or '-'}")
                print(f"  修复文件：{repair.get('fixed_file') or '-'}")
            if monthly:
                print(f"月稿修复：{monthly.get('status')} {monthly.get('message', '')}")
                print(f"  月稿 BV：{job.outputs.get('append', {}).get('vid') or '-'}")
                print(f"  修复文件：{monthly.get('fixed_file') or '-'}")
            if not repair and not monthly:
                print("没有需要修复的退回")
            print("\n删除旧稿件或违规分P后，执行：")
            print(f"  stream-archive repair --job {args.job} --upload")
            return 0

        job = load_job(path)
        repair = job.outputs.get("repair") or {}
        monthly = job.outputs.get("monthly_repair") or {}
        use_monthly = args.monthly or (
            monthly.get("status") == "ready" and repair.get("status") != "ready"
        )
        if use_monthly:
            if monthly.get("status") != "ready":
                logger.error("月稿修复稿还没准备好：%s", monthly.get("status") or "无")
                return 1
            upload_monthly_repair(config, job, logger, args.force)
        else:
            if repair.get("status") != "ready":
                logger.error("单场修复稿还没准备好：%s", repair.get("status") or "无")
                return 1
            upload_daily_repair(config, job, logger, args.force)
        return 0
    except RepairError as exc:
        logger.error("%s", exc)
        return 1
    except Exception:
        logger.exception("修复操作失败")
        return 1


def cmd_retry(config: Config, job_id: str, logger) -> int:
    return 0 if retry(config, job_id, logger) else 1


def cmd_cleanup(config: Config, logger) -> int:
    deleted = cleanup_archives(config, logger)
    logger.info("清理完成，删除 %s 个日期目录", deleted)
    return 0


def cmd_sync(config: Config, logger) -> int:
    changed = sync_all(config, logger)
    logger.info("同步完成（%s）", "有更新" if changed else "无变化")
    return 0


def cmd_check(config: Config, logger) -> int:
    """部署前自检：外部命令、cookies、目录权限。"""
    errors = 0

    for command in ("ffmpeg", "ffprobe"):
        if shutil.which(command):
            logger.info("✅ %s 已安装", command)
        else:
            logger.error("❌ 找不到 %s", command)
            errors += 1

    if config.recorder.enabled:
        binary = Path(
            os.environ.get(
                "RECORDER_BIN", "/opt/bililive/recorder/BililiveRecorder.Cli"
            )
        )
        if binary.is_file() and os.access(binary, os.X_OK):
            logger.info("✅ 录播姬已安装：%s", binary)
        else:
            logger.error(
                "❌ 未安装录播姬（构建镜像时 INSTALL_RECORDER=true）：%s", binary
            )
            errors += 1

    if config.web.enabled:
        probe = subprocess.run(
            [sys.executable, "-c", "import fastapi, uvicorn"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            logger.info("✅ FastAPI/uvicorn 已安装（监控界面）")
        else:
            logger.error(
                "❌ 未安装 FastAPI/uvicorn（构建镜像时把 install/app.conf 的 INSTALL_WEB 设为 true）"
            )
            errors += 1

    for streamer in config.enabled_streamers():
        logger.info("---- 主播 %s（%s）----", streamer.id, streamer.name)

        if streamer.danmaku.enabled and not streamer.danmaku.factory.is_file():
            logger.error("❌ DanmakuFactory 不存在：%s", streamer.danmaku.factory)
            errors += 1

        if streamer.upload.enabled:
            if not streamer.upload.biliup.is_file():
                logger.error("❌ biliup 不存在：%s", streamer.upload.biliup)
                errors += 1
            if not streamer.upload.cookies.is_file():
                logger.error("❌ 投稿 cookies 不存在：%s", streamer.upload.cookies)
                errors += 1

        if streamer.asr.enabled:
            probe = subprocess.run(
                [sys.executable, "-c", "import faster_whisper"],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0:
                logger.info("✅ faster-whisper 已安装")
            else:
                logger.error(
                    "❌ 未安装 faster-whisper（构建镜像时把 install/app.conf 的 INSTALL_ASR 设为 true）"
                )
                errors += 1



    # 目录可写检查：真正写一个临时文件，比只看权限位可靠
    for directory in (
        config.general.work_dir,
        config.general.videos_dir,
        config.general.data_dir,
        config.general.log_dir,
    ):
        try:
            ensure_dir(directory)
            probe_file = directory / ".stream-archive_write_test"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
            logger.info("✅ 目录可写：%s", directory)
        except OSError as exc:
            logger.error("❌ 目录不可写：%s（%s）", directory, exc)
            errors += 1

    if errors:
        logger.error("自检失败，共 %s 个问题", errors)
        return 1
    logger.info("自检通过")
    return 0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _review_loop(config_path: Path, logger) -> None:
    """独立线程：定时刷新单场和月稿的审核状态。"""
    while True:
        try:
            config = load_config(config_path)
            checked = refresh_pending_reviews(config, logger)
            if checked:
                logger.info("审核状态已刷新 %s 个稿件", checked)
        except Exception:
            logger.exception("刷新审核状态失败")
        time.sleep(REVIEW_INTERVAL)


def _heartbeat_loop(config: Config, state: dict) -> None:
    """每 30 秒写一次心跳，和主循环解耦。"""
    while True:
        _write_watch_heartbeat(
            config,
            int(state.get("directories", 0)),
            str(state.get("last_scan", "")),
            str(state.get("last_error", "")),
        )
        time.sleep(30)


def _write_watch_heartbeat(
    config: Config, directories: int, last_scan: str, error: str = ""
) -> None:
    """写 watcher 心跳，网页用它判断监控进程是否存活。"""
    write_heartbeat(
        config.general.data_dir,
        {
            "server_name": config.general.server_name,
            "version": __version__,
            "pid": os.getpid(),
            "source_folders": [str(path) for path in config.watch.source_folders],
            "directories": directories,
            "last_scan": last_scan,
            "last_error": error,
        },
    )


def _acquire_watch_lock(config: Config, logger) -> None:
    """保证同一时间只有一个 watch 进程，避免重复处理。"""
    lock_path = ensure_dir(config.general.data_dir) / "stream_archive.watch.lock"
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("已有 watch 进程在运行（锁文件 %s）", lock_path)
        raise SystemExit(1)
    # 把句柄挂在模块上，进程存活期间不释放
    global _WATCH_LOCK_HANDLE
    _WATCH_LOCK_HANDLE = handle


_WATCH_LOCK_HANDLE = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stream-archive",
        description="通用直播录播处理：录制监控 -> 弹幕压制 -> B站投稿 -> 归档清理",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="配置文件路径（默认 ./config.toml）",
    )
    parser.add_argument("--verbose", action="store_true", help="控制台输出 DEBUG 日志")

    # 让 --config/--verbose 也能写在子命令后面：
    # 子解析器用 SUPPRESS 默认值，避免把主解析器的值覆盖成 None。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "watch", parents=[common], help="常驻监控，录制结束后自动处理"
    )

    once = subparsers.add_parser(
        "once", parents=[common], help="立即处理一个录制目录"
    )
    once.add_argument("--dir", type=Path, required=True, help="录制目录")

    status = subparsers.add_parser("status", parents=[common], help="查看任务状态")
    status.add_argument("job_id", nargs="?", help="任务 id；不填则列出全部")

    retry_parser = subparsers.add_parser(
        "retry", parents=[common], help="重试失败任务"
    )
    retry_parser.add_argument("job_id", help="任务 id")

    subparsers.add_parser("cleanup", parents=[common], help="按保留天数清理旧录播")
    subparsers.add_parser("sync", parents=[common], help="同步 cookies 和录播姬房间配置")
    subparsers.add_parser("check", parents=[common], help="部署前自检")

    review = subparsers.add_parser(
        "review", parents=[common], help="查询/等待 B站审核状态"
    )
    target = review.add_mutually_exclusive_group(required=True)
    target.add_argument("--bvid", help="BV 号")
    target.add_argument("--job", help="任务 id（自动取投稿 BV 和 cookies）")
    review.add_argument("--wait", action="store_true", help="持续轮询直到有结果")
    review.add_argument("--timeout", type=int, default=7200, help="--wait 最长秒数")
    review.add_argument("--interval", type=int, default=300, help="轮询间隔秒数")
    review.add_argument("--cookies", type=Path, help="投稿 cookies（默认取配置）")

    repair = subparsers.add_parser(
        "repair", parents=[common], help="生成/上传被退回稿件的修复版"
    )
    repair.add_argument("--job", required=True, help="任务 id")
    repair.add_argument("--upload", action="store_true", help="确认已删除旧稿后上传")
    repair.add_argument("--monthly", action="store_true", help="处理月稿修复")
    repair.add_argument("--force", action="store_true", help="跳过旧稿件已删除检查")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    logger = setup_logging(config.general.log_dir, verbose=args.verbose)
    logger.info("stream-archive 启动（配置：%s，服务器：%s）", args.config, config.general.server_name)

    if args.command == "watch":
        return cmd_watch(config, args.config, logger)
    if args.command == "once":
        return cmd_once(config, args.dir, logger)
    if args.command == "status":
        return cmd_status(config, args.job_id, logger)
    if args.command == "retry":
        return cmd_retry(config, args.job_id, logger)
    if args.command == "cleanup":
        return cmd_cleanup(config, logger)
    if args.command == "sync":
        return cmd_sync(config, logger)
    if args.command == "check":
        return cmd_check(config, logger)
    if args.command == "review":
        return cmd_review(config, args, logger)
    if args.command == "repair":
        return cmd_repair(config, args, logger)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
