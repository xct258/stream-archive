"""处理流水线：从录制目录到 B 站稿件。

一场录播被拆成多个幂等步骤，每步完成后写状态文件：
    scan -> remux -> duration_check -> append_monthly -> encode -> cover -> upload
         -> archive -> cleanup_after

设计原则：
    - 任一步失败立即停止，状态里能看出失败在哪一步；
    - 重跑时已完成步骤直接跳过（retry 命令会重置失败步骤及之后步骤）；
    - 所有主播相关的参数都来自 StreamerConfig，代码里不出现具体主播名。
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config, StreamerConfig
from .danmaku import encode, xml_to_ass
from .log import attach_job_log, detach_job_log, get_logger
from .media import (
    CoverInfo,
    duration_diff,
    max_packet_gap,
    parse_recording_name,
    pick_cover,
    probe,
    remux_to_mp4,
)
from .asr import step_transcribe
from .monthly import step_append_monthly as _append_monthly_step
from .progress_bar import build_images
from .state import (
    STEPS,
    Job,
    begin,
    date_parts,
    fail,
    finish,
    job_file,
    job_id_from_start_time,
    list_jobs,
    load_job,
    save_job,
    skip,
    update_step_progress,
)
from .upload import extract_bvid, render_desc, render_title, upload
from .util import ensure_dir, human_size
from .webdav import step_upload_danmaku

# 小于这个大小的视频视为录制碎片，整组（视频+XML）删除
SMALL_FILE_BYTES = 10 * 1024 * 1024

# 录播姬分段切换时，视频和弹幕的尾部可能相差几十秒：
#   - 弹幕比视频长：旧文件弹幕写到切换那一刻，视频先停；
#   - 视频比弹幕长：弹幕先停，视频多出无弹幕的尾部。
# 这个范围内的差值不直接判失败：弹幕时间轴仍然对齐，只是尾部缺一段。
TAIL_DANMAKU_TOLERANCE = 300.0

# 相邻视频包 PTS 超过这个间隔，视为录制中间断流（弹幕会错位）。
GAP_TOLERANCE = 1.0

# 步骤顺序；retry 时从失败步骤开始重置

# 每个步骤返回 (说明, 是否跳过)
StepResult = tuple[str, bool]


class StepProgress:
    """把步骤进度节流写进任务状态，供网页实时显示。

    写盘前重新读任务文件，只更新当前步骤的 progress/detail，
    不会覆盖审核线程同时写入的 outputs。
    """

    def __init__(self, config: Config, job: Job, step: str, logger, min_interval: float = 5.0):
        self.config = config
        self.job = job
        self.step = step
        self.logger = logger
        self.min_interval = min_interval
        self._last_saved = 0.0
        self._last_percent = -1000.0

    def update(
        self,
        percent: float | None,
        detail: str | None = None,
        **extra,
    ) -> None:
        now = time.time()
        if percent is not None:
            if (
                abs(percent - self._last_percent) < 1.0
                and now - self._last_saved < self.min_interval
            ):
                return
        elif now - self._last_saved < self.min_interval:
            return

        self._last_saved = now
        if percent is not None:
            self._last_percent = percent
        try:
            update_step_progress(
                self.config.general.data_dir,
                self.job,
                self.step,
                percent,
                detail,
                extra or None,
            )
        except Exception:
            self.logger.debug("写入步骤进度失败", exc_info=True)


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------------------
# 主播匹配与目录发现
# ---------------------------------------------------------------------------


def find_streamer(
    config: Config, raw_name: str, directory: Path
) -> StreamerConfig | None:
    """把录制文件里的主播名映射到配置里的主播。

    匹配顺序：
        1. 名字命中 aliases / name / id，且目录在该主播声明的 source_folders 内；
        2. 目录只属于一个声明了 source_folders 的主播（录播姬昵称和配置不一致时兜底）。
    """
    enabled = config.enabled_streamers()

    for streamer in enabled:
        if streamer.source_folders and not _is_under(directory, streamer.source_folders):
            continue
        if streamer.matches(raw_name):
            return streamer

    owners = [
        streamer
        for streamer in enabled
        if streamer.source_folders and _is_under(directory, streamer.source_folders)
    ]
    if len(owners) == 1:
        return owners[0]

    return None


def _is_under(path: Path, roots: list[Path]) -> bool:
    """判断 path 是否在任意一个 root 目录下。"""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        try:
            if resolved.is_relative_to(root.resolve()):
                return True
        except OSError:
            continue
    return False


def discover_dirs(config: Config) -> list[Path]:
    """找出所有直接包含视频文件的目录。

    全局 source_folders 和主播自己的 source_folders 会合并去重。
    """
    roots = set(config.watch.source_folders)
    for streamer in config.enabled_streamers():
        roots.update(streamer.source_folders)

    result: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for current, _subdirs, files in os.walk(root):
            if any(name.lower().endswith((".flv", ".mp4")) for name in files):
                result.append(Path(current))
    return sorted(set(result))


def is_idle(directory: Path, idle_minutes: int) -> bool:
    """目录里所有文件超过 idle_minutes 没有写入，视为录制结束。"""
    latest = 0.0
    for path in directory.rglob("*"):
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    if latest == 0.0:
        return True
    return latest <= time.time() - idle_minutes * 60


# ---------------------------------------------------------------------------
# 任务构建
# ---------------------------------------------------------------------------


def build_job(
    config: Config, source_dir: Path, logger=None
) -> Job | None:
    """根据录制目录生成任务。

    返回 None 表示目录里没有视频、文件名无法解析，或主播没有配置。
    """
    logger = logger or get_logger("stream_archive.pipeline")
    videos = sorted(source_dir.glob("*.flv")) + sorted(source_dir.glob("*.mp4"))
    if not videos:
        return None

    first = min(videos, key=lambda path: path.stat().st_mtime)
    try:
        name = parse_recording_name(first.name)
    except ValueError as exc:
        logger.warning("跳过无法解析的文件：%s（%s）", first.name, exc)
        return None

    streamer = find_streamer(config, name.streamer, source_dir)
    if streamer is None:
        logger.warning(
            "未配置的主播：%s（目录 %s）。请在 config.toml 的 [[streamers]] 中添加。",
            name.streamer or "未知",
            source_dir,
        )
        return None

    start_time = name.start_time or _mtime_as_start_time(first)
    job_id = f"{streamer.id}-{job_id_from_start_time(start_time)}"

    return Job(
        id=job_id,
        streamer_id=streamer.id,
        platform=name.platform or "未知",
        streamer=streamer.name,
        stream_title=name.stream_title,
        start_time=start_time,
        source_dir=str(source_dir),
        work_dir=str(config.general.work_dir / streamer.name / start_time),
    )


def _mtime_as_start_time(path: Path) -> str:
    """文件名没有时间戳时，用文件修改时间兜底。"""
    stamp = datetime.fromtimestamp(path.stat().st_mtime)
    return stamp.strftime("%Y年%m月%d日%H点%M分%S秒")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def process_source_dir(
    config: Config,
    source_dir: Path,
    logger=None,
    *,
    cleanup_first: bool = False,
) -> bool:
    """处理一个录制目录；已处理过或上次失败的会跳过。

    cleanup_first=True 时，只在确实要开始一个新任务前清理一次旧录播，
    避免空目录每轮都触发清理。
    这个函数给 watch/once 使用；手动重试请用 retry()。
    """
    logger = logger or get_logger("stream_archive.pipeline")
    job = build_job(config, source_dir, logger)
    if job is None:
        return False

    state_path = job_file(config.general.data_dir, job.id)
    if state_path.is_file():
        existing = load_job(state_path)
        if has_failure(existing):
            logger.warning("任务 %s 上次失败，等待手动 retry，不自动重跑", existing.id)
            return False
        if is_finished(existing):
            logger.info("任务 %s 已完成，跳过", existing.id)
            return False
        job = existing
    else:
        save_job(config.general.data_dir, job)
        if cleanup_first:
            try:
                deleted = cleanup_archives(config, logger)
                logger.info("旧录播清理完成，删除 %s 个日期目录", deleted)
            except Exception:
                logger.exception("清理旧录播失败，继续处理任务")

    return process_job(config, job, logger)


def process_job(config: Config, job: Job, logger=None) -> bool:
    """按步骤执行任务。返回 True 表示全部完成。"""
    logger = logger or get_logger("stream_archive.pipeline")
    streamer = config.streamer_by_id(job.streamer_id)
    if streamer is None:
        raise RuntimeError(f"任务 {job.id} 的主播配置已不存在：{job.streamer_id}")

    handler = None
    try:
        handler = attach_job_log(config.general.log_dir, job.id)
    except OSError as exc:
        logger.warning("无法创建任务日志：%s", exc)

    try:
        return _process_job_steps(config, streamer, job, logger)
    finally:
        if handler is not None:
            detach_job_log(handler)


def _process_job_steps(config: Config, streamer, job: Job, logger) -> bool:
    """步骤执行主体；任务日志 handler 由 process_job 负责挂载/清理。"""
    reset_running(job)
    save_job(config.general.data_dir, job)

    handlers = {
        "scan": step_scan,
        "remux": step_remux,
        "duration_check": step_duration_check,
        "encode": step_encode,
        "cover": step_cover,
        "upload": step_upload,
        "append_monthly": step_append_monthly,
        "archive": step_archive,
        "transcribe": step_transcribe,
        "upload_danmaku": step_upload_danmaku,
        "cleanup_after": step_cleanup_after,
    }

    for step in STEPS:
        if job.is_done(step):
            logger.info("跳过已完成步骤：%s", step)
            continue

        logger.info("===== [%s] 开始 %s =====", job.id, step)
        begin(job, step)
        save_job(config.general.data_dir, job)

        try:
            detail, was_skipped = handlers[step](config, streamer, job, logger)
        except Exception as exc:  # 单步失败不应让守护进程退出
            logger.exception("步骤 %s 失败：%s", step, exc)
            fail(job, step, str(exc))
            save_job(config.general.data_dir, job)
            return False

        if was_skipped:
            skip(job, step, detail)
            logger.info("步骤 %s 已跳过：%s", step, detail)
        else:
            finish(job, step, detail)
            logger.info("步骤 %s 完成：%s", step, detail)

        save_job(config.general.data_dir, job)

    logger.info("任务 %s 全部完成", job.id)
    return True


def retry(config: Config, job_id: str, logger=None) -> bool:
    """手动重试：从第一个失败/未完成步骤开始执行。"""
    logger = logger or get_logger("stream_archive.pipeline")
    path = job_file(config.general.data_dir, job_id)
    if not path.is_file():
        logger.error("找不到任务：%s", job_id)
        return False

    job = load_job(path)
    if is_finished(job):
        logger.info("任务 %s 已完成，无需重试", job_id)
        return True

    reset_from_failure(job)
    return process_job(config, job, logger)


def resume_incomplete_jobs(config: Config, logger=None) -> int:
    """恢复“执行到一半进程被杀”的任务。

    只处理既没有失败、也还没完成的（running 会在 process_job 里重置为 pending），
    失败任务仍然需要人工 retry，避免自动重试把错误放大。
    """
    logger = logger or get_logger("stream_archive.pipeline")
    resumed = 0
    for job in list_jobs(config.general.data_dir):
        if is_finished(job) or has_failure(job):
            continue
        if not Path(job.work_dir).is_dir():
            continue
        logger.info("发现未完成任务 %s，继续执行", job.id)
        process_job(config, job, logger)
        resumed += 1
    return resumed


def has_failure(job: Job) -> bool:
    return any(info.get("status") == "failed" for info in job.steps.values())


def is_finished(job: Job) -> bool:
    steps = job.steps_expected or list(STEPS)
    return all(job.status_of(step) in ("done", "skipped") for step in steps)


def reset_running(job: Job) -> None:
    """把上次崩溃残留的 running 状态改回 pending。"""
    for info in job.steps.values():
        if info.get("status") == "running":
            info["status"] = "pending"
            info["detail"] = "上次执行中断，已重置"


def reset_from_failure(job: Job) -> None:
    """把第一个失败步骤及其后续步骤重置为 pending。"""
    failed_index = None
    for index, step in enumerate(STEPS):
        if job.status_of(step) == "failed":
            failed_index = index
            break
    start_index = failed_index if failed_index is not None else 0
    for step in STEPS[start_index:]:
        job.steps.pop(step, None)


# ---------------------------------------------------------------------------
# 各步骤实现
# ---------------------------------------------------------------------------


def step_scan(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """把源目录文件搬进缓存目录，并清理录制碎片。"""
    work_dir = Path(job.work_dir)
    source_dir = Path(job.source_dir)
    ensure_dir(work_dir)

    moved = 0
    moved_bytes = 0
    if source_dir.resolve() != work_dir.resolve() and source_dir.is_dir():
        for path in sorted(source_dir.iterdir()):
            if not path.is_file():
                continue
            target = work_dir / path.name
            moved_bytes += path.stat().st_size
            shutil.move(str(path), str(target))
            moved += 1
        # 源目录空了就删掉，避免一直在监控列表里
        try:
            source_dir.rmdir()
        except OSError:
            pass

    # ---- 清理碎片：视频 <10MB 的整组删除 ----
    removed = 0
    for video in list(work_dir.glob("*.flv")) + list(work_dir.glob("*.mp4")):
        if video.stat().st_size >= SMALL_FILE_BYTES:
            continue
        logger.info("清理录制碎片：%s（%s）", video.name, human_size(video.stat().st_size))
        video.unlink(missing_ok=True)
        video.with_suffix(".xml").unlink(missing_ok=True)
        removed += 1

    # ---- 清理没有视频的孤儿 XML ----
    for xml_path in work_dir.glob("*.xml"):
        if not any(
            xml_path.with_suffix(suffix).is_file() for suffix in (".mp4", ".flv")
        ):
            logger.info("清理孤儿弹幕：%s", xml_path.name)
            xml_path.unlink(missing_ok=True)
            removed += 1

    kept = sorted(path.name for path in work_dir.iterdir() if path.is_file())
    if not any(name.lower().endswith((".mp4", ".flv")) for name in kept):
        raise RuntimeError("缓存目录里没有有效视频文件")

    job.outputs["files"] = kept
    detail = (
        f"搬入 {moved} 个文件（{human_size(moved_bytes)}），"
        f"清理 {removed} 个碎片，保留 {len(kept)} 个"
    )
    return detail, False


def step_remux(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """FLV -> MP4 无损封装。

    统一转成 MP4 有两个好处：biliup 上传更稳，弹幕压制少一次解码兼容问题。
    """
    work_dir = Path(job.work_dir)
    flv_files = sorted(work_dir.glob("*.flv"))
    if not flv_files:
        return "没有需要封装的 FLV", True

    progress = StepProgress(config, job, "remux", logger)
    total = len(flv_files)
    done = 0
    for index, flv_path in enumerate(flv_files, start=1):
        mp4_path = flv_path.with_suffix(".mp4")
        # 断点续跑：上次已经封装成功，直接删掉 FLV
        if mp4_path.is_file() and mp4_path.stat().st_size > 0:
            flv_path.unlink(missing_ok=True)
        else:
            remux_to_mp4(flv_path, mp4_path)
            flv_path.unlink(missing_ok=True)
        done += 1
        progress.update(index / total * 100, f"封装 {index}/{total}：{flv_path.stem}")

    return f"封装 {done} 个 FLV", False


def step_duration_check(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """检查视频时长和最后一条弹幕的时间差。

    差值过大通常意味着录制分段、网络波动或弹幕文件不完整，
    此时强行压制会出现弹幕整体偏移，所以直接跳过这一P。

    例外：录播姬按文件分段时，旧文件的弹幕往往比视频多出几十秒
    （弹幕写到切换那一刻，视频先停）。只要视频内部没有断流，
    弹幕时间轴就是对齐的，尾部那些弹幕本来就不会显示，可以正常压制。
    """
    if not streamer.danmaku.enabled:
        return "弹幕压制已关闭，跳过时长校验", True

    work_dir = Path(job.work_dir)
    videos = sorted(work_dir.glob("*.mp4"))
    if not videos:
        raise RuntimeError("没有 MP4 文件可校验")

    pairs = []
    progress = StepProgress(config, job, "duration_check", logger)
    total = len(videos)
    for index, video in enumerate(videos, start=1):
        xml_path = video.with_suffix(".xml")
        entry = {"video": video.name, "xml": xml_path.name, "ok": False, "reason": ""}

        if not xml_path.is_file():
            entry["reason"] = "缺少弹幕文件"
        else:
            try:
                diff = duration_diff(video, xml_path)
            except Exception as exc:  # 探测失败也按不通过处理
                diff = None
                entry["reason"] = f"时长探测失败：{exc}"

            if diff is None and not entry["reason"]:
                entry["reason"] = "无法解析弹幕时间"
            elif diff is not None:
                entry["diff"] = round(diff, 3)
                limit = streamer.danmaku.max_diff_seconds
                if abs(diff) <= limit:
                    entry["ok"] = True
                elif diff < 0 and -diff <= TAIL_DANMAKU_TOLERANCE:
                    try:
                        gap = max_packet_gap(video, stop_at=GAP_TOLERANCE)
                    except Exception as exc:
                        entry["reason"] = f"断流检测失败：{exc}"
                    else:
                        if gap < GAP_TOLERANCE:
                            entry["ok"] = True
                            entry["trailing"] = round(-diff, 1)
                            logger.info(
                                "视频比弹幕短 %.1fs，但视频内部无断流，按正常分段压制：%s",
                                -diff,
                                video.name,
                            )
                        else:
                            entry["reason"] = f"视频中间断流 {gap:.1f}s，弹幕会错位"
                elif diff > 0 and diff <= TAIL_DANMAKU_TOLERANCE:
                    # 弹幕先结束：尾部没有弹幕，但前面的时间轴是对齐的
                    entry["ok"] = True
                    entry["leading"] = round(diff, 1)
                    logger.info(
                        "视频比弹幕长 %.1fs，尾部无弹幕但时间轴对齐，按正常压制：%s",
                        diff,
                        video.name,
                    )
                else:
                    entry["reason"] = (
                        f"时间差 {diff:+.1f}s 超过 {limit}s"
                    )

        if not entry["ok"]:
            logger.warning("跳过压制：%s（%s）", video.name, entry["reason"])
        pairs.append(entry)
        progress.update(index / total * 100, f"校验 {index}/{total}：{video.name}")

    job.outputs["pairs"] = pairs
    passed = sum(1 for pair in pairs if pair["ok"])
    return f"{passed}/{len(pairs)} 个视频通过时长校验", False


def step_encode(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """生成投稿版：XML -> ASS -> 烧进画面。"""
    if not streamer.danmaku.enabled:
        return "弹幕压制已关闭，跳过", True

    work_dir = Path(job.work_dir)
    pairs = [pair for pair in job.outputs.get("pairs", []) if pair.get("ok")]
    if not pairs:
        return "没有通过校验的视频，跳过压制", True

    encoded: list[str] = []
    failures: list[str] = []

    for pair in pairs:
        video = work_dir / pair["video"]
        xml_path = work_dir / pair["xml"]
        if not video.is_file() or not xml_path.is_file():
            failures.append(f"{pair['video']}: 文件缺失")
            continue

        bar_images = None
        progress = StepProgress(config, job, "encode", logger)
        try:
            ass_path = xml_to_ass(xml_path, streamer.danmaku)
            output = work_dir / f"投稿版-{video.name}"
            info = probe(video)
            progress.update(0, f"开始压制：{video.name}")

            # 可选高能进度条：生成失败不阻塞压制，退化为普通投稿版
            if streamer.encode.progress_bar:
                try:
                    bar_images = build_images(
                        xml_path,
                        info.duration,
                        info.width or 1920,
                        streamer.encode.progress_bar_height,
                        streamer.encode.progress_bar_color,
                        streamer.encode.progress_bar_smooth,
                        work_dir,
                        f"bar-{video.stem}",
                    )
                except Exception as exc:
                    logger.warning("高能进度条生成失败，改为无进度条压制：%s", exc)
                    bar_images = None

            def _on_encode_progress(
                percent: float, out_time: float, speed: str
            ) -> None:
                eta = None
                try:
                    factor = float(str(speed).rstrip("x"))
                except (TypeError, ValueError):
                    factor = 0.0
                if factor > 0 and info.duration > out_time:
                    eta = int((info.duration - out_time) / factor)
                progress.update(
                    percent,
                    f"{_clock(out_time)} / {_clock(info.duration)}，速度 {speed or '未知'}",
                    speed=speed or "",
                    processed_seconds=round(out_time, 1),
                    total_seconds=round(info.duration, 1),
                    eta_seconds=eta,
                )

            encode(
                video,
                ass_path,
                output,
                streamer.encode,
                info.duration,
                bar_images,
                on_progress=_on_encode_progress,
            )
            progress.update(100, f"压制完成：{output.name}", eta_seconds=0)
            encoded.append(str(output))
        except Exception as exc:
            logger.error("压制失败：%s（%s）", video.name, exc)
            failures.append(f"{video.name}: {exc}")
        finally:
            if bar_images is not None:
                for path in bar_images.paths():
                    path.unlink(missing_ok=True)

    if not encoded:
        raise RuntimeError("全部视频压制失败：" + "；".join(failures))

    job.outputs["encoded"] = encoded
    total = sum(Path(path).stat().st_size for path in encoded if Path(path).is_file())
    detail = f"压制 {len(encoded)} 个视频，共 {human_size(total)}"
    if failures:
        detail += f"，失败 {len(failures)} 个"
    return detail, False


def step_cover(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """从弹幕峰值处抽封面。"""
    cover: CoverInfo | None = pick_cover(Path(job.work_dir), logger)
    if cover is None:
        return "未找到可用的弹幕/视频，跳过封面", True

    job.outputs["cover"] = {
        "path": str(cover.path),
        "danmaku_count": cover.danmaku_count,
        "cover_time": cover.cover_time,
        "cover_p": cover.cover_p,
    }
    return f"封面：{cover.path.name}（弹幕 {cover.danmaku_count} 条）", False


def step_upload(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """按主播配置投稿。"""
    if not streamer.upload.enabled:
        return "该主播未启用投稿", True

    files = upload_candidates(job, streamer)
    if not files:
        return "没有可投稿的文件", True

    cover_info = job.outputs.get("cover") or {}
    date_cn = date_parts(job.start_time)[0]
    title = render_title(
        streamer.upload.title_template,
        date=date_cn,
        title=job.stream_title or job.id,
        streamer=job.streamer,
    )
    description = render_desc(
        streamer.upload.desc_template,
        title=job.stream_title,
        streamer=job.streamer,
        platform=job.platform,
        danmaku_count=int(cover_info.get("danmaku_count", 0)),
        cover_time=str(cover_info.get("cover_time", "")),
        cover_p=int(cover_info.get("cover_p", 0)),
    )
    cover_path = Path(cover_info["path"]) if cover_info.get("path") else None

    progress = StepProgress(config, job, "upload", logger)
    progress.update(None, f"biliup 上传中（{len(files)} 个文件）")
    result = upload(
        streamer.upload,
        files,
        title=title,
        description=description,
        cover=cover_path,
    )
    bvid = extract_bvid(result.detail)
    job.outputs["upload"] = {
        "ok": result.ok,
        "detail": result.detail,
        "title": title,
        "description": description,
        "bvid": bvid or "",
        "files": [str(path) for path in files],
    }
    if not result.ok:
        raise RuntimeError(f"投稿失败：{result.detail[-300:]}")
    progress.update(100, "投稿完成")
    if bvid:
        logger.info("投稿 BV：%s", bvid)

    suffix = f"（{bvid}）" if bvid else ""
    return f"投稿成功：{title}{suffix}", False


def step_append_monthly(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """按月追加，并把“已追加/总数”写进步骤进度。"""
    progress = StepProgress(config, job, "append_monthly", logger)

    def _on_progress(done_count: int, total: int, name: str) -> None:
        percent = done_count / total * 100 if total else 100.0
        detail = f"追加 {done_count}/{total}"
        if name:
            detail += f"：{name}"
        progress.update(percent, detail)

    detail, skipped = _append_monthly_step(
        config, streamer, job, logger, on_progress=_on_progress
    )
    if not skipped:
        progress.update(100, detail)
    return detail, skipped


def upload_candidates(job: Job, streamer: StreamerConfig) -> list[Path]:
    """确定要投稿的文件。

    优先级：
        1. 弹幕压制开启 -> 投稿版
        2. 弹幕压制关闭 -> 原视频 MP4
        3. 之前已经归档过 -> 使用归档路径（支持投稿失败后单独重试）
    """
    if streamer.danmaku.enabled:
        encoded = [Path(path) for path in job.outputs.get("encoded", [])]
        existing = sorted(path for path in encoded if path.is_file())
        if existing:
            return existing
        work_dir = Path(job.work_dir)
        if work_dir.is_dir():
            pressed = sorted(work_dir.glob("投稿版-*.mp4"))
            if pressed:
                return pressed
    else:
        work_dir = Path(job.work_dir)
        if work_dir.is_dir():
            raw = sorted(work_dir.glob("*.mp4"))
            if raw:
                return raw

    archived = [Path(path) for path in job.outputs.get("upload_files", [])]
    return sorted(path for path in archived if path.is_file())


def step_archive(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """把压制版和原文件移到归档目录。"""
    work_dir = Path(job.work_dir)
    pressed_dir, raw_dir = archive_dirs(config, streamer, job.start_time)

    pressed_moved: list[Path] = []
    for path in sorted(work_dir.glob("投稿版-*.mp4")):
        ensure_dir(pressed_dir)
        target = pressed_dir / path.name
        shutil.move(str(path), str(target))
        pressed_moved.append(target)

    raw_moved: list[Path] = []
    # .txt 是录播姬的问题记录，和原文件一起归档，避免 work 留下空壳目录
    for suffix in ("*.mp4", "*.flv", "*.xml", "*.txt"):
        for path in sorted(work_dir.glob(suffix)):
            ensure_dir(raw_dir)
            target = raw_dir / path.name
            shutil.move(str(path), str(target))
            raw_moved.append(target)

    # ASS 是中间产物，不归档
    for ass_path in work_dir.glob("*.ass"):
        ass_path.unlink(missing_ok=True)

    # 封面也归档：投稿失败重试时还要用
    cover_info = dict(job.outputs.get("cover") or {})
    for cover_path in sorted(work_dir.glob("*.jpg")):
        target_dir = pressed_dir if pressed_moved else raw_dir
        ensure_dir(target_dir)
        target = target_dir / cover_path.name
        shutil.move(str(cover_path), str(target))
        if cover_info.get("path") == str(cover_path):
            cover_info["path"] = str(target)
            job.outputs["cover"] = cover_info

    # 记录投稿文件路径：投稿失败重试时还能找到文件
    job.outputs["archive"] = {
        "pressed_dir": str(pressed_dir),
        "raw_dir": str(raw_dir),
    }
    job.outputs["upload_files"] = [
        str(path) for path in (pressed_moved or [path for path in raw_moved if path.suffix == ".mp4"])
    ]

    _remove_empty_dirs(work_dir)
    try:
        work_dir.rmdir()
    except OSError:
        pass
    # 顺手清理 work/<主播> 这类空目录，保留 work 根目录
    _remove_empty_dirs(config.general.work_dir)

    return (
        f"归档：压制版 {len(pressed_moved)} 个，原文件 {len(raw_moved)} 个"
    ), False


def step_cleanup_after(
    config: Config, streamer: StreamerConfig, job: Job, logger
) -> StepResult:
    """投稿成功后按配置删除本地文件。"""
    if not streamer.archive.delete_after_upload:
        return "未启用“投稿后删除”，保留本地文件", True

    # 开了语音转写时保留本地文件，避免把 txt/srt 一起删掉
    if streamer.asr.enabled:
        return "已启用语音转文本，保留本地文件（含 txt/srt）", True

    upload_info = job.outputs.get("upload") or {}
    append_info = job.outputs.get("append") or {}
    upload_ok = bool(upload_info.get("ok"))
    append_ok = bool(append_info.get("done"))
    if not upload_ok and not append_ok:
        return "投稿/按月追加均未成功，保留本地文件", True

    deleted = 0
    for path in [Path(item) for item in job.outputs.get("upload_files", [])]:
        if path.is_file():
            path.unlink()
            deleted += 1

    archive_info = job.outputs.get("archive") or {}
    raw_dir = Path(archive_info.get("raw_dir", "")) if archive_info.get("raw_dir") else None
    if raw_dir and raw_dir.is_dir():
        for path in raw_dir.iterdir():
            if path.is_file():
                path.unlink()
                deleted += 1

    # 清理空目录时从主播根目录开始，才能把空的日期目录一起删掉
    _remove_empty_dirs(config.general.videos_dir / streamer.name)

    return f"已删除本地文件 {deleted} 个", False


# ---------------------------------------------------------------------------
# 归档目录与清理
# ---------------------------------------------------------------------------


def archive_dirs(
    config: Config, streamer: StreamerConfig, start_time: str
) -> tuple[Path, Path]:
    """返回 (压制版目录, 原文件目录)。

    目录结构：<videos_dir>/<主播>/压制版|原文件/YYYY/MM/YYYY-MM-DD/
    """
    _, year, month, date_text = date_parts(start_time)
    base = config.general.videos_dir / streamer.name
    return (
        base / "压制版" / year / month / date_text,
        base / "原文件" / year / month / date_text,
    )


def protected_date_dirs(config: Config) -> dict[str, set[str]]:
    """返回暂时不能删的“主播归档名 -> 日期目录”集合。

    审核还没结论、被退回、步骤失败或修复未完成的任务，源文件可能还要用，
    在 repair.max_age_days 天内先保留对应日期，避免清理把修复素材删掉。
    """
    limit_days = max(1, config.repair.max_age_days)
    now = datetime.now()
    result: dict[str, set[str]] = {}

    for job in list_jobs(config.general.data_dir):
        try:
            created = datetime.strptime(job.created_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if (now - created).days > limit_days:
            continue

        review = job.outputs.get("review") or {}
        append_review = job.outputs.get("append_review") or {}
        repair = job.outputs.get("repair") or {}
        monthly_repair = job.outputs.get("monthly_repair") or {}

        needs_files = has_failure(job)
        # 审核中/被退回/状态未知，都先留着，等结论出来再清
        if review and review.get("kind") not in ("published",):
            needs_files = True
        if append_review and append_review.get("kind") not in ("published",):
            needs_files = True
        for info in (repair, monthly_repair):
            if info.get("status") in ("running", "failed", "pending"):
                needs_files = True

        if not needs_files:
            continue
        try:
            date_key = date_parts(job.start_time)[3]
        except Exception:
            continue
        result.setdefault(job.streamer, set()).add(date_key)

    return result


def cleanup_archives(config: Config, logger=None, dry_run: bool = False) -> int:
    """清理旧录播，返回处理的日期目录数量。

    两种模式（每个主播可单独配置）：
        days  —— 保留最近 retention_days 个自然日（含当天）
        count —— 保留最近 retention_count 个日期目录

    保护措施：
        - 只处理严格的 YYYY-MM-DD 目录；
        - 同一天的压制版/原文件一起处理；
        - 审核未定/失败/待修复的任务，对应日期在修复窗口内保留；
        - dry_run=True 时只记录将要删除的内容，不实际删除。
    """
    logger = logger or get_logger("stream_archive.pipeline")
    today = date.today()
    total = 0
    protected = protected_date_dirs(config)
    kinds = ("压制版", "原文件")

    for streamer in config.enabled_streamers():
        archive = streamer.archive
        if not archive.cleanup_enabled:
            continue
        keep_dates = protected.get(streamer.name, set())
        base = config.general.videos_dir / streamer.name

        # 同一天的两种目录归到一组，避免删一半
        dirs_by_date: dict[date, list[Path]] = {}
        for kind in kinds:
            kind_dir = base / kind
            if not kind_dir.is_dir():
                continue
            for directory in kind_dir.glob("*/*/*"):
                if not directory.is_dir():
                    continue
                try:
                    directory_date = date.fromisoformat(directory.name)
                except ValueError:
                    continue
                dirs_by_date.setdefault(directory_date, []).append(directory)

        if not dirs_by_date:
            continue

        dates = sorted(dirs_by_date, reverse=True)
        if archive.retention_mode == "count":
            candidates = dates[archive.retention_count :]
            logger.info(
                "清理模式=count：主播 %s 保留最近 %s 个日期目录，待处理 %s 个",
                streamer.id,
                archive.retention_count,
                len(candidates),
            )
        else:
            cutoff = today - timedelta(days=archive.retention_days - 1)
            candidates = [item for item in dates if item < cutoff]
            logger.info(
                "清理模式=days：主播 %s 保留 %s 天（%s 之前删除），待处理 %s 个",
                streamer.id,
                archive.retention_days,
                cutoff.isoformat(),
                len(candidates),
            )

        for directory_date in candidates:
            name = directory_date.isoformat()
            if name in keep_dates:
                logger.info("保留待处理任务的录播：%s/%s", base, name)
                continue
            removed = False
            for directory in dirs_by_date[directory_date]:
                if _cleanup_directory(
                    directory, archive.keep_xml, logger, dry_run
                ):
                    removed = True
            if removed:
                total += 1

        if not dry_run:
            _remove_empty_dirs(base)

    if not dry_run:
        _remove_empty_dirs(config.general.work_dir)
    return total


def _cleanup_directory(
    directory: Path, keep_xml: bool, logger, dry_run: bool
) -> bool:
    """删除一个日期目录里的内容；返回是否真的有可删内容。"""
    if not directory.is_dir():
        return False

    if keep_xml:
        targets = [
            path
            for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix.lower() != ".xml"
        ]
        if not targets:
            return False
        freed = sum(path.stat().st_size for path in targets)
        if dry_run:
            logger.info(
                "[dry-run] 清理旧录播（保留 XML）：%s（%s 个文件，%s）",
                directory,
                len(targets),
                human_size(freed),
            )
            return True

        removed = 0
        freed = 0
        for path in targets:
            try:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("删除文件失败：%s（%s）", path, exc)
        if removed == 0:
            return False
        logger.info(
            "清理旧录播（保留 XML）：%s（删除 %s 个文件，%s）",
            directory,
            removed,
            human_size(freed),
        )
        return True

    size = _dir_size(directory)
    if dry_run:
        logger.info("[dry-run] 清理旧录播：%s（%s）", directory, human_size(size))
        return True
    shutil.rmtree(directory)
    logger.info("清理旧录播：%s（%s）", directory, human_size(size))
    return True


def _dir_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def _remove_empty_dirs(root: Path) -> None:
    """自底向上删除空目录，root 本身保留。"""
    if not root.is_dir():
        return
    for current, _subdirs, _files in os.walk(root, topdown=False):
        path = Path(current)
        if path == root:
            continue
        try:
            path.rmdir()
        except OSError:
            pass
