"""被退回后的自动修复。

策略：
    - 只处理有明确 P 号 + 时间段、且不是权利人举报的退回；
    - 单场投稿：从归档的压制版里切掉违规时间段，重新投一个新 BV；
    - 旧 BV 保留（退回后本来就是仅自己可见），不调用删除接口；
    - 月稿：只生成切好的原版修复稿，不自动追加，因为 biliup 不能删除旧分P；
    - 每个任务最多自动重投一次，失败后不再循环。

切视频用关键帧 copy + concat，速度很快；代价是切点可能多删几秒。
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .log import get_logger
from .media import FFMPEG, FFPROBE, probe
from .state import JOB_LOCK, Job, job_file, list_jobs, load_job, save_job
from .upload import extract_bvid, render_desc, upload
from .util import ensure_dir, human_size, run

# P1(00:46:58-00:47:11) / P2（03:52:30 - 03:52:34）
P_RANGE_RE = re.compile(
    r"P(\d+)\s*[\(（]\s*(\d{1,2}):(\d{2}):(\d{2})\s*[-~—–至]\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})"
)
# 【 02:25:26 - 02:27:09 】（没有 P 号）
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


def _merge_ranges(ranges: list[CutRange], margin: int, duration: float) -> list[tuple[float, float]]:
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


def keep_segments(duration: float, ranges: list[CutRange], margin: int) -> list[tuple[float, float]]:
    """返回需要保留的时间段。"""
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
    """关键帧切不开时，整场重编码（慢，但切割精确）。"""
    if logger is not None:
        logger.warning("关键帧切分不可用，改为整场重编码：%s", source.name)
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


def _keyframe_times(source: Path) -> list[float]:
    """列出视频关键帧时间；失败返回空列表。"""
    result = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(source),
        ],
        check=False,
    )
    if result.returncode != 0:
        return []
    times = []
    for line in result.stdout.splitlines():
        value = line.strip().rstrip(",")
        try:
            times.append(float(value))
        except ValueError:
            pass
    return sorted(set(times))


def cut_file(
    source: Path,
    ranges: list[CutRange],
    output: Path,
    margin: int,
    logger=None,
) -> Path:
    """按时间段切掉违规内容。

    优先用 segment muxer 按关键帧切分（快），切点会多删一点；
    如果关键帧太稀疏导致没有可保留片段，退化为整场重编码（慢但精确）。
    """
    info = probe(source)
    duration = float(info.duration or 0)
    if duration <= 1:
        raise RepairError(f"无法读取视频时长：{source}")

    blocked = _merge_ranges(ranges, margin, duration)
    actual = _merge_ranges(ranges, 0, duration)
    if not blocked or not actual:
        raise RepairError("没有可删除的时间段")
    if blocked[0][0] <= 0 and blocked[-1][1] >= duration:
        raise RepairError("违规时间段覆盖了完整视频，无法切出保留内容")

    keyframes = _keyframe_times(source)
    split_values: list[float] = []
    selection = blocked
    if keyframes:
        selection = actual
        for start, end in actual:
            before = [value for value in keyframes if value <= start]
            after = [value for value in keyframes if value >= end]
            # segment muxer 会切在“分段点之后的第一个关键帧”，
            # 所以取相邻关键帧的中点，保证切在想要的关键帧上
            if before:
                boundary = before[-1]
                try:
                    index = keyframes.index(boundary)
                except ValueError:
                    index = 0
                previous = keyframes[index - 1] if index > 0 else None
                split_values.append(
                    (previous + boundary) / 2 if previous is not None else boundary
                )
            if after:
                boundary = after[0]
                try:
                    index = keyframes.index(boundary)
                except ValueError:
                    index = 0
                previous = keyframes[index - 1] if index > 0 else None
                split_values.append(
                    (previous + boundary) / 2 if previous is not None else boundary
                )

    if not split_values:
        split_values = [value for pair in blocked for value in pair]

    temp_dir = ensure_dir(output.parent / f".segments-{int(time.time())}-{output.stem[:16]}")
    try:
        split_times = sorted(
            {
                round(value, 3)
                for value in split_values
                if 0.5 < value < duration - 0.5
            }
        )
        if split_times:
            part_pattern = temp_dir / "part-%03d.mp4"
            try:
                run(
                    [
                        FFMPEG,
                        "-nostdin",
                        "-v",
                        "error",
                        "-i",
                        str(source),
                        "-c",
                        "copy",
                        "-f",
                        "segment",
                        "-segment_times",
                        ",".join(f"{value:.3f}" for value in split_times),
                        "-reset_timestamps",
                        "1",
                        "-segment_format",
                        "mp4",
                        "-y",
                        str(part_pattern),
                    ],
                    logger=logger,
                    check=True,
                )
                parts = sorted(temp_dir.glob("part-*.mp4"))
                if parts:
                    kept: list[Path] = []
                    cursor = 0.0
                    for part in parts:
                        part_duration = float(probe(part).duration or 0)
                        part_start, part_end = cursor, cursor + part_duration
                        cursor = part_end
                        overlap = any(
                            not (part_end <= block_start or part_start >= block_end)
                            for block_start, block_end in selection
                        )
                        if not overlap:
                            kept.append(part)
                    if kept and len(kept) < len(parts):
                        concat_list = temp_dir / "concat.txt"
                        concat_list.write_text(
                            "\n".join(
                                f"file '{part.as_posix()}'" for part in kept
                            )
                            + "\n",
                            encoding="utf-8",
                        )
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
                                "-movflags",
                                "+faststart",
                                "-y",
                                str(output),
                            ],
                            logger=logger,
                            check=True,
                        )
                        if output.is_file() and output.stat().st_size > 0:
                            out_duration = float(probe(output).duration or 0)
                            if out_duration <= 1 or out_duration >= duration - 0.5:
                                raise RepairError("关键帧切分结果时长异常")
                            if logger is not None:
                                logger.info(
                                    "关键帧切分完成：%s -> %s（保留 %s/%s 段，删除 %s 段，时长 %.1fs -> %.1fs）",
                                    source.name,
                                    output.name,
                                    len(kept),
                                    len(parts),
                                    len(ranges),
                                    duration,
                                    out_duration,
                                )
                            return output
            except Exception as exc:
                if logger is not None:
                    logger.warning("关键帧切分失败，尝试整场重编码：%s", exc)

        _reencode_cut(source, blocked, output, logger)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not output.is_file() or output.stat().st_size == 0:
        raise RepairError(f"修复文件生成失败：{output}")
    if logger is not None:
        logger.info(
            "切片段完成：%s -> %s（删除 %s 段）",
            source.name,
            output.name,
            len(ranges),
        )
    return output


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
    append = job.outputs.get("append") or {}
    archive = job.outputs.get("archive") or {}
    raw_dir = Path(str(archive.get("raw_dir") or ""))
    names = [str(name) for name in append.get("done") or []]
    candidates = [raw_dir / name for name in names if (raw_dir / name).is_file()]
    # 一次追加多个分P时无法可靠判断是哪一段被退回，交给人工
    if len(candidates) != 1:
        return None
    return candidates[0]


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


def _repair_daily(config: Config, job: Job, review: dict, logger) -> None:
    ranges = parse_review_ranges(review)
    if not ranges:
        raise RepairError("退回原因里没有可解析的 P 号和时间段")
    if is_rights_rejection(review):
        raise RepairError("权利人举报类退回不自动切")

    files = _pressed_files(job)
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
            raise RepairError("退回原因没有 P 号，且该稿件有多个分P，改为人工处理")
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

    files[pindex - 1] = fixed
    streamer = next(
        (
            item
            for item in config.enabled_streamers()
            if item.id == job.streamer_id or job.streamer in (item.id, item.name)
        ),
        None,
    )
    if streamer is None:
        raise RepairError(f"找不到主播配置：{job.streamer_id or job.streamer}")
    if not streamer.upload.enabled:
        raise RepairError("该主播未启用投稿，不能自动重投")

    upload_info = job.outputs.get("upload") or {}
    title = str(upload_info.get("title") or job.stream_title or job.id)
    suffix = config.repair.title_suffix
    title = f"{title}{suffix}"[:80]

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

    logger.info("自动重投：%s（%s，%s）", title, len(files), human_size(sum(p.stat().st_size for p in files)))
    result = upload(streamer.upload, files, title=title, description=description, cover=cover)
    if not result.ok:
        raise RepairError(f"重投失败：{result.detail[-300:]}")

    bvid = extract_bvid(result.detail) or ""
    if not bvid:
        raise RepairError("重投成功但没解析到 BV 号，请检查投稿输出")

    repair = job.outputs["repair"]
    repair.update(
        {
            "status": "uploaded",
            "bvid": bvid,
            "title": title,
            "fixed_file": str(fixed),
            "source_bvid": str(upload_info.get("bvid") or ""),
            "pindex": pindex,
            "ranges": [[item.start, item.end] for item in p_ranges],
            "finished_at": int(time.time()),
            "message": "已重投，等待审核",
        }
    )
    _write_repair_state(config, job, "repair", repair)
    logger.info("自动重投成功：%s", bvid)


def _repair_monthly(config: Config, job: Job, review: dict, logger) -> None:
    ranges = parse_review_ranges(review)
    if not ranges:
        raise RepairError("月稿退回原因里没有可解析的时间段")
    if is_rights_rejection(review):
        raise RepairError("权利人举报类退回不自动切")

    source = _monthly_raw_file(job)
    if source is None:
        raise RepairError("找不到月稿对应的原版文件")

    fixed = cut_file(
        source,
        ranges,
        _repair_dir(config, job) / f"修复版-{source.name}",
        config.repair.margin_seconds,
        logger,
    )
    monthly = {
        "status": "ready",
        "kind": "monthly",
        "fixed_file": str(fixed),
        "source_file": str(source),
        "ranges": [[item.start, item.end] for item in ranges],
        "finished_at": int(time.time()),
        "message": "修复稿已生成，需要人工替换月稿分P",
    }
    _write_repair_state(config, job, "monthly_repair", monthly)
    logger.warning("月稿修复稿已生成：%s（biliup 不能删除旧分P，请人工处理）", fixed)


def _repair_attempts(job: Job) -> int:
    repair = job.outputs.get("repair") or {}
    try:
        return int(repair.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _stale_repair(repair: dict) -> bool:
    if repair.get("status") != "running":
        return False
    started = float(repair.get("started_at") or 0)
    return bool(started) and time.time() - started > REPAIR_STALE_SECONDS


def execute_one_repair(config: Config, job: Job, logger) -> bool:
    """尝试处理一个任务；返回 True 表示做了实际操作。"""
    if not config.repair.enabled:
        return False

    review = job.outputs.get("review") or {}
    append_review = job.outputs.get("append_review") or {}

    # ---- 单场 ----
    repair = job.outputs.get("repair") or {}
    if _stale_repair(repair):
        repair["status"] = "failed"
        repair["message"] = "上次修复中断（容器重启或超时）"
        _write_repair_state(config, job, "repair", repair)
    if review.get("kind") == "rejected" and not repair:
        if is_rights_rejection(review):
            _write_repair_state(
                config,
                job,
                "repair",
                {
                    "status": "skipped",
                    "kind": "daily",
                    "message": "权利人举报类退回，未自动处理",
                    "finished_at": int(time.time()),
                },
            )
            logger.info("跳过自动修复：权利人举报类退回（%s）", review.get("bvid"))
            return False
        if not parse_review_ranges(review):
            _write_repair_state(
                config,
                job,
                "repair",
                {
                    "status": "skipped",
                    "kind": "daily",
                    "message": "退回原因没有具体时间段，未自动处理",
                    "finished_at": int(time.time()),
                },
            )
            logger.info("跳过自动修复：退回原因没有具体时间段（%s）", review.get("bvid"))
            return False
        if _repair_attempts(job) >= config.repair.max_attempts:
            return False

        plan = {
            "status": "running",
            "kind": "daily",
            "source_bvid": str(review.get("bvid") or ""),
            "attempts": _repair_attempts(job) + 1,
            "started_at": int(time.time()),
            "message": "正在切片段并重投",
        }
        _write_repair_state(config, job, "repair", plan)
        try:
            _repair_daily(config, job, review, logger)
        except Exception as exc:
            plan = job.outputs.get("repair") or plan
            plan.update(
                {
                    "status": "failed",
                    "message": str(exc)[:500],
                    "finished_at": int(time.time()),
                }
            )
            _write_repair_state(config, job, "repair", plan)
            logger.error("自动修复失败：%s（%s）", job.id, exc)
        return True

    # ---- 月稿：只生成修复稿 ----
    monthly = job.outputs.get("monthly_repair") or {}
    if _stale_repair(monthly):
        monthly["status"] = "failed"
        monthly["message"] = "上次月稿修复中断"
        _write_repair_state(config, job, "monthly_repair", monthly)
    if append_review.get("kind") == "rejected" and not monthly:
        if is_rights_rejection(append_review):
            _write_repair_state(
                config,
                job,
                "monthly_repair",
                {
                    "status": "skipped",
                    "kind": "monthly",
                    "message": "权利人举报类退回，未自动处理",
                    "finished_at": int(time.time()),
                },
            )
            logger.info("跳过月稿修复：权利人举报类退回（%s）", append_review.get("bvid"))
            return False
        if not parse_review_ranges(append_review):
            _write_repair_state(
                config,
                job,
                "monthly_repair",
                {
                    "status": "skipped",
                    "kind": "monthly",
                    "message": "没有具体时间段，未自动处理",
                    "finished_at": int(time.time()),
                },
            )
            logger.info("跳过月稿修复：没有具体时间段（%s）", append_review.get("bvid"))
            return False
        plan = {
            "status": "running",
            "kind": "monthly",
            "source_bvid": str(append_review.get("bvid") or ""),
            "attempts": 1,
            "started_at": int(time.time()),
            "message": "正在生成月稿修复稿",
        }
        _write_repair_state(config, job, "monthly_repair", plan)
        try:
            _repair_monthly(config, job, append_review, logger)
        except Exception as exc:
            plan = job.outputs.get("monthly_repair") or plan
            plan.update(
                {
                    "status": "failed",
                    "message": str(exc)[:500],
                    "finished_at": int(time.time()),
                }
            )
            _write_repair_state(config, job, "monthly_repair", plan)
            logger.error("月稿修复失败：%s（%s）", job.id, exc)
        return True

    return False


def repair_worker(config: Config, logger) -> bool:
    """扫描任务，处理一个最合适的新退回；返回是否有动作。"""
    jobs = list_jobs(config.general.data_dir)
    now = datetime.now()
    for job in jobs:
        try:
            created = datetime.strptime(job.created_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if (now - created).days > config.repair.max_age_days:
            continue
        if execute_one_repair(config, job, logger):
            return True
    return False


def repair_worker_loop(config_path: Path, logger, interval: int = 600) -> None:
    from .config import load_config

    while True:
        try:
            config = load_config(config_path)
            if config.repair.enabled and repair_worker(config, logger):
                logger.info("本轮自动修复结束")
        except Exception:
            logger.exception("自动修复线程异常")
        time.sleep(max(60, interval))
