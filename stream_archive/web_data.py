"""Web 界面的数据层（纯标准库，不依赖 FastAPI）。

职责：
    - 读取 /data/state 下的任务状态、心跳
    - 扫描录制源目录
    - 读日志尾部
    - 生成脱敏后的配置摘要
所有路径都做了白名单/相对路径校验，避免网页读任意文件。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config
from .state import STEPS, Job, job_file, list_jobs, load_job, read_heartbeat

VIDEO_ID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}|av[0-9]+")
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# 时间与状态
# ---------------------------------------------------------------------------


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _seconds_between(start: str | None, end: str | None = None) -> int:
    start_dt = _parse_time(start)
    if not start_dt:
        return 0
    end_dt = _parse_time(end) if end else datetime.now()
    if not end_dt:
        return 0
    return max(0, int((end_dt - start_dt).total_seconds()))


def _job_status(statuses: list[str]) -> str:
    if "failed" in statuses:
        return "failed"
    if all(status in ("done", "skipped") for status in statuses):
        return "done"
    if "running" in statuses:
        return "running"
    return "pending"


def _expected(raw: dict, steps: dict) -> list[str]:
    """旧任务没有 steps_expected 时，只按实际出现过的步骤统计。"""
    expected = raw.get("steps_expected")
    if isinstance(expected, list) and expected:
        return [str(step) for step in expected]
    if steps:
        return list(steps.keys())
    return list(STEPS)


def _current_step(statuses: dict[str, str], order: list[str]) -> str:
    for step in order:
        if statuses.get(step) == "running":
            return step
    for step in order:
        if statuses.get(step, "pending") not in ("done", "skipped"):
            return step
    return ""


def _finished_at(steps: dict) -> str:
    values = [
        info.get("finished_at")
        for info in steps.values()
        if isinstance(info, dict) and info.get("finished_at")
    ]
    return max(values) if values else ""


def _video_id(outputs: dict) -> str:
    """从投稿输出或月稿件映射里提取 BV/av 号。"""
    append = outputs.get("append") or {}
    if append.get("vid"):
        return str(append["vid"])

    upload = outputs.get("upload") or {}
    detail = str(upload.get("detail") or "")
    match = VIDEO_ID_PATTERN.search(detail)
    return match.group(0) if match else ""


def job_summary(raw: dict) -> dict:
    """把状态 JSON 转成列表用的摘要。"""
    steps = raw.get("steps") or {}
    outputs = raw.get("outputs") or {}
    order = _expected(raw, steps)
    statuses = {
        step: str((steps.get(step) or {}).get("status", "pending")) for step in order
    }
    status = _job_status(list(statuses.values()))
    finished = _finished_at(steps)

    done_count = sum(1 for value in statuses.values() if value in ("done", "skipped"))
    progress = int(done_count / len(order) * 100) if order else 100

    cover = (outputs.get("cover") or {}).get("path") or ""
    cover_url = f"/api/jobs/{raw.get('id')}/cover" if cover and Path(cover).is_file() else ""
    asr_done = len((outputs.get("asr") or {}).get("done") or [])
    review = outputs.get("review") or {}
    append_review = outputs.get("append_review") or {}
    repair = outputs.get("repair") or {}
    monthly_repair = outputs.get("monthly_repair") or {}

    return {
        "id": raw.get("id", ""),
        "streamer": raw.get("streamer", ""),
        "streamer_id": raw.get("streamer_id", ""),
        "stream_title": raw.get("stream_title", ""),
        "start_time": raw.get("start_time", ""),
        "created_at": raw.get("created_at", ""),
        "status": status,
        "current_step": _current_step(statuses, order),
        "progress": progress,
        "steps_expected": order,
        "elapsed_seconds": _seconds_between(raw.get("created_at"), finished),
        "finished_at": finished,
        "upload_ok": bool((outputs.get("upload") or {}).get("ok")),
        "video_id": _video_id(outputs),
        "review": {
            "kind": str(review.get("kind") or ""),
            "message": str(review.get("message") or ""),
        },
        "append_review": {
            "kind": str(append_review.get("kind") or ""),
            "message": str(append_review.get("message") or ""),
        },
        "repair": {
            "status": str(repair.get("status") or ""),
            "message": str(repair.get("message") or ""),
        },
        "monthly_repair": {
            "status": str(monthly_repair.get("status") or ""),
            "message": str(monthly_repair.get("message") or ""),
        },
        "asr_done": asr_done,
        "cover_url": cover_url,
    }


def job_detail(raw: dict) -> dict:
    """任务详情：摘要 + 每一步状态 + 产物。"""
    summary = job_summary(raw)
    steps = raw.get("steps") or {}
    outputs = raw.get("outputs") or {}
    order = summary.get("steps_expected") or list(STEPS)

    timeline = []
    for step in order:
        info = steps.get(step) or {}
        started = info.get("started_at")
        finished = info.get("finished_at")
        timeline.append(
            {
                "name": step,
                "status": info.get("status", "pending"),
                "detail": info.get("detail", ""),
                "started_at": started or "",
                "finished_at": finished or "",
                "duration_seconds": _seconds_between(started, finished) if started else 0,
            }
        )

    archive = outputs.get("archive") or {}
    files = []
    for key in ("pressed_dir", "raw_dir"):
        directory = archive.get(key)
        if not directory:
            continue
        files.extend(_list_files(Path(directory), key))

    asr_files = (outputs.get("asr") or {}).get("files") or []

    return {
        **summary,
        "steps": timeline,
        "paths": {
            "work_dir": raw.get("work_dir", ""),
            "source_dir": raw.get("source_dir", ""),
            "pressed_dir": archive.get("pressed_dir", ""),
            "raw_dir": archive.get("raw_dir", ""),
        },
        "files": files,
        "asr_files": asr_files,
        "upload": {
            "ok": bool((outputs.get("upload") or {}).get("ok")),
            "title": (outputs.get("upload") or {}).get("title", ""),
            "bvid": (outputs.get("upload") or {}).get("bvid", ""),
            "detail": str((outputs.get("upload") or {}).get("detail", ""))[-2000:],
        },
        "append": outputs.get("append") or {},
        "review": outputs.get("review") or {},
        "append_review": outputs.get("append_review") or {},
        "repair_review": outputs.get("repair_review") or {},
        "repair": outputs.get("repair") or {},
        "monthly_repair": outputs.get("monthly_repair") or {},
        "cover_url": summary["cover_url"],
    }


def _list_files(directory: Path, kind: str) -> list[dict]:
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.iterdir()):
        if path.is_file():
            result.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "kind": kind,
                }
            )
    return result


# ---------------------------------------------------------------------------
# 任务读取
# ---------------------------------------------------------------------------


def load_job_safe(config: Config, job_id: str) -> dict | None:
    """按任务号读取状态；任务号做严格校验，防止路径穿越。"""
    if not JOB_ID_PATTERN.fullmatch(job_id):
        return None
    path = job_file(config.general.data_dir, job_id)
    if not path.is_file():
        return None
    try:
        return load_job(path).to_dict()
    except (OSError, ValueError, KeyError):
        return None


def list_job_summaries(config: Config) -> list[dict]:
    return [job_summary(job.to_dict()) for job in list_jobs(config.general.data_dir)]


def scan_recordings(config: Config) -> list[dict]:
    """扫描监控目录，返回正在录制/已静默的目录。"""
    roots: list[Path] = list(config.watch.source_folders)
    for streamer in config.enabled_streamers():
        roots.extend(streamer.source_folders)

    seen: set[Path] = set()
    result: list[dict] = []
    now = time.time()

    for root in roots:
        if not root.is_dir():
            continue
        for current, _subdirs, filenames in os.walk(root):
            videos = [
                name
                for name in filenames
                if name.lower().endswith((".flv", ".mp4"))
            ]
            if not videos:
                continue

            directory = Path(current)
            if directory in seen:
                continue
            seen.add(directory)

            size = 0
            latest = 0.0
            for name in filenames:
                path = directory / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                size += stat.st_size
                latest = max(latest, stat.st_mtime)

            age = int(now - latest) if latest else None
            idle = age is None or age >= config.watch.idle_minutes * 60
            result.append(
                {
                    "path": str(directory),
                    "files": len(filenames),
                    "videos": len(videos),
                    "size": size,
                    "last_write": (
                        datetime.fromtimestamp(latest).strftime(TIME_FORMAT)
                        if latest
                        else ""
                    ),
                    "age_seconds": age,
                    "status": "idle" if idle else "recording",
                }
            )

    result.sort(key=lambda item: item["age_seconds"] if item["age_seconds"] is not None else 10**9)
    return result[:100]


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def log_dir(config: Config) -> Path:
    return config.general.log_dir


def service_log_names() -> tuple[str, ...]:
    return ("watcher", "sync", "web")


def log_path(config: Config, name: str) -> Path | None:
    """只允许读主日志和已知 service 日志。"""
    if name == "main":
        return config.general.log_dir / "stream-archive.log"
    if name in service_log_names():
        return config.general.log_dir / "service" / f"{name}.log"
    return None


def list_logs(config: Config) -> list[dict]:
    result = []
    main = config.general.log_dir / "stream-archive.log"
    if main.is_file():
        result.append(
            {
                "name": "main",
                "label": "主日志",
                "size": main.stat().st_size,
                "path": str(main),
            }
        )
    for name in service_log_names():
        path = config.general.log_dir / "service" / f"{name}.log"
        if path.is_file():
            result.append(
                {
                    "name": name,
                    "label": f"{name} 服务",
                    "size": path.stat().st_size,
                    "path": str(path),
                }
            )
    return result


def tail_text(path: Path, lines: int = 200, max_bytes: int = 300_000) -> str:
    """高效读取文件尾部，避免把几百 MB 日志读进内存。"""
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def job_log_path(config: Config, job_id: str) -> Path | None:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        return None
    return config.general.log_dir / "jobs" / f"{job_id}.log"


def job_cover_path(config: Config, job_id: str) -> Path | None:
    raw = load_job_safe(config, job_id)
    if not raw:
        return None
    cover = (raw.get("outputs", {}).get("cover") or {}).get("path")
    if not cover:
        return None
    path = Path(cover)
    if not path.is_file():
        return None

    # 只允许 videos 或 work 目录下的封面，避免状态文件被篡改后读任意文件
    allowed_roots = [config.general.videos_dir, config.general.work_dir]
    try:
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
            return None
    except OSError:
        return None
    return path


# ---------------------------------------------------------------------------
# 总览与配置
# ---------------------------------------------------------------------------


def heartbeat_status(config: Config) -> dict:
    heartbeat = read_heartbeat(config.general.data_dir)
    if not heartbeat:
        return {"alive": False, "age_seconds": None, "last_scan": "", "directories": 0}

    updated = heartbeat.get("updated_ts")
    age = int(time.time() - float(updated)) if updated else None
    threshold = max(config.watch.poll_seconds * 2, 120)
    return {
        "alive": age is not None and age <= threshold,
        "age_seconds": age,
        "last_scan": heartbeat.get("last_scan", ""),
        "directories": heartbeat.get("directories", 0),
        "last_error": heartbeat.get("last_error", ""),
        "pid": heartbeat.get("pid"),
        "version": heartbeat.get("version", ""),
    }


def disk_usage(path: Path) -> dict | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    percent = int(usage.used / usage.total * 100) if usage.total else 0
    return {
        "path": str(path),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
    }


def overview(config: Config) -> dict:
    jobs = list_job_summaries(config)
    today = datetime.now().strftime("%Y-%m-%d")
    counts = {
        "total": len(jobs),
        "running": sum(1 for job in jobs if job["status"] in ("running", "pending")),
        "failed": sum(1 for job in jobs if job["status"] == "failed"),
        "done": sum(1 for job in jobs if job["status"] == "done"),
        "today": sum(1 for job in jobs if str(job["created_at"]).startswith(today)),
        "transcribed": sum(1 for job in jobs if job["asr_done"] > 0),
    }
    return {
        "server_name": config.general.server_name,
        "version": __version__,
        "now": datetime.now().strftime(TIME_FORMAT),
        "refresh_seconds": config.web.refresh_seconds,
        "watcher": heartbeat_status(config),
        "jobs": counts,
        "disk": disk_usage(config.general.videos_dir) or disk_usage(config.general.data_dir),
        "recordings": scan_recordings(config),
        "recent": jobs[:10],
    }


def health(config: Config) -> dict:
    watcher = heartbeat_status(config)
    return {
        "status": "ok" if watcher["alive"] else "degraded",
        "version": __version__,
        "now": datetime.now().strftime(TIME_FORMAT),
        "refresh_seconds": config.web.refresh_seconds,
        "watcher": watcher,
    }


def masked_config(config: Config) -> dict:
    """只暴露非敏感配置，密码/Token/cookie 一律不返回。"""
    streamers = []
    for streamer in config.streamers:
        streamers.append(
            {
                "id": streamer.id,
                "name": streamer.name,
                "enabled": streamer.enabled,
                "room_id": streamer.room_id,
                "aliases": streamer.aliases,
                "danmaku": streamer.danmaku.enabled,
                "upload": streamer.upload.enabled,
                "append": streamer.append.enabled,
                "webdav": streamer.webdav.enabled,
                "asr": streamer.asr.enabled,
                "progress_bar": streamer.encode.progress_bar,
            }
        )

    return {
        "server_name": config.general.server_name,
        "version": __version__,
        "paths": {
            "work_dir": str(config.general.work_dir),
            "videos_dir": str(config.general.videos_dir),
            "data_dir": str(config.general.data_dir),
            "log_dir": str(config.general.log_dir),
        },
        "watch": {
            "source_folders": [str(path) for path in config.watch.source_folders],
            "poll_seconds": config.watch.poll_seconds,
            "idle_minutes": config.watch.idle_minutes,
        },
        "web": {
            "enabled": config.web.enabled,
            "host": config.web.host,
            "port": config.web.port,
            "refresh_seconds": config.web.refresh_seconds,
        },
        "streamers": streamers,
    }


# 供 API 层复用
def load_job_object(config: Config, job_id: str) -> Job | None:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        return None
    path = job_file(config.general.data_dir, job_id)
    if not path.is_file():
        return None
    try:
        return load_job(path)
    except (OSError, ValueError, KeyError):
        return None
