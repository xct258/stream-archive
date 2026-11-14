"""语音转文本（ASR）。

流程：
    1. 从归档的原视频里抽一条 16kHz 单声道 m4a 音轨；
    2. 把音频路径提交给 ASR 服务；
    3. 轮询任务状态，直到 done / error / 超时；
    4. 把服务生成的 txt / srt 留在音频旁边（也就是原文件目录）。

兼容协议（旧版 Whisper 服务）：
    POST /submit_task           {"audio_path": "...", "device": "auto"}
    GET  /task_status/{task_id}
    GET  /task_file/{task_id}   返回 {"filename", "content"}

服务端和本工具不在同一挂载时，用 path_map_from / path_map_to 做前缀替换。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import AsrConfig, Config, StreamerConfig
from .log import get_logger
from .media import FFMPEG
from .state import Job, save_job
from .util import human_size, run


class AsrError(RuntimeError):
    """提交、轮询或读取转写结果失败。"""


# ---------------------------------------------------------------------------
# 路径与 HTTP
# ---------------------------------------------------------------------------


def remote_path(audio_path: Path, config: AsrConfig) -> str:
    """把本地路径映射成服务端能访问的路径。

    没有配置映射时直接使用本地绝对路径（适合服务与本工具共享挂载的情况）。
    """
    text = str(audio_path)
    if not config.path_map_from:
        return text

    prefix = config.path_map_from.rstrip("/\\")
    if not text.startswith(prefix):
        raise AsrError(
            f"音频路径不在 path_map_from 下：{text}（前缀 {config.path_map_from}）"
        )

    suffix = text[len(prefix) :].lstrip("/\\")
    # Windows 路径用反斜杠，Linux 用斜杠
    separator = "\\" if "\\" in config.path_map_to else "/"
    return config.path_map_to.rstrip("/\\") + separator + suffix.replace("/", separator)


def _request_json(
    url: str, payload: dict | None = None, api_key: str = "", timeout: int = 30
) -> dict:
    """发一个 JSON 请求并返回解析后的对象。"""
    data = None
    headers = {"Accept": "application/json", "User-Agent": "stream-archive"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise AsrError(f"{url} 返回 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AsrError(f"{url} 请求失败：{exc}") from exc

    try:
        value = json.loads(body)
    except ValueError as exc:
        raise AsrError(f"{url} 返回的不是 JSON：{body[:200]}") from exc
    if not isinstance(value, dict):
        raise AsrError(f"{url} 返回格式不正确：{body[:200]}")
    return value


# ---------------------------------------------------------------------------
# 音频提取
# ---------------------------------------------------------------------------


def extract_audio(video: Path, output: Path) -> Path:
    """抽 16kHz 单声道 AAC 音轨，体积约为 4 小时 115MB。

    16k 单声道是 Whisper 系列模型的原生输入规格，服务端不用再重采样。
    """
    logger = get_logger("stream_archive.asr")
    logger.info("提取音频：%s", video.name)

    run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(video),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "aac",
            "-b:a", "64k",
            str(output),
        ]
    )

    if not output.is_file() or output.stat().st_size == 0:
        raise AsrError(f"音频提取失败：{output}")
    logger.info("音频提取完成：%s（%s）", output.name, human_size(output.stat().st_size))
    return output


# ---------------------------------------------------------------------------
# 提交与轮询
# ---------------------------------------------------------------------------


def submit_and_wait(
    config: AsrConfig, audio_path: Path, logger=None
) -> tuple[str, dict]:
    """提交音频并等待任务完成，返回 (task_id, result)。"""
    logger = logger or get_logger("stream_archive.asr")
    server = config.server.rstrip("/")

    payload = {
        "audio_path": remote_path(audio_path, config),
        "device": config.device,
    }
    logger.info("提交 ASR 任务：%s -> %s", audio_path.name, payload["audio_path"])
    submitted = _request_json(
        f"{server}/submit_task", payload, config.api_key, timeout=60
    )
    task_id = submitted.get("task_id")
    if not task_id:
        raise AsrError(f"ASR 服务未返回 task_id：{submitted}")
    logger.info("ASR 任务已提交：%s", task_id)

    deadline = time.monotonic() + config.timeout
    last_status = ""
    last_progress = None

    while True:
        info = _request_json(
            f"{server}/task_status/{task_id}", api_key=config.api_key, timeout=60
        )
        status = str(info.get("status", ""))
        progress = info.get("progress")

        # 只在状态或进度变化时写日志，避免每 5 秒刷一行
        if status != last_status or progress != last_progress:
            suffix = f" {progress}%" if progress is not None else ""
            logger.info("ASR %s：%s%s", task_id, status or "未知", suffix)
            last_status = status
            last_progress = progress

        if status == "done":
            result = info.get("result") or {}
            if not isinstance(result, dict):
                result = {}
            return task_id, result
        if status in ("error", "cancelled"):
            message = info.get("error_msg") or info.get("message") or status
            raise AsrError(f"ASR 任务 {task_id} 失败：{message}")

        if time.monotonic() >= deadline:
            raise AsrError(
                f"ASR 任务 {task_id} 超时（{config.timeout}s，最后状态 {status}）"
            )
        time.sleep(config.poll_interval)


def collect_outputs(
    task_id: str, audio_path: Path, result: dict, config: AsrConfig, logger=None
) -> tuple[Path | None, Path | None]:
    """确认 txt/srt 落盘位置。

    优先用服务写在音频旁边的文件；txt 缺失时再调 /task_file 兜底。
    返回 (txt_path, srt_path)，至少有一个存在，否则抛异常。
    """
    logger = logger or get_logger("stream_archive.asr")
    txt_path = audio_path.with_suffix(".txt")
    srt_path = audio_path.with_suffix(".srt")

    # 服务端返回的路径如果本地可访问（共享挂载），复制到音频旁边
    for key, local_path in (("txt_file", txt_path), ("srt_file", srt_path)):
        if local_path.is_file() and local_path.stat().st_size > 0:
            continue
        remote_value = result.get(key)
        if remote_value:
            candidate = Path(str(remote_value))
            if candidate.is_file() and candidate.stat().st_size > 0:
                local_path.write_bytes(candidate.read_bytes())

    # txt 还没有：用 /task_file 接口取内容
    if not txt_path.is_file() or txt_path.stat().st_size == 0:
        try:
            payload = _request_json(
                f"{config.server.rstrip('/')}/task_file/{task_id}",
                api_key=config.api_key,
                timeout=60,
            )
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                txt_path.write_text(content, encoding="utf-8")
        except AsrError as exc:
            logger.warning("获取 ASR 文本失败：%s", exc)

    txt_ok = txt_path.is_file() and txt_path.stat().st_size > 0
    srt_ok = srt_path.is_file() and srt_path.stat().st_size > 0
    if not txt_ok and not srt_ok:
        raise AsrError(f"ASR 任务 {task_id} 完成，但没有找到 txt/srt 结果")

    return (
        txt_path if txt_ok else None,
        srt_path if srt_ok else None,
    )


# ---------------------------------------------------------------------------
# 流水线步骤
# ---------------------------------------------------------------------------


def step_transcribe(
    config: Config, streamer: StreamerConfig, job: Job, logger=None
) -> tuple[str, bool]:
    """对归档后的原视频做语音转文本。"""
    logger = logger or get_logger("stream_archive.asr")
    asr = streamer.asr

    if not asr.enabled:
        return "未启用语音转文本", True

    raw_dir = Path(
        (job.outputs.get("archive") or {}).get("raw_dir") or job.work_dir
    )
    videos = sorted(raw_dir.glob("*.mp4")) + sorted(raw_dir.glob("*.flv"))
    if not videos:
        return "没有可转写的视频", True

    state = job.outputs.setdefault("asr", {"done": [], "files": []})
    done = set(state.get("done", []))
    pending = [video for video in videos if video.name not in done]
    if not pending:
        return "本场视频已全部转写", True

    for video in pending:
        audio_path = raw_dir / f"{video.stem}.m4a"
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            extract_audio(video, audio_path)

        task_id, result = submit_and_wait(asr, audio_path, logger)
        txt_path, srt_path = collect_outputs(
            task_id, audio_path, result, asr, logger
        )

        state["done"].append(video.name)
        state["files"].append(
            {
                "video": video.name,
                "txt": str(txt_path) if txt_path else "",
                "srt": str(srt_path) if srt_path else "",
            }
        )
        save_job(config.general.data_dir, job)
        logger.info(
            "ASR 完成：%s（txt=%s, srt=%s）",
            video.name,
            txt_path.name if txt_path else "-",
            srt_path.name if srt_path else "-",
        )

    return f"转写 {len(pending)} 个视频（txt/srt）", False
