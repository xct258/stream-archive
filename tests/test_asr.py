"""语音转文本测试：音频提取、一次性 worker、txt/srt 落盘、重跑幂等。

worker 通过真实的子进程运行，faster_whisper 用临时目录里的假模块代替，
不下载模型、不依赖网络。
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_archive.asr import (
    AsrError,
    build_worker_command,
    extract_audio,
    step_transcribe,
)
from stream_archive.config import AsrConfig, Config, GeneralConfig, StreamerConfig
from stream_archive.state import Job

FAKE_FASTER_WHISPER = '''"""测试用假 faster-whisper。"""

import os


class Segment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class Info:
    duration = 5.0


class WhisperModel:
    def __init__(self, *args, **kwargs):
        if os.environ.get("FAKE_ASR_FAIL"):
            raise RuntimeError("fake model load failure")

    def transcribe(self, audio_path, **kwargs):
        return iter([Segment(0.0, 2.0, "你好"), Segment(2.0, 4.0, "世界")]), Info()
'''


class AsrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.fake_dir = self.root / "fake_modules"
        self.fake_dir.mkdir()
        (self.fake_dir / "faster_whisper.py").write_text(
            FAKE_FASTER_WHISPER, encoding="utf-8"
        )

        self.raw_dir = self.root / "videos" / "主播" / "原文件"
        self.raw_dir.mkdir(parents=True)
        self.video = self.raw_dir / "demo.mp4"
        self.video.write_bytes(b"video")
        self.audio = self.raw_dir / "demo.m4a"
        self.audio.write_bytes(b"audio")

        self.config = Config(
            general=GeneralConfig(
                work_dir=self.root / "work",
                videos_dir=self.root / "videos",
                data_dir=self.root / "state",
                log_dir=self.root / "logs",
            )
        )
        self.streamer = StreamerConfig(
            id="demo",
            name="主播",
            asr=AsrConfig(
                enabled=True,
                model="fake",
                model_dir=self.root / "models",
                timeout=60,
            ),
        )
        self.job = Job(
            id="demo-1",
            streamer_id="demo",
            platform="录播姬",
            streamer="主播",
            stream_title="直播",
            start_time="2026年11月15日20点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work"),
        )
        self.job.outputs["archive"] = {"raw_dir": str(self.raw_dir)}

        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self.temp_dir.cleanup()

    # ---- 命令组装 ----
    def test_build_worker_command(self) -> None:
        asr = AsrConfig(
            enabled=True,
            model="small",
            language="zh",
            device="cpu",
            compute_type="int8",
            model_dir=self.root / "models",
            beam_size=3,
            hf_endpoint="https://hf-mirror.com",
        )
        command = build_worker_command(asr, [self.audio])

        self.assertEqual(command[1:3], ["-m", "stream_archive.asr_worker"])
        self.assertIn("small", command)
        self.assertIn("--hf-endpoint", command)
        self.assertIn(str(self.audio), command)

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

        with patch.dict(
            os.environ, {"PATH": f"{self.root}:{os.environ.get('PATH','')}"}
        ):
            extract_audio(video, output)

        self.assertTrue(output.is_file())

    # ---- 完整步骤 ----
    def test_step_transcribe(self) -> None:
        env = {
            "PYTHONPATH": f"{self.fake_dir}{os.pathsep}{os.environ.get('PYTHONPATH','')}",
        }
        with patch.dict(os.environ, env):
            detail, skipped = step_transcribe(self.config, self.streamer, self.job)

        self.assertFalse(skipped)
        self.assertIn("转写 1 个视频", detail)
        self.assertEqual(
            (self.raw_dir / "demo.txt").read_text(encoding="utf-8"), "你好\n世界\n"
        )
        self.assertIn(
            "00:00:00,000 --> 00:00:02,000",
            (self.raw_dir / "demo.srt").read_text(encoding="utf-8"),
        )
        self.assertIn("demo.mp4", self.job.outputs["asr"]["done"])

        # 重跑直接跳过
        with patch.dict(os.environ, env):
            detail2, skipped2 = step_transcribe(self.config, self.streamer, self.job)
        self.assertTrue(skipped2)
        self.assertIn("已全部转写", detail2)

    def test_worker_failure_raises(self) -> None:
        env = {
            "PYTHONPATH": f"{self.fake_dir}{os.pathsep}{os.environ.get('PYTHONPATH','')}",
            "FAKE_ASR_FAIL": "1",
        }
        with patch.dict(os.environ, env):
            with self.assertRaises(AsrError):
                step_transcribe(self.config, self.streamer, self.job)

    def test_disabled_is_skipped(self) -> None:
        self.streamer.asr.enabled = False
        _, skipped = step_transcribe(self.config, self.streamer, self.job)
        self.assertTrue(skipped)


if __name__ == "__main__":
    unittest.main()
