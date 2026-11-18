"""按月投稿：把同一主播同一自然月的视频追加到同一个 B 站稿件。

工作方式：
    - 映射文件记录“主播归档名|YYYY-MM = BV号”，一行一条；
    - 当月第一次处理：用第一个视频新建稿件，它自然成为 P1；
    - 之后同一月的其它视频/场次：biliup append --vid 追加为新的分P；
    - 同一个任务重试时，已追加的文件名记在任务状态的 append.done 里，
      不会重复追加。

限制：
    新建稿件成功、但进程在写映射文件之前被杀，重试可能重复建稿。
    biliup 没有幂等键，遇到这种情况按日志里的 BV 号手工修正映射即可。
"""

from __future__ import annotations

import time
from pathlib import Path

from .config import AppendConfig, Config, StreamerConfig
from .log import get_logger
from .review import fetch_page_count
from .state import Job, date_parts, save_job
from .upload import append, extract_bvid, list_recent_vid, upload
from .util import atomic_write_text, ensure_dir
from .webdav import month_public_url


# ---------------------------------------------------------------------------
# 月份与映射文件
# ---------------------------------------------------------------------------


def month_of(start_time: str) -> tuple[str, str]:
    """从开播时间得到 (2026-11, 2026年11月)。"""
    _, year, month, _ = date_parts(start_time)
    return f"{year}-{month}", f"{year}年{month}月"


def map_path(config: Config, append_config: AppendConfig) -> Path:
    """映射文件路径：配置为空时放在 data_dir 下。"""
    if append_config.map_file:
        return Path(append_config.map_file)
    return config.general.data_dir / "append_month_vid.map"


def map_key(streamer_name: str, month: str) -> str:
    """映射键：主播归档名|YYYY-MM。用归档名是为了兼容旧版映射文件。"""
    return f"{streamer_name}|{month}"


def load_vid_map(path: Path) -> dict[str, str]:
    """读取映射文件，格式 key=BV号；坏行直接跳过。"""
    if not path.is_file():
        return {}

    mapping: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def save_vid_map(path: Path, mapping: dict[str, str]) -> None:
    """排序后原子写入，方便人眼对比和手工修改。"""
    ensure_dir(path.parent)
    lines = [f"{key}={value}" for key, value in sorted(mapping.items())]
    atomic_write_text(path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 模板与候选文件
# ---------------------------------------------------------------------------


def render_template(
    template: str, streamer: str, month: str, month_cn: str, pan_url: str = ""
) -> str:
    """替换 {streamer} {month} {month_cn} {pan_url}。

    用 str.replace 而不是 format，避免简介里出现其它花括号时报错。
    """
    return (
        template.replace("{streamer}", streamer)
        .replace("{month_cn}", month_cn)
        .replace("{month}", month)
        .replace("{pan_url}", pan_url)
        .strip()
    )


def candidates(job: Job, source: str) -> list[Path]:
    """本次任务里可以追加的视频。

    raw:     原文件 MP4（优先）和 FLV，排除投稿版
    pressed: 压制版
    按文件名排序，保证分P顺序与时间顺序一致。
    """
    work_dir = Path(job.work_dir)

    if source == "pressed":
        files = [
            Path(path)
            for path in job.outputs.get("encoded", [])
            if Path(path).is_file()
        ]
        if not files and work_dir.is_dir():
            files = sorted(work_dir.glob("投稿版-*.mp4"))
        return sorted(files)

    files = [
        path
        for path in work_dir.glob("*.mp4")
        if not path.name.startswith("投稿版-")
    ]
    files += list(work_dir.glob("*.flv"))
    return sorted(path for path in files if path.is_file())


# ---------------------------------------------------------------------------
# 流水线步骤
# ---------------------------------------------------------------------------


def step_append_monthly(
    config: Config,
    streamer: StreamerConfig,
    job: Job,
    logger=None,
    on_progress=None,
) -> tuple[str, bool]:
    """把本场视频追加到月稿件；返回 (说明, 是否跳过)。

    on_progress(已追加数, 本任务文件总数, 文件名) 用于网页显示分P进度。
    """
    logger = logger or get_logger("stream_archive.monthly")
    append_config = streamer.append

    if not append_config.enabled:
        return "未启用按月投稿", True

    files = candidates(job, append_config.source)
    if not files:
        return "没有可追加的视频", True

    month, month_cn = month_of(job.start_time)
    key = map_key(streamer.name, month)
    path = map_path(config, append_config)
    mapping = load_vid_map(path)
    vid = mapping.get(key, "")

    # 本次任务的追加进度：换稿件（映射被手工修改）时清空
    info = job.outputs.setdefault(
        "append", {"vid": vid, "done": [], "created": False}
    )
    if info.get("vid") != vid:
        info["vid"] = vid
        info["done"] = []
    done = set(info.get("done", []))

    pending = [file for file in files if file.name not in done]
    if not pending:
        if on_progress is not None:
            try:
                on_progress(len(files), len(files), "")
            except Exception:
                pass
        return f"{month} 的 {len(files)} 个文件已全部追加（{vid}）", True

    # 追加前记录分P数量：审核通过后用它核对追加是否真的生效
    if "pages_before" not in info:
        if not vid:
            info["pages_before"] = 0
        else:
            pages = fetch_page_count(streamer.upload.cookies, vid)
            if pages is not None:
                info["pages_before"] = pages
                save_job(config.general.data_dir, job)

    created = False
    if not vid:
        # ---- 当月第一场：新建月稿件，首个视频就是 P1 ----
        first = pending[0]
        pan_url = month_public_url(streamer.webdav, streamer.name, job.start_time)
        title = render_template(
            append_config.title_template, streamer.name, month, month_cn, pan_url
        )
        description = render_template(
            append_config.desc_template, streamer.name, month, month_cn, pan_url
        )
        cover = Path(append_config.cover) if append_config.cover else None

        logger.info("按月投稿：%s 尚无稿件，用 %s 新建：%s", month, first.name, title)
        result = upload(
            streamer.upload,
            [first],
            title=title,
            description=description,
            cover=cover,
        )
        if not result.ok:
            raise RuntimeError(f"新建月稿件失败：{result.detail[-300:]}")

        vid = extract_bvid(result.detail) or list_recent_vid(
            streamer.upload, title, logger
        )
        if not vid:
            raise RuntimeError(
                "月稿件投稿成功但未解析到 BV 号，请手动写入映射："
                f"{path}（键 {key}）"
            )

        mapping[key] = vid
        save_vid_map(path, mapping)
        logger.info("按月投稿：新建稿件成功 %s -> %s", key, vid)

        info["vid"] = vid
        info["created"] = True
        info["done"].append(first.name)
        done.add(first.name)
        save_job(config.general.data_dir, job)
        if on_progress is not None:
            try:
                on_progress(len(done), len(files), first.name)
            except Exception:
                pass

        created = True
        pending = pending[1:]

    # ---- 其余文件逐个追加；每成功一个就落盘，重试不重复 ----
    for file in pending:
        result = append(streamer.upload, vid, file)
        if not result.ok:
            raise RuntimeError(f"追加分P失败：{file.name}：{result.detail[-300:]}")
        info["done"].append(file.name)
        done.add(file.name)
        save_job(config.general.data_dir, job)
        if on_progress is not None:
            try:
                on_progress(len(done), len(files), file.name)
            except Exception:
                pass

    # 记录追加完成时间：审核观察期从这里开始算，而不是任务创建时间
    info["finished_at"] = int(time.time())
    save_job(config.general.data_dir, job)

    added = len(pending) + (1 if created else 0)
    action = "新建并追加" if created else "追加"
    return f"{action} {added} 个文件到 {vid}（{month} 共 {len(done)} 个）", False
