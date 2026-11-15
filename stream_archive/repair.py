"""被退回后的修复处理。

流程（按用户要求）：
    1. 自动检测退回，切掉违规时间段，生成修复稿；
    2. **不自动投稿**；
    3. 等用户到 B 站删除旧稿件或违规分P；
    4. 用户手动执行 `stream-archive repair --job <id> --upload` 才重新投稿。

切片段参数对齐旧项目 `ffmpeg视频处理.sh`：
    - 违规时间段前后各扩展 5 秒；
    - `-ss`/`-to` + `-c copy` 切成 .ts 片段；
    - concat 合并成 mp4，不重编码；
    - 如果关键帧导致结果异常，才退化为整场重编码。
"""

from __future__ import annotations

import json
import re
import shutil
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .log import get_logger
from .media import FFMPEG, probe
from .recorder import cookie_string_from_json
from .review import fetch_archive_state
from .state import JOB_LOCK, Job, job_file, list_jobs, load_job, save_job
from .upload import append, extract_bvid, render_desc, upload
from .util import ensure_dir, human_size, run

P_RANGE_RE = re.compile(
    r"P(\d+)\s*[\(（]\s*(\d{1,2}):(\d{2}):(\d{2})\s*[-~—–至]\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})"
)
TIME_RANGE_RE = re.compile(
    r"(?:【|\[)?\s*(\d{1,2}):(\d{2}):(\d{2})\s*[-~—–至]\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})"
)

RIGHTS_KEYWORDS = (
    "权利人",
    "举报",
    "侵犯",
    "公共利益",
    "版权",
    "著作权",
    "肖像权",
    "名誉权",
)

REPAIR_STALE_SECONDS = 6 * 3600
PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"


class RepairError(RuntimeError):
    pass


@dataclass
class CutRange:
    pindex: int
    start: float
    end: float


def _seconds(hours: str, minutes: str, seconds: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def parse_review_ranges(review: dict) -> list[CutRange]:
    """从退回详情里解析 P 号和时间段。"""
    result: list[CutRange] = []
    for detail in review.get("problem_details") or []:
        if not isinstance(detail, dict):
            continue
        text = " ".join(
            str(detail.get(key) or "")
            for key in ("reason", "violation_time", "advice")
        )
        found = False
        for match in P_RANGE_RE.finditer(text):
            pindex = int(match.group(1))
            start = _seconds(*match.group(2, 3, 4))
            end = _seconds(*match.group(5, 6, 7))
            if end > start:
                result.append(CutRange(pindex, start, end))
                found = True
        if found:
            continue
        index = detail.get("index")
        try:
            pindex = int(index) if index and int(index) > 0 else 0
        except (TypeError, ValueError):
            pindex = 0
        for match in TIME_RANGE_RE.finditer(text):
            start = _seconds(*match.group(1, 2, 3))
            end = _seconds(*match.group(4, 5, 6))
            if end > start:
                result.append(CutRange(pindex, start, end))

    unique = []
    seen = set()
    for item in result:
        key = (item.pindex, round(item.start, 3), round(item.end, 3))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def is_rights_rejection(review: dict) -> bool:
    text = " ".join(
        str(review.get(key) or "")
        for key in ("reject_reason", "problem_title", "modify_advise")
    )
    return any(keyword in text for keyword in RIGHTS_KEYWORDS)


def _merge_ranges(
    ranges: list[CutRange], margin: int, duration: float
) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for item in sorted(ranges, key=lambda value: value.start):
        start = max(0.0, item.start - margin)
        end = min(duration, item.end + margin)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def keep_segments(
    duration: float, ranges: list[CutRange], margin: int
) -> list[tuple[float, float]]:
    """返回旧脚本逻辑下需要保留的时间段。"""
    blocked = _merge_ranges(ranges, margin, duration)
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in blocked:
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    return [(start, end) for start, end in keep if end - start >= 0.5]


def _reencode_cut(
    source: Path,
    blocked: list[tuple[float, float]],
    output: Path,
    logger=None,
) -> Path:
    """旧脚本切分结果异常时，整场重编码（慢但精确）。"""
    if logger is not None:
        logger.warning("零重编码切分失败，改为整场重编码：%s", source.name)
    condition = "+".join(
        f"between(t,{start:.3f},{end:.3f})" for start, end in blocked
    )
    ensure_dir(output.parent)
    run(
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            f"select='not({condition})',setpts=N/FRAME_RATE/TB",
            "-af",
            f"aselect='not({condition})',asetpts=N/SR/TB",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ],
        logger=logger,
        check=True,
    )
    return output


def cut_file(
    source: Path,
    ranges: list[CutRange],
    output: Path,
    margin: int,
    logger=None,
) -> Path:
    """按旧项目 `ffmpeg视频处理.sh` 的参数切掉违规片段。"""
    info = probe(source)
    duration = float(info.duration or 0)
    if duration <= 1:
        raise RepairError(f"无法读取视频时长：{source}")

    blocked = _merge_ranges(ranges, margin, duration)
    keep = keep_segments(duration, ranges, margin)
    if not blocked:
        raise RepairError("没有可删除的时间段")
    if not keep:
        raise RepairError("违规时间段覆盖了完整视频，无法切出保留内容")

    temp_dir = ensure_dir(
        output.parent / f".segments-{int(time.time())}-{output.stem[:16]}"
    )
    try:
        parts: list[Path] = []
        for index, (start, end) in enumerate(keep):
            part = temp_dir / f"part_{index}.ts"
            run(
                [
                    FFMPEG,
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    f"{start:.3f}",
                    "-to",
                    f"{end:.3f}",
                    "-i",
                    str(source),
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    "-y",
                    str(part),
                ],
                logger=logger,
                check=True,
            )
            parts.append(part)

        concat_list = temp_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{part.as_posix()}'" for part in parts) + "\n",
            encoding="utf-8",
        )
        ensure_dir(output.parent)
        run(
            [
                FFMPEG,
                "-nostdin",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                "-y",
                str(output),
            ],
            logger=logger,
            check=True,
        )

        out_duration = float(probe(output).duration or 0)
        if out_duration <= 1 or out_duration >= duration - 0.5:
            raise RepairError("零重编码切分结果时长异常")
        if logger is not None:
            logger.info(
                "切片段完成：%s -> %s（保留 %s 段，时长 %.1fs -> %.1fs）",
                source.name,
                output.name,
                len(parts),
                duration,
                out_duration,
            )
        return output
    except Exception as exc:
        if logger is not None:
            logger.warning("旧脚本切分失败，改用整场重编码：%s", exc)
        _reencode_cut(source, blocked, output, logger)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not output.is_file() or output.stat().st_size == 0:
        raise RepairError(f"修复文件生成失败：{output}")
    return output


# ---------------------------------------------------------------------------
# 文件定位
# ---------------------------------------------------------------------------


def _pressed_files(job: Job) -> list[Path]:
    files = [
        Path(path)
        for path in job.outputs.get("upload_files") or []
        if Path(path).is_file()
    ]
    if files:
        return files
    archive = job.outputs.get("archive") or {}
    pressed_dir = Path(str(archive.get("pressed_dir") or ""))
    if pressed_dir.is_dir():
        return sorted(pressed_dir.glob("投稿版-*.mp4"))
    return []


def _monthly_raw_file(job: Job) -> Path | None:
    append_info = job.outputs.get("append") or {}
    archive = job.outputs.get("archive") or {}
    raw_dir = Path(str(archive.get("raw_dir") or ""))
    names = [str(name) for name in append_info.get("done") or []]
    candidates = [raw_dir / name for name in names if (raw_dir / name).is_file()]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _streamer(config: Config, job: Job):
    return next(
        (
            item
            for item in config.enabled_streamers()
            if item.id == job.streamer_id or job.streamer in (item.id, item.name)
        ),
        None,
    )


def _repair_dir(config: Config, job: Job) -> Path:
    return config.general.data_dir / "repair" / job.id


def _write_repair_state(config: Config, job: Job, key: str, value: dict) -> None:
    """只合并修复字段，避免覆盖审核线程刚写入的状态。"""
    with JOB_LOCK:
        try:
            latest = load_job(job_file(config.general.data_dir, job.id))
        except (OSError, ValueError, KeyError):
            latest = job
        latest.outputs[key] = value
        save_job(config.general.data_dir, latest)
        job.outputs[key] = value


# ---------------------------------------------------------------------------
# 自动生成修复稿（不上传）
# ---------------------------------------------------------------------------


def _prepare_daily(config: Config, job: Job, review: dict, logger) -> dict:
    ranges = parse_review_ranges(review)
    files = _pressed_files(job)
    if not ranges:
        raise RepairError("退回原因里没有可解析的 P 号和时间段")
    if not files:
        raise RepairError("找不到已归档的压制版文件")

    by_p: dict[int, list[CutRange]] = {}
    for item in ranges:
        by_p.setdefault(item.pindex, []).append(item)
    if len(by_p) != 1:
        raise RepairError(f"一次退回涉及多个 P：{sorted(by_p)}，改为人工处理")

    pindex, p_ranges = next(iter(by_p.items()))
    if pindex == 0:
        if len(files) != 1:
            raise RepairError("退回原因没有 P 号，且该稿件有多个分P")
        pindex = 1
    if not 1 <= pindex <= len(files):
        raise RepairError(f"P{pindex} 超出文件数量 {len(files)}")

    source = files[pindex - 1]
    fixed = cut_file(
        source,
        p_ranges,
        _repair_dir(config, job) / f"修复版-{source.name}",
        config.repair.margin_seconds,
        logger,
    )
    return {
        "status": "ready",
        "kind": "daily",
        "source_bvid": str(review.get("bvid") or ""),
        "pindex": pindex,
        "source_file": str(source),
        "fixed_file": str(fixed),
        "ranges": [[item.start, item.end] for item in p_ranges],
        "attempts": 0,
        "finished_at": int(time.time()),
        "message": "修复稿已生成，请删除旧稿件或违规分P后用 repair --upload 投稿",
    }


def _prepare_monthly(config: Config, job: Job, review: dict, logger) -> dict:
    ranges = parse_review_ranges(review)
    if not ranges:
        raise RepairError("月稿退回原因里没有可解析的时间段")
    source = _monthly_raw_file(job)
    if source is None:
        raise RepairError("找不到唯一的月稿原版文件（多文件追加需人工处理）")

    fixed = cut_file(
        source,
        ranges,
        _repair_dir(config, job) / f"修复版-{source.name}",
        config.repair.margin_seconds,
        logger,
    )
    return {
        "status": "ready",
        "kind": "monthly",
        "source_bvid": str(review.get("bvid") or ""),
        "source_file": str(source),
        "fixed_file": str(fixed),
        "ranges": [[item.start, item.end] for item in ranges],
        "attempts": 0,
        "finished_at": int(time.time()),
        "message": "月稿修复稿已生成，请删除违规分P后用 repair --upload 追加",
    }


def _stale(repair: dict) -> bool:
    if repair.get("status") != "running":
        return False
    started = float(repair.get("started_at") or 0)
    return bool(started) and time.time() - started > REPAIR_STALE_SECONDS


def prepare_repair(config: Config, job: Job, logger, *, manual: bool = False) -> bool:
    """生成修复稿，不上传。manual=True 时即使自动开关关闭也执行。"""
    if not config.repair.enabled and not manual:
        return False

    review = job.outputs.get("review") or {}
    append_review = job.outputs.get("append_review") or {}

    repair = job.outputs.get("repair") or {}
    if _stale(repair):
        repair["status"] = "failed"
        repair["message"] = "上次修复中断（容器重启或超时）"
        _write_repair_state(config, job, "repair", repair)
    if review.get("kind") == "rejected" and not repair:
        if is_rights_rejection(review):
            _write_repair_state(
                config,
                job,
                "repair",
                {"status": "skipped", "kind": "daily", "message": "权利人举报类退回", "finished_at": int(time.time())},
            )
            return False
        if not parse_review_ranges(review):
            _write_repair_state(
                config,
                job,
                "repair",
                {"status": "skipped", "kind": "daily", "message": "没有具体时间段", "finished_at": int(time.time())},
            )
            return False

        plan = {
            "status": "running",
            "kind": "daily",
            "source_bvid": str(review.get("bvid") or ""),
            "started_at": int(time.time()),
            "message": "正在生成修复稿",
        }
        _write_repair_state(config, job, "repair", plan)
        try:
            ready = _prepare_daily(config, job, review, logger)
            _write_repair_state(config, job, "repair", ready)
            logger.info("修复稿已生成（未上传）：%s", ready["fixed_file"])
        except Exception as exc:
            plan = job.outputs.get("repair") or plan
            plan.update({"status": "failed", "message": str(exc)[:500], "finished_at": int(time.time())})
            _write_repair_state(config, job, "repair", plan)
            logger.error("生成修复稿失败：%s（%s）", job.id, exc)
        return True

    monthly = job.outputs.get("monthly_repair") or {}
    if _stale(monthly):
        monthly["status"] = "failed"
        monthly["message"] = "上次月稿修复中断"
        _write_repair_state(config, job, "monthly_repair", monthly)
    if append_review.get("kind") == "rejected" and not monthly:
        if is_rights_rejection(append_review):
            _write_repair_state(
                config,
                job,
                "monthly_repair",
                {"status": "skipped", "kind": "monthly", "message": "权利人举报类退回", "finished_at": int(time.time())},
            )
            return False
        if not parse_review_ranges(append_review):
            _write_repair_state(
                config,
                job,
                "monthly_repair",
                {"status": "skipped", "kind": "monthly", "message": "没有具体时间段", "finished_at": int(time.time())},
            )
            return False

        plan = {
            "status": "running",
            "kind": "monthly",
            "source_bvid": str(append_review.get("bvid") or ""),
            "started_at": int(time.time()),
            "message": "正在生成月稿修复稿",
        }
        _write_repair_state(config, job, "monthly_repair", plan)
        try:
            ready = _prepare_monthly(config, job, append_review, logger)
            _write_repair_state(config, job, "monthly_repair", ready)
            logger.info("月稿修复稿已生成（未上传）：%s", ready["fixed_file"])
        except Exception as exc:
            plan = job.outputs.get("monthly_repair") or plan
            plan.update({"status": "failed", "message": str(exc)[:500], "finished_at": int(time.time())})
            _write_repair_state(config, job, "monthly_repair", plan)
            logger.error("生成月稿修复稿失败：%s（%s）", job.id, exc)
        return True

    return False


def repair_worker(config: Config, logger) -> bool:
    jobs = list_jobs(config.general.data_dir)
    now = datetime.now()
    for job in jobs:
        try:
            created = datetime.strptime(job.created_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if (now - created).days > config.repair.max_age_days:
            continue
        if prepare_repair(config, job, logger):
            return True
    return False


def repair_worker_loop(config_path: Path, logger, interval: int = 600) -> None:
    from .config import load_config

    while True:
        try:
            config = load_config(config_path)
            if config.repair.enabled and repair_worker(config, logger):
                logger.info("本轮修复稿生成结束")
        except Exception:
            logger.exception("自动修复线程异常")
        time.sleep(max(60, interval))


# ---------------------------------------------------------------------------
# 手动上传
# ---------------------------------------------------------------------------


def _pagelist(cookies: Path, bvid: str) -> list[dict]:
    try:
        cookie = cookie_string_from_json(cookies)
    except Exception:
        return []
    url = f"{PAGELIST_URL}?bvid={bvid}"
    request = urllib.request.Request(
        url,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    if data.get("code") != 0:
        return []
    return data.get("data") or []


def _daily_mode(config: Config, job: Job, cookies: Path, force: bool) -> str:
    repair = job.outputs.get("repair") or {}
    source_bvid = str(repair.get("source_bvid") or "")
    original_count = len(_pressed_files(job)) or 1
    if not source_bvid:
        return "all"

    state = fetch_archive_state(cookies, source_bvid)
    if state.kind == "not_found":
        return "all"  # 旧 BV 已删除，重投完整集合

    pages = _pagelist(cookies, source_bvid)
    if pages and len(pages) < original_count:
        return "single"  # 删掉了分P，只补被修复的那一P

    if force:
        return "single"

    raise RepairError(
        "旧稿件仍然存在。请先在 B 站删除违规稿件或对应分P，"
        "确认后重新执行；确实要强制上传可加 --force"
    )


def upload_daily_repair(config: Config, job: Job, logger, force: bool) -> None:
    repair = dict(job.outputs.get("repair") or {})
    if repair.get("status") != "ready":
        raise RepairError("修复稿还没生成完成")
    fixed = Path(str(repair.get("fixed_file") or ""))
    if not fixed.is_file():
        raise RepairError(f"修复文件不存在：{fixed}")

    streamer = _streamer(config, job)
    if streamer is None or not streamer.upload.enabled:
        raise RepairError("找不到可投稿的主播配置")

    mode = _daily_mode(config, job, streamer.upload.cookies, force)
    original_files = _pressed_files(job)
    pindex = int(repair.get("pindex") or 1)
    if mode == "all" and original_files:
        if not 1 <= pindex <= len(original_files):
            raise RepairError(f"P{pindex} 超出文件数量")
        files = list(original_files)
        files[pindex - 1] = fixed
    else:
        files = [fixed]

    upload_info = job.outputs.get("upload") or {}
    title = str(upload_info.get("title") or job.stream_title or job.id)
    title = f"{title}{config.repair.title_suffix}"[:80]
    cover_info = job.outputs.get("cover") or {}
    description = str(upload_info.get("description") or "")
    if not description:
        description = render_desc(
            streamer.upload.desc_template,
            title=job.stream_title,
            streamer=job.streamer,
            platform=job.platform,
            danmaku_count=int(cover_info.get("danmaku_count", 0)),
            cover_time=str(cover_info.get("cover_time", "")),
            cover_p=int(cover_info.get("cover_p", 0)),
        )
    cover_path = Path(str(cover_info.get("path") or ""))
    cover = cover_path if cover_path.is_file() else None

    logger.info("手动重投：%s（%s，%s）", title, mode, human_size(sum(p.stat().st_size for p in files)))
    result = upload(streamer.upload, files, title=title, description=description, cover=cover)
    if not result.ok:
        raise RepairError(f"重投失败：{result.detail[-300:]}")
    bvid = extract_bvid(result.detail) or ""
    if not bvid:
        raise RepairError("重投成功但没解析到 BV 号，请检查投稿输出")

    repair.update(
        {
            "status": "uploaded",
            "bvid": bvid,
            "mode": mode,
            "title": title,
            "files": [str(path) for path in files],
            "attempts": int(repair.get("attempts") or 0) + 1,
            "uploaded_at": int(time.time()),
            "message": "已手动重投，等待审核",
        }
    )
    _write_repair_state(config, job, "repair", repair)
    logger.info("手动重投成功：%s", bvid)


def _reset_append_review(config: Config, job: Job, now: float) -> None:
    """追加修复版后，让审核线程重新跟踪月稿。"""
    with JOB_LOCK:
        try:
            latest = load_job(job_file(config.general.data_dir, job.id))
        except (OSError, ValueError, KeyError):
            latest = job
        latest.outputs["append_review"] = {}
        latest.outputs["append_review_until"] = now + 86400
        for key in (
            "append_review_first_at",
            "append_review_next_at",
            "append_review_attempts",
        ):
            latest.outputs.pop(key, None)
        append_info = latest.outputs.get("append") or {}
        append_info["finished_at"] = int(now)
        latest.outputs["append"] = append_info
        save_job(config.general.data_dir, latest)
        job.outputs["append_review"] = {}
        job.outputs["append_review_until"] = now + 86400
        job.outputs["append"] = append_info


def _monthly_page_exists(cookies: Path, bvid: str, source_file: Path) -> bool:
    pages = _pagelist(cookies, bvid)
    if not pages:
        return False
    names = {
        source_file.name,
        source_file.stem,
        Path(source_file.name).stem,
    }
    for page in pages:
        part = str(page.get("part") or "")
        if any(name and name in part for name in names):
            return True
    return False


def upload_monthly_repair(config: Config, job: Job, logger, force: bool) -> None:
    monthly = dict(job.outputs.get("monthly_repair") or {})
    if monthly.get("status") != "ready":
        raise RepairError("月稿修复稿还没生成完成")
    fixed = Path(str(monthly.get("fixed_file") or ""))
    if not fixed.is_file():
        raise RepairError(f"修复文件不存在：{fixed}")

    streamer = _streamer(config, job)
    if streamer is None or not streamer.upload.enabled:
        raise RepairError("找不到可投稿的主播配置")
    append_info = job.outputs.get("append") or {}
    vid = str(append_info.get("vid") or "")
    if not vid:
        raise RepairError("没有月稿 BV 号")

    source_file = Path(str(monthly.get("source_file") or ""))
    if not force and _monthly_page_exists(streamer.upload.cookies, vid, source_file):
        raise RepairError(
            "月稿里还能找到原违规分P。请先在 B 站删除该分P，"
            "确认后重新执行；确实要强制追加可加 --force"
        )

    logger.info("手动追加月稿修复版：%s -> %s", fixed.name, vid)
    result = append(streamer.upload, vid, fixed)
    if not result.ok:
        raise RepairError(f"月稿追加失败：{result.detail[-300:]}")

    monthly.update(
        {
            "status": "uploaded",
            "vid": vid,
            "attempts": int(monthly.get("attempts") or 0) + 1,
            "uploaded_at": int(time.time()),
            "message": "已手动追加修复版，等待审核",
        }
    )
    _write_repair_state(config, job, "monthly_repair", monthly)
    _reset_append_review(config, job, time.time())
    logger.info("月稿修复版追加成功，已重新纳入审核跟踪")
