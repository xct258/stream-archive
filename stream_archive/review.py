"""B 站投稿审核状态检查。

biliup 输出里的“投稿成功”只代表接口接单，稿件还要过 B 站审核。
这个模块用投稿账号的 cookies 查询稿件列表里的 state，判断：

    0          已通过审核，公开
    -1/-30     审核中
    -2         审核退回，仅自己可见
    -50        仅自己可见
    -100 等    未通过/已下线

为了减少 API 调用：
    - 自动跟踪按 BV 批量翻页，一次请求覆盖多个稿件；
    - 每个稿件有独立的退避时间（10 分钟 -> 30 分钟 -> 1 小时 -> 2 小时 -> 4 小时）；
    - 单场只跟踪 24 小时，月稿追加观察 48 小时；
    - 同一轮里相同 BV 只查一次；
    - 已公开/仅自见/已退回的稿件不再查询。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .recorder import cookie_string_from_json

ARCHIVE_LIST_URL = "https://member.bilibili.com/x/web/archives"

# 手动查询时最多翻 30 页（1500 个稿件）
DEFAULT_MAX_PAGES = 30
# 自动跟踪每轮最多翻 3 页（150 个稿件），通常第 1 页就能找到新投稿
REVIEW_PAGE_LIMIT = 3

PAGE_SIZE = 50
BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")

FINAL_KINDS = {"published", "self_only", "rejected"}

# 自动检查的退避间隔（秒）：10 分钟、30 分钟、1 小时、2 小时、4 小时
REVIEW_DELAYS = (600, 1800, 3600, 7200, 14400)
REVIEW_MAX_HOURS = 24
APPEND_REVIEW_MAX_HOURS = 48
APPEND_REVIEW_GRACE = 86400
# 任务创建超过这么久还没有审核记录，就不再自动跟踪，避免翻旧账
JOB_TRACK_MAX_AGE_HOURS = 72


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


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def _archive_page(cookie: str, page: int, timeout: float) -> list[dict]:
    """请求账号稿件列表的一页，返回 arc_audits。"""
    params = (
        f"?pn={page}&ps={PAGE_SIZE}"
        "&status=is_pubing,pubed,not_pubed&interactive=1"
    )
    data = _request_json(ARCHIVE_LIST_URL + params, cookie, timeout)
    if data.get("code") not in (0, None):
        raise RuntimeError(
            f"接口返回 code={data.get('code')}：{data.get('message')}"
        )
    return (data.get("data") or {}).get("arc_audits") or []


def _state_from_item(item: dict, bvid: str, checked_at: str) -> ReviewState:
    """把一条稿件列表记录转成 ReviewState，包含逐P退回原因。"""
    archive = item.get("Archive") or {}
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
    reject_reason = (chosen or {}).get("reason") or archive_reason
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


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def fetch_archive_state(
    cookies: Path,
    bvid: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = 20.0,
) -> ReviewState:
    """手动查询单个 BV（会翻更多页，适合查历史稿件）。

    不抛异常；网络/接口错误会返回 kind="error"。
    """
    bvid = bvid.strip()
    checked_at = _now_text()
    if not BV_PATTERN.fullmatch(bvid):
        return ReviewState(bvid, None, "error", "BV 号格式不正确", checked_at=checked_at)

    try:
        cookie = cookie_string_from_json(cookies)
    except Exception as exc:
        return ReviewState(
            bvid, None, "error", f"读取 cookies 失败：{exc}", checked_at=checked_at
        )

    for page in range(1, max_pages + 1):
        try:
            audits = _archive_page(cookie, page, timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            return ReviewState(
                bvid, None, "error", f"查询稿件列表失败：{exc}", checked_at=checked_at
            )

        for item in audits:
            archive = item.get("Archive") or {}
            if archive.get("bvid") == bvid:
                return _state_from_item(item, bvid, checked_at)

        if len(audits) < PAGE_SIZE:
            break

    return ReviewState(
        bvid,
        None,
        "not_found",
        "稿件列表里没有找到（可能刚提交，稍后再查）",
        checked_at=checked_at,
    )


def fetch_states_for_bvids(
    cookies: Path,
    bvids: set[str],
    *,
    max_pages: int = REVIEW_PAGE_LIMIT,
    timeout: float = 20.0,
) -> dict[str, ReviewState]:
    """一次翻页查询多个 BV，返回找到的 bvid -> ReviewState。

    自动跟踪用这个函数：同一轮里所有待查 BV 共享最多 3 次列表请求。
    网络/接口错误会抛出，由调用方统一退避。
    """
    remaining = {bvid.strip() for bvid in bvids if bvid}
    if not remaining:
        return {}

    cookie = cookie_string_from_json(cookies)
    checked_at = _now_text()
    found: dict[str, ReviewState] = {}

    for page in range(1, max_pages + 1):
        audits = _archive_page(cookie, page, timeout)
        for item in audits:
            archive = item.get("Archive") or {}
            bvid = str(archive.get("bvid") or "")
            if bvid in remaining:
                found[bvid] = _state_from_item(item, bvid, checked_at)
                remaining.discard(bvid)
        if not remaining or len(audits) < PAGE_SIZE:
            break

    return found


def wait_for_review(
    cookies: Path,
    bvid: str,
    *,
    timeout: int = 7200,
    interval: int = 300,
    logger=None,
    on_update=None,
) -> ReviewState:
    """轮询直到审核出结果或超时。"""
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
        if time.monotonic() >= deadline:
            if logger is not None:
                logger.warning("等待审核超时（%s 秒）：%s", timeout, result.message)
            return result

        time.sleep(max(10, interval))


# ---------------------------------------------------------------------------
# 自动跟踪
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


def _job_age_hours(job) -> float | None:
    try:
        created = datetime.strptime(job.created_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (datetime.now() - created).total_seconds() / 3600


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


def _schedule(job, key: str, attempts: int, now: float) -> None:
    delay = REVIEW_DELAYS[min(attempts, len(REVIEW_DELAYS) - 1)]
    job.outputs[f"{key}_next_at"] = now + delay
    job.outputs[f"{key}_attempts"] = attempts + 1
    job.outputs[f"{key}_last_check"] = now


def _targets(job) -> list[tuple[str, str]]:
    result = []
    bvid = job_bvid(job)
    if bvid:
        result.append(("review", bvid))
    vid = job_append_vid(job)
    if vid:
        result.append(("append_review", vid))
    repair = job.outputs.get("repair") or {}
    repair_bvid = str(repair.get("bvid") or "").strip()
    if repair_bvid and repair.get("status") == "uploaded":
        result.append(("repair_review", repair_bvid))
    return result


REVIEW_KEY_PREFIXES = ("review", "append_review", "repair_review")


def _save_review_updates(config, job) -> None:
    """只把审核相关字段合并回最新状态，避免覆盖 repair/步骤字段。"""
    from .state import JOB_LOCK, job_file, load_job, save_job

    with JOB_LOCK:
        try:
            latest = load_job(job_file(config.general.data_dir, job.id))
        except (OSError, ValueError, KeyError):
            latest = job
        for key, value in job.outputs.items():
            if key.startswith(REVIEW_KEY_PREFIXES):
                latest.outputs[key] = value
        save_job(config.general.data_dir, latest)


def refresh_pending_reviews(config, logger=None, max_jobs: int = 0) -> int:
    """刷新到期的审核状态，返回本轮检查的目标数。

    每轮最多对每个账号发 3 次列表请求，且只检查到了退避时间的 BV。
    """
    from .state import list_jobs

    jobs = list_jobs(config.general.data_dir)
    if max_jobs and max_jobs > 0:
        jobs = jobs[:max_jobs]

    now = time.time()
    due: list[tuple[object, str, str, Path]] = []

    for job in jobs:
        targets = _targets(job)
        if not targets:
            continue

        cookies = job_cookies(config, job)
        if cookies is None or not cookies.is_file():
            continue

        changed = False
        age = _job_age_hours(job)
        if age is not None and age > JOB_TRACK_MAX_AGE_HOURS:
            # 老任务不自动翻旧账；手动 review 仍可查
            continue

        for key, bvid in targets:
            previous = job.outputs.get(key) or {}
            kind = str(previous.get("kind") or "")
            if _is_final_review(job, key, kind, now):
                continue

            if f"{key}_first_at" not in job.outputs:
                job.outputs[f"{key}_first_at"] = now
                job.outputs[f"{key}_attempts"] = 0
                job.outputs[f"{key}_next_at"] = now
                changed = True

            if key == "append_review" and "append_review_until" not in job.outputs:
                append = job.outputs.get("append") or {}
                finished = float(append.get("finished_at") or 0)
                # 新任务按追加完成时间 + 24h；老任务没记录时间就只补查一次
                job.outputs["append_review_until"] = (
                    finished + APPEND_REVIEW_GRACE if finished else now
                )
                changed = True

            first_at = float(job.outputs.get(f"{key}_first_at") or now)
            max_hours = (
                REVIEW_MAX_HOURS if key == "review" else APPEND_REVIEW_MAX_HOURS
            )
            if now - first_at > max_hours * 3600:
                job.outputs[f"{key}_stopped_at"] = now
                changed = True
                continue

            next_at = float(job.outputs.get(f"{key}_next_at") or 0)
            if now < next_at:
                continue

            due.append((job, key, bvid, cookies))

        if changed:
            _save_review_updates(config, job)

    if not due:
        return 0

    # 同一个 cookies 的 BV 合并成一次翻页
    groups: dict[Path, list[tuple[object, str, str, Path]]] = defaultdict(list)
    for item in due:
        groups[item[3]].append(item)

    checked = 0
    for cookies, items in groups.items():
        bvids = {bvid for _, _, bvid, _ in items}
        try:
            states = fetch_states_for_bvids(cookies, bvids)
        except Exception as exc:  # 网络/风控：统一延后 30 分钟
            if logger is not None:
                logger.warning("查询审核状态失败，30 分钟后重试：%s", exc)
            for job, key, _bvid, _cookies in items:
                attempts = int(job.outputs.get(f"{key}_attempts") or 0)
                job.outputs[f"{key}_next_at"] = time.time() + 1800
                job.outputs[f"{key}_attempts"] = attempts + 1
                _save_review_updates(config, job)
            continue

        for job, key, bvid, _cookies in items:
            previous = job.outputs.get(key) or {}
            old_kind = str(previous.get("kind") or "")
            attempts = int(job.outputs.get(f"{key}_attempts") or 0)
            checked += 1

            state = states.get(bvid)
            if state is not None:
                job.outputs[key] = state.as_dict()
                if logger is not None and state.kind != old_kind:
                    label = {
                    "review": "单场",
                    "append_review": "月稿",
                    "repair_review": "修复版",
                }.get(key, key)
                    logger.info(
                        "%s审核状态：%s %s（%s）",
                        label,
                        state.bvid,
                        state.message,
                        state.title or "-",
                    )
            elif not previous:
                # 刚提交还没出现在列表里，先记一个“待审核”
                job.outputs[key] = ReviewState(
                    bvid,
                    None,
                    "not_found",
                    "稿件列表里还没找到（可能刚提交）",
                    checked_at=_now_text(),
                ).as_dict()

            if state is not None and _is_final_review(job, key, state.kind, now):
                job.outputs[f"{key}_next_at"] = 0
            else:
                _schedule(job, key, attempts, now)

            _save_review_updates(config, job)

    return checked
