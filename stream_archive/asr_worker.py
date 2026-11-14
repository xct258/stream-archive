"""一次性本地 ASR worker。

由 stream_archive.asr 在转写步骤启动：
    加载 faster-whisper -> 转写全部音频 -> 写 txt/srt -> 退出。

进程退出后模型占用的内存/显存由系统回收，不需要常驻 HTTP 服务。
也可以手动运行：

    stream-archive-asr --audio /data/videos/a.m4a --model small
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import time
from pathlib import Path

LOGGER = logging.getLogger("stream_archive.asr_worker")


def _patch_av_compat() -> None:
    """兼容 av 19。

    faster-whisper 1.2.1 调用 av.open(..., metadata_errors="ignore")，
    而 av 19 移除了这个参数。等同于“忽略元数据错误”，直接丢掉即可。
    """
    try:
        import av
    except Exception:
        return

    original = av.open
    if getattr(original, "_stream_archive_compat", False):
        return

    def open_compat(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return original(*args, **kwargs)

    open_compat._stream_archive_compat = True  # type: ignore[attr-defined]
    av.open = open_compat


def load_model(settings: dict):
    """加载 faster-whisper 模型。"""
    # HuggingFace 镜像必须在导入 huggingface_hub 之前设置
    endpoint = str(settings.get("hf_endpoint", "")).strip()
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint

    _patch_av_compat()
    from faster_whisper import WhisperModel

    device = str(settings["device"])
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"

    compute_type = str(settings["compute_type"])
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    model_dir = Path(settings["model_dir"])
    model_dir.mkdir(parents=True, exist_ok=True)

    LOGGER.info(
        "加载模型 %s（device=%s, compute_type=%s, 目录=%s）",
        settings["model"],
        device,
        compute_type,
        model_dir,
    )
    return WhisperModel(
        str(settings["model"]),
        device=device,
        compute_type=compute_type,
        download_root=str(model_dir),
    )


def format_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_outputs(audio_path: Path, segments: list) -> tuple[Path, Path]:
    """写同名 txt / srt；没有语音时写空文件，也是有效结果。"""
    txt_path = audio_path.with_suffix(".txt")
    srt_path = audio_path.with_suffix(".srt")

    texts = []
    lines = []
    for index, segment in enumerate(segments, start=1):
        text = str(getattr(segment, "text", "")).strip()
        if not text:
            continue
        texts.append(text)
        lines.append(str(index))
        lines.append(
            f"{format_srt_time(float(segment.start))} --> "
            f"{format_srt_time(float(segment.end))}"
        )
        lines.append(text)
        lines.append("")

    txt_path.write_text("\n".join(texts) + ("\n" if texts else ""), encoding="utf-8")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return txt_path, srt_path


def transcribe_file(model, audio_path: Path, settings: dict) -> None:
    """转写单个音频，打印进度并写结果。"""
    if not audio_path.is_file():
        raise FileNotFoundError(f"音频不存在：{audio_path}")

    started = time.monotonic()
    segments_iterator, info = model.transcribe(
        str(audio_path),
        language=str(settings["language"]) or None,
        beam_size=int(settings["beam_size"]),
        vad_filter=True,
    )

    duration = float(getattr(info, "duration", 0) or 0)
    segments = []
    last_progress = -1

    for segment in segments_iterator:
        segments.append(segment)
        if duration > 0:
            progress = min(99, max(0, int(float(segment.end) / duration * 100)))
            # 每 5% 打一行，避免刷屏
            if progress >= last_progress + 5:
                LOGGER.info("进度 %s%%", progress)
                last_progress = progress

    txt_path, srt_path = write_outputs(audio_path, segments)
    LOGGER.info(
        "完成：%s（耗时 %.1fs，txt=%s，srt=%s）",
        audio_path.name,
        time.monotonic() - started,
        txt_path.name,
        srt_path.name,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stream-archive-asr", description="一次性本地语音转文本 worker"
    )
    parser.add_argument(
        "--audio",
        type=Path,
        action="append",
        required=True,
        help="音频文件，可重复传入多个",
    )
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--model-dir", type=Path, default=Path("/data/asr/models"))
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--hf-endpoint", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    settings = {
        "model": args.model,
        "language": args.language,
        "device": args.device,
        "compute_type": args.compute_type,
        "model_dir": args.model_dir,
        "beam_size": args.beam_size,
        "hf_endpoint": args.hf_endpoint,
    }

    try:
        model = load_model(settings)
    except Exception as exc:
        LOGGER.error(
            "加载 faster-whisper 失败：%s（构建镜像时把 install/app.conf 的 INSTALL_ASR 设为 true）",
            exc,
        )
        return 2

    exit_code = 0
    for audio in args.audio:
        LOGGER.info("开始转写：%s", audio)
        try:
            transcribe_file(model, audio, settings)
        except Exception as exc:
            LOGGER.exception("转写失败：%s", audio)
            exit_code = 1

    # 显式释放；进程随后退出，内存/显存全部归还
    del model
    gc.collect()
    LOGGER.info("worker 结束（exit=%s）", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
