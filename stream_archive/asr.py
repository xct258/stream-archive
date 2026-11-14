"""语音转文本（本地 ASR）。

流程：
    1. 从归档的原视频里抽一条 16kHz 单声道 m4a 音轨；
    2. 启动一次性 worker 子进程，加载 faster-whisper 并转写；
    3. worker 把 txt / srt 写在音频旁边，然后退出；
    4. 模型随进程退出释放，不会常驻占用内存/显存。

不再需要 HTTP 服务、任务队列和轮询。镜像构建时 `INSTALL_ASR=true`
会安装 faster-whisper；运行时 `[asr].enabled=true` 即可。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from .config import AsrConfig, Config, StreamerConfig
from .log import get_logger
from .media import FFMPEG
from .state import Job, save_job
from .util import human_size, run


class AsrError(RuntimeError):
    """音频提取或本地转写失败。"""


# ---------------------------------------------------------------------------
# 音频提取
# ---------------------------------------------------------------------------


def extract_audio(video: Path, output: Path) -> Path:
    """抽 16kHz 单声道 AAC 音轨，体积约为 4 小时 115MB。

    16k 单声道是 Whisper 系列模型的原生输入规格，worker 不用再重采样。
    """
    logger = get_logger("stream_archive.asr")
    logger.info("提取音频：%s", video.name)

    run(
        [
            FFMPEG,
            "-nostdin",
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
# 一次性 worker
# ---------------------------------------------------------------------------


def build_worker_command(config: AsrConfig, audios: list[Path]) -> list[str]:
    """组装 worker 命令。参数直接传，worker 不依赖 config 文件。"""
    command = [
        sys.executable,
        "-m", "stream_archive.asr_worker",
        "--model", config.model,
        "--language", config.language,
        "--device", config.device,
        "--compute-type", config.compute_type,
        "--model-dir", str(config.model_dir),
        "--beam-size", str(config.beam_size),
    ]
    if config.hf_endpoint.strip():
        command += ["--hf-endpoint", config.hf_endpoint.strip()]
    for audio in audios:
        command += ["--audio", str(audio)]
    return command


def run_local_asr(config: AsrConfig, audios: list[Path], logger=None) -> None:
    """启动 worker，转写全部音频；worker 退出后模型自动释放。"""
    logger = logger or get_logger("stream_archive.asr")
    if not audios:
        return

    command = build_worker_command(config, audios)
    logger.info("启动本地 ASR worker：%d 个音频", len(audios))
    logger.debug("ASR 命令：%s", " ".join(command))

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # 后台读日志，主线程负责超时控制
    def pump_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                logger.info("asr: %s", line)

    reader = threading.Thread(target=pump_output, daemon=True)
    reader.start()

    try:
        return_code = process.wait(timeout=config.timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise AsrError(f"本地 ASR 超时（{config.timeout}s），已终止 worker")

    reader.join(timeout=5)
    if process.stdout:
        process.stdout.close()

    if return_code != 0:
        raise AsrError(f"本地 ASR worker 退出码 {return_code}")

    # worker 正常退出，模型内存/显存已经随进程释放
    logger.info("本地 ASR worker 已退出，模型已释放")

    missing = [
        str(audio)
        for audio in audios
        if not audio.with_suffix(".txt").is_file()
        and not audio.with_suffix(".srt").is_file()
    ]
    if missing:
        raise AsrError("ASR 完成但没有生成 txt/srt：" + "；".join(missing))


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

    audios: list[Path] = []
    for video in pending:
        audio_path = raw_dir / f"{video.stem}.m4a"
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            extract_audio(video, audio_path)
        audios.append(audio_path)

    run_local_asr(asr, audios, logger)

    for video, audio in zip(pending, audios):
        txt_path = audio.with_suffix(".txt")
        srt_path = audio.with_suffix(".srt")
        state["done"].append(video.name)
        state["files"].append(
            {
                "video": video.name,
                "txt": str(txt_path) if txt_path.is_file() else "",
                "srt": str(srt_path) if srt_path.is_file() else "",
            }
        )
        logger.info(
            "ASR 完成：%s（txt=%s, srt=%s）",
            video.name,
            txt_path.name if txt_path.is_file() else "-",
            srt_path.name if srt_path.is_file() else "-",
        )

    save_job(config.general.data_dir, job)
    return f"转写 {len(pending)} 个视频（txt/srt）", False
