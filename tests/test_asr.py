"""语音转文本测试：任务提交、轮询、结果落盘、路径映射。

用本地 http.server 模拟旧版 Whisper 服务，不访问外网。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from stream_archive.asr import (
    AsrError,
    collect_outputs,
    extract_audio,
    remote_path,
    step_transcribe,
    submit_and_wait,
)
from stream_archive.config import AsrConfig, Config, GeneralConfig, StreamerConfig
from stream_archive.state import Job


class FakeAsrHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.audio_path = payload.get("audio_path", "")  # type: ignore[attr-defined]
        self.server.submits += 1  # type: ignore[attr-defined]
        self._json({"task_id": "t1"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/task_status/"):
            if self.server.fail:  # type: ignore[attr-defined]
                self._json({"status": "error", "error_msg": "boom"})
                return
            if self.server.write_files:  # type: ignore[attr-defined]
                audio = Path(self.server.audio_path)  # type: ignore[attr-defined]
                audio.with_suffix(".txt").write_text("转写文本", encoding="utf-8")
                audio.with_suffix(".srt").write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n转写文本\n", encoding="utf-8"
                )
            self._json({"status": "done", "result": {}})
        elif self.path.startswith("/task_file/"):
            self._json({"filename": "a.txt", "content": "兜底文本"})
        else:
            self._json({"error": "not found"}, status=404)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class AsrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAsrHandler)
        self.server.audio_path = ""  # type: ignore[attr-defined]
        self.server.submits = 0  # type: ignore[attr-defined]
        self.server.write_files = True  # type: ignore[attr-defined]
        self.server.fail = False  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.config = AsrConfig(
            enabled=True,
            server=f"http://127.0.0.1:{self.server.server_address[1]}",
            poll_interval=1,
            timeout=10,
        )
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def _audio(self) -> Path:
        audio = self.root / "a.m4a"
        audio.write_bytes(b"fake audio")
        return audio

    # ---- 路径映射 ----
    def test_remote_path_without_mapping(self) -> None:
        audio = self.root / "a.m4a"
        self.assertEqual(remote_path(audio, self.config), str(audio))

    def test_remote_path_with_windows_mapping(self) -> None:
        config = AsrConfig(
            path_map_from="/data/videos", path_map_to="\\\\NAS\\videos"
        )
        mapped = remote_path(Path("/data/videos/主播/a.m4a"), config)
        self.assertEqual(mapped, "\\\\NAS\\videos\\主播\\a.m4a")

    def test_remote_path_outside_prefix_fails(self) -> None:
        config = AsrConfig(path_map_from="/data/videos", path_map_to="/mnt")
        with self.assertRaises(AsrError):
            remote_path(Path("/other/a.m4a"), config)

    # ---- HTTP 流程 ----
    def test_submit_and_collect_outputs(self) -> None:
        audio = self._audio()
        task_id, result = submit_and_wait(self.config, audio)

        self.assertEqual(task_id, "t1")
        self.assertEqual(self.server.submits, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.audio_path, str(audio))  # type: ignore[attr-defined]

        txt, srt = collect_outputs(task_id, audio, result, self.config)
        self.assertIsNotNone(txt)
        self.assertIsNotNone(srt)
        self.assertEqual(txt.read_text(encoding="utf-8"), "转写文本")  # type: ignore[union-attr]
        self.assertIn("00:00:00,000", srt.read_text(encoding="utf-8"))  # type: ignore[union-attr]

    def test_collect_uses_task_file_fallback(self) -> None:
        audio = self._audio()
        task_id, result = submit_and_wait(self.config, audio)
        # 服务没有写文件，只能走 /task_file
        audio.with_suffix(".txt").unlink()
        audio.with_suffix(".srt").unlink()

        txt, srt = collect_outputs(task_id, audio, result, self.config)
        self.assertIsNotNone(txt)
        self.assertIsNone(srt)
        self.assertEqual(txt.read_text(encoding="utf-8"), "兜底文本")  # type: ignore[union-attr]

    def test_error_status_raises(self) -> None:
        self.server.fail = True  # type: ignore[attr-defined]
        with self.assertRaises(AsrError):
            submit_and_wait(self.config, self._audio())

    # ---- 音频提取 ----
    def test_extract_audio_with_fake_ffmpeg(self) -> None:
        binary = self.root / "ffmpeg"
        binary.write_text(
            "#!/usr/bin/env python3\nimport sys\nopen(sys.argv[-1], 'wb').write(b'audio')\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        video = self.root / "v.mp4"
        video.write_bytes(b"video")
        output = self.root / "out.m4a"

        with patch.dict(os.environ, {"PATH": f"{self.root}:{os.environ.get('PATH','')}"}):
            extract_audio(video, output)

        self.assertTrue(output.is_file())

    # ---- 流水线步骤 ----
    def test_step_transcribe(self) -> None:
        raw_dir = self.root / "videos" / "主播" / "原文件"
        raw_dir.mkdir(parents=True)
        video = raw_dir / "demo.mp4"
        video.write_bytes(b"video")
        (raw_dir / "demo.m4a").write_bytes(b"audio")

        config = Config(
            general=GeneralConfig(
                work_dir=self.root / "work",
                videos_dir=self.root / "videos",
                data_dir=self.root / "state",
                log_dir=self.root / "logs",
            )
        )
        streamer = StreamerConfig(id="demo", name="主播", asr=self.config)
        job = Job(
            id="demo-1",
            streamer_id="demo",
            platform="录播姬",
            streamer="主播",
            stream_title="直播",
            start_time="2026年11月15日20点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work"),
        )
        job.outputs["archive"] = {"raw_dir": str(raw_dir)}

        detail, skipped = step_transcribe(config, streamer, job)

        self.assertFalse(skipped)
        self.assertIn("转写 1 个视频", detail)
        self.assertTrue((raw_dir / "demo.txt").is_file())
        self.assertTrue((raw_dir / "demo.srt").is_file())
        self.assertIn("demo.mp4", job.outputs["asr"]["done"])

        # 重跑不重复提交
        _, skipped2 = step_transcribe(config, streamer, job)
        self.assertTrue(skipped2)
        self.assertEqual(self.server.submits, 1)  # type: ignore[attr-defined]

    def test_disabled_is_skipped(self) -> None:
        streamer = StreamerConfig(id="demo", name="主播")
        job = Job(
            id="demo-2",
            streamer_id="demo",
            platform="录播姬",
            streamer="主播",
            stream_title="直播",
            start_time="2026年11月15日20点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work"),
        )
        config = Config(general=GeneralConfig(data_dir=self.root / "state"))
        _, skipped = step_transcribe(config, streamer, job)
        self.assertTrue(skipped)


if __name__ == "__main__":
    unittest.main()
