"""内置 ASR HTTP 服务（faster-whisper）。

镜像里通过 `INSTALL_ASR=true` 安装 faster-whisper 后，可以用这个服务
在本机完成语音转文本，协议与外部 Whisper 服务完全一致：

    POST /submit_task           {"audio_path": "...", "device": "auto"}
    GET  /task_status/{task_id}
    GET  /task_file/{task_id}
    GET  /status

设计：
    - 单工作线程 + 队列，避免同时加载多份模型；
    - 模型第一次收到任务时才加载/下载，启动更快；
    - 结果写到音频旁边：同名 .txt 和 .srt；
    - 绑定 127.0.0.1，默认只给本机 stream-archive 使用。
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGGER = logging.getLogger("stream_archive.asr_server")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_settings(config_path: Path) -> dict:
    """从 config.toml 的 [asr] 读取服务参数。"""
    defaults = {
        "host": "127.0.0.1",
        "port": 8286,
        "model": "large-v3-turbo",
        "language": "zh",
        "compute_type": "auto",
        "model_dir": "/data/asr/models",
        "device": "auto",
        "beam_size": 5,
        "hf_endpoint": "",
    }
    if not config_path.is_file():
        return defaults

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    asr = data.get("asr", {})
    if not isinstance(asr, dict):
        return defaults

    settings = dict(defaults)
    for key in defaults:
        if key in asr:
            settings[key] = asr[key]
    settings["port"] = int(settings["port"])
    settings["beam_size"] = int(settings["beam_size"])
    return settings


# ---------------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------------


class TaskStore:
    """内存任务表 + 先进先出队列。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tasks: dict[str, dict] = {}
        self.queue: queue.Queue[str] = queue.Queue()

    def create(self, audio_path: str, device: str) -> str:
        task_id = uuid.uuid4().hex[:10]
        with self.lock:
            # 内存里最多保留 200 个任务，优先清理已结束的旧任务
            if len(self.tasks) >= 200:
                for old_id in list(self.tasks):
                    if len(self.tasks) < 200:
                        break
                    if self.tasks[old_id]["status"] in ("done", "error", "cancelled"):
                        del self.tasks[old_id]
            self.tasks[task_id] = {
                "task_id": task_id,
                "audio_path": audio_path,
                "filename": Path(audio_path).name,
                "status": "queued",
                "progress": 0,
                "result": {},
                "error_msg": "",
                "requested_device": device,
            }
        self.queue.put(task_id)
        return task_id

    def update(self, task_id: str, **fields) -> None:
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id].update(fields)

    def get(self, task_id: str) -> dict | None:
        with self.lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None

    def size(self) -> int:
        return self.queue.qsize()


# ---------------------------------------------------------------------------
# 转写 worker
# ---------------------------------------------------------------------------


def _load_model(settings: dict):
    """加载 faster-whisper 模型。

    device=auto 时优先 CUDA；compute_type=auto 时 CUDA 用 float16、CPU 用 int8。
    """
    # HuggingFace 镜像：必须在导入 huggingface_hub 之前设置
    endpoint = str(settings.get("hf_endpoint", "")).strip()
    if endpoint:
        import os

        os.environ["HF_ENDPOINT"] = endpoint

    from faster_whisper import WhisperModel  # 延迟导入，未安装时服务仍可启动

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
        "加载模型 %s（device=%s, compute_type=%s, download_root=%s）",
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


def _format_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_outputs(audio_path: Path, segments: list) -> tuple[Path, Path]:
    """写同名 txt / srt。"""
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
            f"{_format_srt_time(float(segment.start))} --> "
            f"{_format_srt_time(float(segment.end))}"
        )
        lines.append(text)
        lines.append("")

    txt_path.write_text("\n".join(texts) + ("\n" if texts else ""), encoding="utf-8")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return txt_path, srt_path


def transcribe_task(model, task: dict, store: TaskStore, settings: dict) -> None:
    """执行一次转写，把进度和结果写回任务表。"""
    audio_path = Path(task["audio_path"])
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
    for segment in segments_iterator:
        segments.append(segment)
        if duration > 0:
            progress = min(99, max(0, int(float(segment.end) / duration * 100)))
            store.update(task["task_id"], progress=progress)

    txt_path, srt_path = _write_outputs(audio_path, segments)
    store.update(
        task["task_id"],
        status="done",
        progress=100,
        result={
            "time_cost": round(time.monotonic() - started, 2),
            "txt_file": str(txt_path),
            "srt_file": str(srt_path),
        },
    )
    LOGGER.info("转写完成：%s", audio_path.name)


class Worker(threading.Thread):
    """单线程消费队列，模型只加载一次。"""

    def __init__(self, store: TaskStore, settings: dict) -> None:
        super().__init__(daemon=True, name="asr-worker")
        self.store = store
        self.settings = settings
        self.model = None

    def run(self) -> None:
        while True:
            task_id = self.store.queue.get()
            task = self.store.get(task_id)
            if not task:
                self.store.queue.task_done()
                continue

            try:
                self.store.update(task_id, status="running", progress=0)
                if self.model is None:
                    self.model = _load_model(self.settings)
                transcribe_task(self.model, task, self.store, self.settings)
            except Exception as exc:  # 单任务失败不能杀死 worker
                LOGGER.exception("转写任务 %s 失败", task_id)
                self.store.update(task_id, status="error", error_msg=str(exc))
            finally:
                self.store.queue.task_done()


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------


class AsrHandler(BaseHTTPRequestHandler):
    store: TaskStore
    settings: dict

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        if self.path != "/submit_task":
            self._json({"error": "not found"}, status=404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "invalid json"}, status=400)
            return

        audio_path = str(payload.get("audio_path", "")).strip()
        if not audio_path or not Path(audio_path).is_file():
            self._json({"error": f"audio_path 不存在：{audio_path}"}, status=400)
            return

        task_id = self.store.create(audio_path, str(payload.get("device", "")))
        LOGGER.info("收到任务 %s：%s", task_id, audio_path)
        self._json({"task_id": task_id, "message": "任务已加入队列"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            self._json(
                {
                    "status": "ok",
                    "model": self.settings["model"],
                    "queue": self.store.size(),
                }
            )
            return

        if self.path.startswith("/task_status/"):
            task_id = self.path.rsplit("/", 1)[-1]
            task = self.store.get(task_id)
            self._json(task or {"error": "task not found"}, status=200 if task else 404)
            return

        if self.path.startswith("/task_file/"):
            task_id = self.path.rsplit("/", 1)[-1]
            task = self.store.get(task_id)
            if not task or task.get("status") != "done":
                self._json({"error": "task not done"}, status=404)
                return
            txt_path = Path((task.get("result") or {}).get("txt_file", ""))
            if not txt_path.is_file():
                self._json({"error": "txt not found"}, status=404)
                return
            self._json(
                {
                    "filename": txt_path.name,
                    "content": txt_path.read_text(encoding="utf-8"),
                }
            )
            return

        self._json({"error": "not found"}, status=404)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        LOGGER.debug("%s", args)


class AsrHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stream-archive-asr", description="内置语音转文本服务"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/data/config.toml"),
        help="配置文件路径",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    settings = load_settings(args.config)
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        LOGGER.warning(
            "未安装 faster-whisper；构建镜像时把 install/app.conf 的 INSTALL_ASR 设为 true"
        )

    store = TaskStore()
    Worker(store, settings).start()

    AsrHandler.store = store
    AsrHandler.settings = settings

    server = AsrHttpServer((str(settings["host"]), int(settings["port"])), AsrHandler)
    LOGGER.info(
        "ASR 服务已启动：http://%s:%s（模型 %s，目录 %s）",
        settings["host"],
        settings["port"],
        settings["model"],
        settings["model_dir"],
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("收到中断，退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
