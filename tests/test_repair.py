"""自动修复测试：解析退回时间段、切片段、单场重投流程。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_archive.config import (
    Config,
    GeneralConfig,
    RepairConfig,
    StreamerConfig,
    UploadConfig,
)
from stream_archive.repair import (
    CutRange,
    _pressed_files,
    cut_file,
    execute_one_repair,
    is_rights_rejection,
    keep_segments,
    parse_review_ranges,
)
from stream_archive.state import Job
from stream_archive.upload import UploadResult


def make_job(data: Path, outputs: dict) -> Job:
    job = Job(
        id="j-repair",
        streamer_id="gaojidong",
        platform="录播姬",
        streamer="高机动持盾军官",
        stream_title="测试直播",
        start_time="2026-11-14 19:48:08",
        source_dir="",
        work_dir="",
    )
    job.outputs = outputs
    return job


class RepairParseTest(unittest.TestCase):
    def test_parse_p_range(self) -> None:
        review = {
            "kind": "rejected",
            "problem_details": [
                {
                    "type": "video",
                    "index": 2,
                    "reason": "该视频【P2(03:52:30-03:52:34)】含有违禁内容",
                    "violation_time": "",
                }
            ],
        }
        ranges = parse_review_ranges(review)
        self.assertEqual(ranges, [CutRange(2, 3 * 3600 + 52 * 60 + 30, 3 * 3600 + 52 * 60 + 34)])

    def test_parse_time_range_without_p(self) -> None:
        review = {
            "kind": "rejected",
            "problem_details": [
                {
                    "type": "video",
                    "index": 1,
                    "reason": "该视频【 02:25:26 - 02:27:09 】争议性较大",
                }
            ],
        }
        ranges = parse_review_ranges(review)
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0].pindex, 1)
        self.assertEqual(ranges[0].start, 2 * 3600 + 25 * 60 + 26)
        self.assertEqual(ranges[0].end, 2 * 3600 + 27 * 60 + 9)

    def test_rights_rejection(self) -> None:
        self.assertTrue(
            is_rights_rejection({"modify_advise": "接权利人或相关方举报，现予以限制播放"})
        )
        self.assertFalse(is_rights_rejection({"reject_reason": "该视频含有违禁内容"}))

    def test_keep_segments(self) -> None:
        keep = keep_segments(100, [CutRange(1, 40, 50)], margin=2)
        self.assertEqual(keep, [(0.0, 38.0), (52.0, 100.0)])

    def test_keep_segments_merge(self) -> None:
        keep = keep_segments(
            100, [CutRange(1, 10, 20), CutRange(1, 19, 30)], margin=0
        )
        self.assertEqual(keep, [(0.0, 10.0), (30.0, 100.0)])


class RepairFlowTest(unittest.TestCase):
    def test_pressed_files_from_upload_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.mp4"
            path.write_bytes(b"x")
            job = make_job(Path(temp), {"upload_files": [str(path)]})
            self.assertEqual(_pressed_files(job), [path])

    def test_daily_repair_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pressed_dir = root / "pressed"
            pressed_dir.mkdir()
            source = pressed_dir / "投稿版-a.mp4"
            source.write_bytes(b"source")
            cookies = root / "cookies.json"
            cookies.write_text("{}", encoding="utf-8")

            config = Config(
                general=GeneralConfig(data_dir=root / "state"),
                repair=RepairConfig(max_attempts=1, margin_seconds=2),
            )
            config.streamers = [
                StreamerConfig(
                    id="gaojidong",
                    name="高机动持盾军官",
                    upload=UploadConfig(enabled=True, cookies=cookies),
                )
            ]
            job = make_job(
                root,
                {
                    "upload": {"ok": True, "title": "原标题", "bvid": "BV1OLD000000"},
                    "upload_files": [str(source)],
                    "archive": {"pressed_dir": str(pressed_dir), "raw_dir": ""},
                    "cover": {},
                    "review": {
                        "kind": "rejected",
                        "bvid": "BV1OLD000000",
                        "problem_details": [
                            {
                                "type": "video",
                                "index": 1,
                                "reason": "该视频【P1(00:00:01-00:00:02)】含有违禁内容",
                            }
                        ],
                        "reject_reason": "稿件中发现问题。",
                        "problem_description": "",
                        "modify_advise": "建议删除片段",
                        "problem_title": "关于违禁内容",
                    },
                },
            )
            logger = logging.getLogger("test-repair")

            def fake_cut(src, ranges, output, margin, logger=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"fixed")
                return output

            with (
                patch("stream_archive.repair.cut_file", side_effect=fake_cut),
                patch(
                    "stream_archive.repair.upload",
                    return_value=UploadResult(True, "投稿成功 BV1FIX000000"),
                ) as upload_mock,
            ):
                acted = execute_one_repair(config, job, logger)

            self.assertTrue(acted)
            self.assertEqual(job.outputs["repair"]["status"], "uploaded")
            self.assertEqual(job.outputs["repair"]["bvid"], "BV1FIX000000")
            self.assertEqual(job.outputs["repair"]["attempts"], 1)
            upload_mock.assert_called_once()
            files = upload_mock.call_args.args[1]
            self.assertEqual(files[0].name, "修复版-投稿版-a.mp4")
            self.assertIn("修复版", upload_mock.call_args.kwargs["title"])

    def test_monthly_repair_only_generates_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_dir = root / "raw"
            raw_dir.mkdir()
            source = raw_dir / "录播姬_测试.mp4"
            source.write_bytes(b"raw")
            config = Config(
                general=GeneralConfig(data_dir=root / "state"),
                repair=RepairConfig(enabled=True),
            )
            job = make_job(
                root,
                {
                    "append": {"vid": "BV1MONTH00000", "done": [source.name]},
                    "archive": {"raw_dir": str(raw_dir), "pressed_dir": ""},
                    "append_review": {
                        "kind": "rejected",
                        "bvid": "BV1MONTH00000",
                        "problem_details": [
                            {
                                "type": "video",
                                "index": 1,
                                "reason": "该视频【P1(00:00:01-00:00:02)】含有违禁内容",
                            }
                        ],
                        "modify_advise": "建议删除片段",
                    },
                },
            )

            def fake_cut(src, ranges, output, margin, logger=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"fixed")
                return output

            with (
                patch("stream_archive.repair.cut_file", side_effect=fake_cut),
                patch("stream_archive.repair.upload") as upload_mock,
            ):
                acted = execute_one_repair(config, job, logging.getLogger("test"))

            self.assertTrue(acted)
            self.assertEqual(job.outputs["monthly_repair"]["status"], "ready")
            self.assertIn("人工替换", job.outputs["monthly_repair"]["message"])
            upload_mock.assert_not_called()

    def test_rights_rejection_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = Config(repair=RepairConfig())
            job = make_job(
                root,
                {
                    "review": {
                        "kind": "rejected",
                        "bvid": "BV1OLD000000",
                        "problem_details": [
                            {
                                "type": "video",
                                "index": 1,
                                "reason": "该视频【P1(00:00:01-00:00:02)】",
                            }
                        ],
                        "modify_advise": "接权利人或相关方举报，现予以限制播放",
                    }
                },
            )
            acted = execute_one_repair(config, job, logging.getLogger("test"))
            self.assertFalse(acted)
            self.assertEqual(job.outputs["repair"]["status"], "skipped")


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe"
)
class CutFileTest(unittest.TestCase):
    def test_cut_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x240:rate=30:duration=5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100:duration=5",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-c:a",
                    "aac",
                    "-y",
                    str(source),
                ],
                check=True,
            )
            output = root / "fixed.mp4"
            cut_file(source, [CutRange(1, 2.0, 3.0)], output, margin=0)
            self.assertTrue(output.is_file())
            duration = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertGreater(float(duration), 2.5)
            self.assertLess(float(duration), 5.0)


if __name__ == "__main__":
    unittest.main()
