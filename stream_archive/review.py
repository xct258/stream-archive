"""B 站投稿审核状态检查。

biliup 输出里的“投稿成功”只代表接口接单，稿件还要过 B 站审核。
这个模块用投稿账号的 cookies 查询稿件列表里的 state，判断：

    0      已通过审核，公开
    -1/-2/-30  审核中
    -50    仅自己可见（已通过审核，但不是公开状态）
    -100 等  未通过/已下线

只依赖标准库，不引入额外 HTTP 库。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .recorder import cookie_string_from_json

ARCHIVE_LIST_URL = "https://member.bilibili.com/x/web/archives"
# 每页最大 100 条；账号稿件多时最多翻这么多页
DEFAULT_MAX_PAGES = 30
PAGE_SIZE = 50

BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")

FINAL_KINDS = {"published", "self_only", "rejected"}


@dataclass
class ReviewState:
    bvid: str
    state: int | None
    kind: str
    message: str
    title: str = ""
    checked_at: str = ""
    # 退回/审核不通过时 B 站返回的详细信息
    state_desc: str = ""
    reject_reason: str = ""
    problem_title: str = ""
    problem_description: str = ""
    modify_advise: str = ""
    reject_reason_url: str = ""
    # problem_detail / Videos 里更具体的逐P问题（含时间段）
    problem_details: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否已经是一个确定的好结果。"""
        return self.kind in ("published", "self_only")

    @property
    def pending(self) -> bool:
        return self.kind == "reviewing"

    def as_dict(self) -> dict:
        return asdict(self)


def classify_state(state: int | None) -> tuple[str, str]:
    """把 B 站稿件的 state 映射成 (kind, 中文说明)。"""
    if state is None:
        return "not_found", "没有在稿件列表里找到这个稿件"
    if state == 0:
        return "published", "已通过审核，公开可见"
    if state == -2:
        return "rejected", "审核退回，仅自己可见"
    if state in (-1, -30):
        return "reviewing", f"审核中（state={state}）"
    if state == -50:
        return "self_only", "仅自己可见（已通过审核，非公开）"
    if state in (-100, -101, -102):
        return "rejected", f"审核未通过或已下线（state={state}）"
    return "unknown", f"未知状态（state={state}）"


def find_bvid(text: str) -> str | None:
    match = BV_PATTERN.search(text or "")
    return match.group(0) if match else None


def _request_json(url: str, cookie: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://member.bilibili.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_archive_state(
    cookies: Path,
    bvid: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = 20.0,
) -> ReviewState:
    """在投稿账号的稿件列表里查找 bvid，返回审核状态。

    不抛异常；网络/接口错误会返回 kind="error"，方便轮询时继续等待。
    """
    bvid = bvid.strip()
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not BV_PATTERN.fullmatch(bvid):
        return ReviewState(bvid, None, "error", "BV 号格式不正确", checked_at=checked_at)

    try:
        cookie = cookie_string_from_json(cookies)
    except Exception as exc:  # ConfigError / OSError / JSON 解析失败
        return ReviewState(bvid, None, "error", f"读取 cookies 失败：{exc}", checked_at=checked_at)

    for page in range(1, max_pages + 1):
        params = (
            f"?pn={page}&ps={PAGE_SIZE}"
            "&status=is_pubing,pubed,not_pubed&interactive=1"
        )
        try:
            data = _request_json(ARCHIVE_LIST_URL + params, cookie, timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return ReviewState(
                bvid, None, "error", f"查询稿件列表失败：{exc}", checked_at=checked_at
            )

        if data.get("code") not in (0, None):
            return ReviewState(
                bvid,
                None,
                "error",
                f"接口返回 code={data.get('code')}：{data.get('message')}",
                checked_at=checked_at,
            )

        audits = (data.get("data") or {}).get("arc_audits") or []
        for item in audits:
            archive = item.get("Archive") or {}
            if archive.get("bvid") != bvid:
                continue
            state = archive.get("state")
            try:
                state = int(state)
            except (TypeError, ValueError):
                state = None
            kind, message = classify_state(state)

            problem_details = []
            for detail in item.get("problem_detail") or []:
                if not isinstance(detail, dict):
                    continue
                problem_details.append(
                    {
                        "type": str(detail.get("type") or ""),
                        "index": detail.get("index"),
                        "reason": str(detail.get("reject_reason") or "").strip(),
                        "advice": str(detail.get("modify_advise") or "").strip(),
                        "problem_title": str(
                            detail.get("problem_description_title") or ""
                        ).strip(),
                        "description": str(
                            detail.get("problem_description") or ""
                        ).strip(),
                        "violation_time": str(
                            detail.get("violation_time") or ""
                        ).strip(),
                        "violation_position": str(
                            detail.get("violation_position") or ""
                        ).strip(),
                    }
                )

            videos = []
            for video in item.get("Videos") or []:
                if not isinstance(video, dict):
                    continue
                videos.append(
                    {
                        "index": video.get("index"),
                        "title": str(video.get("title") or ""),
                        "reason": str(video.get("reject_reason") or "").strip(),
                        "advice": str(video.get("modify_advise") or "").strip(),
                    }
                )

            # 优先用 problem_detail 里带 P/时间段的详细原因
            chosen = next(
                (
                    detail
                    for detail in problem_details
                    if detail.get("reason")
                    and (
                        detail.get("type") == "video"
                        or "P" in detail["reason"]
                        or detail.get("violation_time")
                    )
                ),
                problem_details[0] if problem_details else None,
            )

            archive_reason = str(archive.get("reject_reason") or "").strip()
            reject_reason = (
                (chosen or {}).get("reason") or archive_reason
            )
            if kind == "rejected" and reject_reason:
                message = f"{message}：{reject_reason}"

            return ReviewState(
                bvid,
                state,
                kind,
                message,
                title=str(archive.get("title") or ""),
                checked_at=checked_at,
                state_desc=str(archive.get("state_desc") or ""),
                reject_reason=reject_reason,
                problem_title=(
                    (chosen or {}).get("problem_title")
                    or str(archive.get("problem_description_title") or "").strip()
                ),
                problem_description=(
                    (chosen or {}).get("description")
                    or str(archive.get("problem_description") or "").strip()
                ),
                modify_advise=(
                    (chosen or {}).get("advice")
                    or str(archive.get("modify_advise") or "").strip()
                ),
                reject_reason_url=str(archive.get("reject_reason_url") or "").strip(),
                problem_details=problem_details,
                videos=videos,
            )

        if len(audits) < PAGE_SIZE:
            break

    return ReviewState(
        bvid, None, "not_found", "稿件列表里没有找到（可能刚提交，稍后再查）", checked_at=checked_at
    )


def wait_for_review(
    cookies: Path,
    bvid: str,
    *,
    timeout: int = 7200,
    interval: int = 300,
    logger=None,
    on_update=None,
) -> ReviewState:
    """轮询直到审核出结果或超时。

    on_update(state) 每次查询后被调用，可用来写任务状态。
    """
    deadline = time.monotonic() + max(1, timeout)
    last_kind = ""
    result: ReviewState | None = None

    while True:
        result = fetch_archive_state(cookies, bvid)
        if logger is not None:
            if result.kind != last_kind:
                logger.info("审核状态：%s（%s）", result.message, result.bvid)
            else:
                logger.debug("审核状态未变化：%s", result.message)
        last_kind = result.kind
        if on_update is not None:
            on_update(result)

        if result.ok or result.kind == "rejected":
            return result
        if result.kind == "error" and not result.pending:
            # 网络错误也继续等，直到超时
            pass
        if time.monotonic() >= deadline:
            if logger is not None:
                logger.warning("等待审核超时（%s 秒）：%s", timeout, result.message)
            return result

        time.sleep(max(10, interval))


# ---------------------------------------------------------------------------
# 守护进程里自动刷新未出结果的稿件
# ---------------------------------------------------------------------------


def job_bvid(job) -> str | None:
    """从任务状态里取单场投稿 BV；老任务没有 bvid 字段时从 detail 里解析。"""
    upload = job.outputs.get("upload") or {}
    bvid = str(upload.get("bvid") or "").strip()
    if bvid:
        return bvid
    return find_bvid(str(upload.get("detail") or ""))


def job_append_vid(job) -> str | None:
    """月稿追加成功后需要跟踪的 BV。"""
    append = job.outputs.get("append") or {}
    if not (append.get("done") or append.get("created")):
        return None
    vid = str(append.get("vid") or "").strip()
    return vid or None


def job_cookies(config, job) -> Path | None:
    """按任务的主播找投稿 cookies。"""
    streamer = next(
        (
            item
            for item in config.enabled_streamers()
            if item.id == job.streamer_id or job.streamer in (item.id, item.name)
        ),
        None,
    )
    if streamer is None:
        return None
    return streamer.upload.cookies


# 月稿追加后，B 站可能延迟进入审核；0 状态要多观察一段时间
APPEND_REVIEW_GRACE = 86400


def _is_final_review(job, key: str, kind: str, now: float) -> bool:
    """判断某个跟踪目标是否已经不需要再查。"""
    if kind in ("rejected", "self_only"):
        return True
    if kind == "published":
        if key != "append_review":
            return True
        # 月稿追加：给 B 站 24 小时处理新分P
        until = float(job.outputs.get("append_review_until") or 0)
        return now >= until
    return False


def refresh_pending_reviews(config, logger=None, max_jobs: int = 0) -> int:
    """刷新还没出结果的单场投稿和月稿追加，返回检查次数。

    由 watcher 的独立线程每 10 分钟调用，不受长时间压制阻塞。
    """
    from .state import list_jobs, save_job

    jobs = list_jobs(config.general.data_dir)
    if max_jobs and max_jobs > 0:
        jobs = jobs[:max_jobs]

    updated = 0
    cache: dict[str, ReviewState] = {}
    now = time.time()

    for job in jobs:
        upload_bvid = job_bvid(job)
        append_bvid = job_append_vid(job)
        if not upload_bvid and not append_bvid:
            continue

        cookies = job_cookies(config, job)
        if cookies is None or not cookies.is_file():
            continue

        changed = False
        if append_bvid and "append_review_until" not in job.outputs:
            job.outputs["append_review_until"] = now + APPEND_REVIEW_GRACE
            changed = True

        for key, bvid in (("review", upload_bvid), ("append_review", append_bvid)):
            if not bvid:
                continue
            previous = job.outputs.get(key) or {}
            old_kind = str(previous.get("kind") or "")
            if _is_final_review(job, key, old_kind, now):
                continue

            state = cache.get(bvid)
            if state is None:
                state = fetch_archive_state(cookies, bvid)
                cache[bvid] = state
            if state.kind == "error":
                # 网络/接口错误不写状态，下一轮再试
                continue

            job.outputs[key] = state.as_dict()
            changed = True
            updated += 1
            if logger is not None and state.kind != old_kind:
                label = "单场" if key == "review" else "月稿"
                logger.info(
                    "%s审核状态：%s %s（%s）",
                    label,
                    state.bvid,
                    state.message,
                    state.title or "-",
                )

        if changed:
            save_job(config.general.data_dir, job)

    return updated
