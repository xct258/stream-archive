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
from dataclasses import asdict, dataclass
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
            reject_reason = str(archive.get("reject_reason") or "").strip()
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
                problem_title=str(
                    archive.get("problem_description_title") or ""
                ).strip(),
                problem_description=str(
                    archive.get("problem_description") or ""
                ).strip(),
                modify_advise=str(archive.get("modify_advise") or "").strip(),
                reject_reason_url=str(archive.get("reject_reason_url") or "").strip(),
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
    """从任务状态里取投稿 BV；老任务没有 bvid 字段时从 detail 里解析。"""
    upload = job.outputs.get("upload") or {}
    bvid = str(upload.get("bvid") or "").strip()
    if bvid:
        return bvid
    return find_bvid(str(upload.get("detail") or ""))


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


def refresh_pending_reviews(config, logger=None, max_jobs: int = 50) -> int:
    """刷新最近任务里还没出审核结果的稿件，返回检查数量。

    由 watcher 每 10 分钟调用一次；已通过/仅自己可见/未通过的任务不再重复查。
    """
    from .state import list_jobs, save_job

    updated = 0
    for job in list_jobs(config.general.data_dir)[:max_jobs]:
        bvid = job_bvid(job)
        if not bvid:
            continue

        review = job.outputs.get("review") or {}
        if review.get("kind") in FINAL_KINDS:
            continue

        cookies = job_cookies(config, job)
        if cookies is None or not cookies.is_file():
            continue

        state = fetch_archive_state(cookies, bvid)
        if state.kind == "error":
            # 网络/接口错误不写状态，下一轮再试
            continue

        previous = str(review.get("kind") or "")
        job.outputs["review"] = state.as_dict()
        save_job(config.general.data_dir, job)
        updated += 1
        if logger is not None and state.kind != previous:
            logger.info(
                "审核状态：%s %s（%s）",
                state.bvid,
                state.message,
                state.title or "-",
            )

    return updated
