"""WebDAV 弹幕上传。

用途：
    把录播姬生成的 XML 弹幕文件上传到网盘，按“主播/年/月/日”归档，
    方便观众对照录播下载弹幕。

为什么不用 rclone：
    旧版依赖 rclone.conf 里的 webdav remote，部署时还要额外维护一份配置。
    这里直接用标准库发 HTTP PUT/MKCOL，用户名、密码、路径都写在 config.toml，
    不需要安装 rclone，也不需要在容器里挂载 rclone.conf。

兼容性：
    使用 WebDAV 标准的 Basic Auth + MKCOL/PUT，适用于 OpenList/Alist、
    Nextcloud、群晖等常见 WebDAV 服务。Digest 认证和分块上传不支持。
"""

from __future__ import annotations

import base64
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config, StreamerConfig, WebdavConfig
from .log import get_logger
from .media import parse_recording_name
from .state import Job, date_parts, save_job


class WebdavError(RuntimeError):
    """WebDAV 请求失败（网络、认证、权限或路径不存在）。"""


# ---------------------------------------------------------------------------
# 路径模板
# ---------------------------------------------------------------------------


def _template_values(streamer: str, start_time: str) -> dict[str, str]:
    """把开播时间拆成路径模板需要的几个变量。"""
    _, year, month, full_date = date_parts(start_time)
    day = full_date.rsplit("-", 1)[-1]
    return {
        "{streamer}": streamer,
        "{yyyy}": year,
        "{mm}": month,
        "{dd}": day,
        "{month}": f"{year}-{month}",
        "{date}": full_date,
    }


def render_path(template: str, streamer: str, start_time: str) -> str:
    """渲染目录模板，返回不带首尾斜杠的相对路径。

    示例：
        /直播回放弹幕/{streamer}/{yyyy}/{mm}/{date}
        -> 直播回放弹幕/主播A/2026/11/2026-11-14
    """
    result = template
    for placeholder, value in _template_values(streamer, start_time).items():
        result = result.replace(placeholder, value)
    return result.strip("/")


def month_public_url(dav: WebdavConfig, streamer: str, start_time: str) -> str:
    """月稿件的对外访问地址（到月份一级），用于简介里的 {pan_url}。"""
    if not dav.enabled or not dav.public_url.strip():
        return ""
    _, year, month, _ = date_parts(start_time)
    base = dav.public_url.rstrip("/")
    return f"{base}/{streamer}/{year}/{month}"


# ---------------------------------------------------------------------------
# HTTP 请求
# ---------------------------------------------------------------------------


def _quote_segment(segment: str) -> str:
    """对单个路径段做 URL 编码，保留中文可读性交给服务端解码。"""
    return urllib.parse.quote(segment, safe="")


def _quote_path(path: str) -> str:
    return "/".join(_quote_segment(part) for part in path.split("/") if part)


def _request(
    method: str,
    url: str,
    dav: WebdavConfig,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    """发一个 WebDAV 请求，返回 HTTP 状态码；网络错误抛 WebdavError。"""
    request_headers = dict(headers or {})

    if dav.username or dav.password:
        token = base64.b64encode(
            f"{dav.username}:{dav.password}".encode("utf-8")
        ).decode("ascii")
        request_headers["Authorization"] = f"Basic {token}"

    request = urllib.request.Request(
        url, data=data, method=method, headers=request_headers
    )

    # 自签名证书场景：显式关闭校验（配置里要自己承担风险）
    context = ssl._create_unverified_context() if dav.insecure else None

    try:
        with urllib.request.urlopen(
            request, timeout=dav.timeout, context=context
        ) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise WebdavError(f"{method} {url} 失败：{exc}") from exc


def upload_file(dav: WebdavConfig, remote_path: str, local_file: Path) -> None:
    """上传单个文件。

    步骤：
        1. 逐级 MKCOL 父目录（已存在时服务端通常返回 405，视为成功）；
        2. PUT 文件内容（覆盖同名文件）。
    """
    base = dav.url.rstrip("/")
    segments = [part for part in remote_path.split("/") if part]
    if not segments:
        raise WebdavError("WebDAV 目标路径为空")

    # ---- 建目录 ----
    current = ""
    for segment in segments[:-1]:
        current += "/" + _quote_segment(segment)
        status = _request("MKCOL", base + current, dav)
        if status not in (200, 201, 204, 405):
            raise WebdavError(
                f"创建目录失败（HTTP {status}）：{base + current}"
            )

    # ---- 上传 ----
    destination = base + "/" + _quote_path(remote_path)
    status = _request(
        "PUT",
        destination,
        dav,
        data=local_file.read_bytes(),
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )
    if status not in (200, 201, 204):
        raise WebdavError(f"上传失败（HTTP {status}）：{destination}")


# ---------------------------------------------------------------------------
# 流水线步骤
# ---------------------------------------------------------------------------


def step_upload_danmaku(
    config: Config, streamer: StreamerConfig, job: Job, logger=None
) -> tuple[str, bool]:
    """把本场 XML 上传到 WebDAV；返回 (说明, 是否跳过)。"""
    logger = logger or get_logger("stream_archive.webdav")
    dav = streamer.webdav

    if not dav.enabled:
        return "未启用 WebDAV 弹幕上传", True

    xml_files = _xml_candidates(streamer, job)
    if not xml_files:
        return "没有需要上传的 XML", True

    # 已上传过的文件不再重复传（分P较多或中途失败重试时很有用）
    state = job.outputs.setdefault("webdav", {"uploaded": []})
    uploaded_before = set(state.get("uploaded", []))
    pending = [path for path in xml_files if path.name not in uploaded_before]
    if not pending:
        return f"本场 {len(xml_files)} 个 XML 已全部上传", True

    failures: list[str] = []
    for xml_path in pending:
        # 每段录播可能跨天，按文件自己的时间渲染目录，取不到再用本场开播时间
        start_time = _file_start_time(xml_path, job.start_time)
        directory = render_path(dav.path, streamer.name, start_time)
        remote_path = f"{directory}/{xml_path.name}"

        try:
            logger.info("上传弹幕：%s -> %s", xml_path.name, remote_path)
            upload_file(dav, remote_path, xml_path)
        except WebdavError as exc:
            logger.error("弹幕上传失败：%s（%s）", xml_path.name, exc)
            failures.append(f"{xml_path.name}: {exc}")
            continue

        state["uploaded"].append(xml_path.name)
        uploaded_before.add(xml_path.name)
        save_job(config.general.data_dir, job)

    uploaded = len(pending) - len(failures)
    if uploaded == 0:
        raise WebdavError("全部 XML 上传失败：" + "；".join(failures))

    detail = f"上传 {uploaded}/{len(xml_files)} 个 XML 到 WebDAV"
    if failures:
        detail += f"，失败 {len(failures)} 个"
    return detail, False


def _xml_candidates(streamer: StreamerConfig, job: Job) -> list[Path]:
    """找出本场需要上传的 XML。

    归档之后运行时从“原文件”目录取；归档之前运行时从缓存目录取。
    only_appended=true 时只传成功追加到月稿件的那些视频对应的 XML。
    """
    archive = job.outputs.get("archive") or {}
    raw_dir = Path(archive["raw_dir"]) if archive.get("raw_dir") else Path(job.work_dir)
    if not raw_dir.is_dir():
        return []

    xml_files = sorted(raw_dir.glob("*.xml"))

    if streamer.webdav.only_appended:
        # append.done 里是视频文件名，投稿版需要去掉前缀才能对上 XML 的文件名
        done_stems = {
            Path(name).stem.removeprefix("投稿版-")
            for name in job.outputs.get("append", {}).get("done", [])
        }
        xml_files = [path for path in xml_files if path.stem in done_stems]

    return xml_files


def _file_start_time(xml_path: Path, fallback: str) -> str:
    """从 XML 文件名里取开播时间；失败时用本场时间兜底。"""
    try:
        name = parse_recording_name(xml_path.name)
    except ValueError:
        return fallback
    return name.start_time or fallback
