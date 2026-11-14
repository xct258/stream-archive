"""内置 ASR 服务测试：任务队列、HTTP 接口、txt/srt 输出。

用假的 faster_whisper 模块，不下载模型、不做真实推理。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from stream_archive.asr_server import (
    AsrHandler,
    AsrHttpServer,
    TaskStore,
    Worker,
    _format_srt_time,
    _write_outputs,
    load_settings,
)


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeInfo:
    duration = 10.0


class FakeWhisperModel:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def transcribe(self, audio_path, **kwargs):
        return iter(
            [FakeSegment(0.0, 2.0, "你好"), FakeSegment(2.0, 5.0, "世界")]
        ), FakeInfo()


def fake_module() -> types.ModuleType:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    return module


def http_json(url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


class AsrServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.audio = self.root / "demo.m4a"
        self.audio.write_bytes(b"fake audio")

        self.store = TaskStore()
        self.settings = {
            "host": "127.0.0.1",
            "port": 0,
            "model": "fake",
            "language": "zh",
            "compute_type": "int8",
            "model_dir": str(self.root / "models"),
            "device": "cpu",
            "beam_size": 5,
        }

        self.patch = patch.dict(sys.modules, {"faster_whisper": fake_module()})
        self.patch.start()

        Worker(self.store, self.settings).start()
        AsrHandler.store = self.store
        AsrHandler.settings = self.settings

        self.server = AsrHttpServer(("127.0.0.1", 0), AsrHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_format_srt_time(self) -> None:
        self.assertEqual(_format_srt_time(0), "00:00:00,000")
        self.assertEqual(_format_srt_time(3725.123), "01:02:05,123")

    def test_write_outputs(self) -> None:
        txt, srt = _write_outputs(
            self.audio, [FakeSegment(0.0, 2.0, "你好"), FakeSegment(2.0, 5.0, "世界")]
        )
        self.assertEqual(txt.read_text(encoding="utf-8"), "你好\n世界\n")
        srt_text = srt.read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:02,000", srt_text)
        self.assertIn("世界", srt_text)

    def test_submit_and_poll(self) -> None:
        submitted = http_json(
            f"{self.base}/submit_task",
            {"audio_path": str(self.audio), "device": "cpu"},
        )
        task_id = submitted["task_id"]

        deadline = time.monotonic() + 5
        task = {}
        while time.monotonic() < deadline:
            task = http_json(f"{self.base}/task_status/{task_id}")
            if task.get("status") == "done":
                break
            time.sleep(0.05)

        self.assertEqual(task.get("status"), "done")
        self.assertEqual(task.get("progress"), 100)
        self.assertTrue((self.root / "demo.txt").is_file())
        self.assertTrue((self.root / "demo.srt").is_file())

        content = http_json(f"{self.base}/task_file/{task_id}")
        self.assertEqual(content["content"], "你好\n世界\n")

        status = http_json(f"{self.base}/status")
        self.assertEqual(status["status"], "ok")

    def test_submit_missing_audio(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            http_json(
                f"{self.base}/submit_task",
                {"audio_path": str(self.root / "missing.m4a")},
            )
        self.assertEqual(context.exception.code, 400)

    def test_load_settings(self) -> None:
        config = self.root / "config.toml"
        config.write_text(
            """
[asr]
enabled = true
embedded = true
model = "small"
language = "zh"
port = 9999
""",
            encoding="utf-8",
        )
        settings = load_settings(config)
        self.assertEqual(settings["model"], "small")
        self.assertEqual(settings["port"], 9999)
        # 未写的字段使用默认值
        self.assertEqual(settings["host"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
