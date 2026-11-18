"""流水线测试：主播匹配、目录规划、扫描清理、旧录播清理。

不调用 ffmpeg，用空文件模拟视频即可覆盖大部分文件逻辑。
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from stream_archive.config import load_config
from unittest import mock

from stream_archive.pipeline import (
    archive_dirs,
    cleanup_archives,
    find_streamer,
    process_source_dir,
    step_duration_check,
    step_scan,
    upload_candidates,
)
from stream_archive.state import Job, save_job

CONFIG_TEMPLATE = """
[general]
work_dir = "{root}/work"
videos_dir = "{root}/videos"
data_dir = "{root}/data"
log_dir = "{root}/logs"

[watch]
source_folders = ["{root}/rec"]

[archive]
retention_days = 15

[[streamers]]
id = "a"
name = "主播A"
aliases = ["昵称A"]

[[streamers]]
id = "b"
name = "主播B"
aliases = ["昵称B"]
source_folders = ["{root}/onlyb"]
"""

LOGGER = logging.getLogger("stream_archive.tests")


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            CONFIG_TEMPLATE.format(root=self.root), encoding="utf-8"
        )
        self.config = load_config(self.config_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # ---- 主播匹配 ----
    def test_find_streamer_by_alias(self) -> None:
        directory = self.root / "rec" / "anything"
        directory.mkdir(parents=True)
        streamer = find_streamer(self.config, "昵称A", directory)
        self.assertIsNotNone(streamer)
        self.assertEqual(streamer.id, "a")

    def test_find_streamer_by_directory_owner(self) -> None:
        directory = self.root / "onlyb" / "2026-11-13"
        directory.mkdir(parents=True)
        streamer = find_streamer(self.config, "未知昵称", directory)
        self.assertIsNotNone(streamer)
        self.assertEqual(streamer.id, "b")

    def test_find_streamer_unknown(self) -> None:
        directory = self.root / "rec" / "2026-11-13"
        directory.mkdir(parents=True)
        self.assertIsNone(find_streamer(self.config, "未配置的主播", directory))

    # ---- 归档目录 ----
    def test_archive_dirs(self) -> None:
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None
        pressed, raw = archive_dirs(
            self.config, streamer, "2026年11月13日19点56分57秒"
        )
        self.assertEqual(
            pressed,
            self.root / "videos" / "主播A" / "压制版" / "2026" / "11" / "2026-11-13",
        )
        self.assertEqual(
            raw,
            self.root / "videos" / "主播A" / "原文件" / "2026" / "11" / "2026-11-13",
        )

    # ---- 扫描与碎片清理 ----
    def test_step_scan_moves_and_cleans(self) -> None:
        source = self.root / "rec" / "2026-11-13"
        source.mkdir(parents=True)

        big = source / "big.mp4"
        with big.open("wb") as handle:
            handle.seek(11 * 1024 * 1024 - 1)
            handle.write(b"\0")

        small = source / "small.flv"
        small.write_bytes(b"x" * 1024)
        (source / "small.xml").write_text("<i/>", encoding="utf-8")
        (source / "orphan.xml").write_text("<i/>", encoding="utf-8")

        job = Job(
            id="a-test",
            streamer_id="a",
            platform="录播姬",
            streamer="主播A",
            stream_title="测试",
            start_time="2026年11月13日19点56分57秒",
            source_dir=str(source),
            work_dir=str(self.root / "work" / "主播A" / "2026年11月13日19点56分57秒"),
        )
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None

        detail, skipped = step_scan(self.config, streamer, job, LOGGER)

        self.assertFalse(skipped)
        self.assertIn("保留 1 个", detail)
        work_dir = Path(job.work_dir)
        self.assertTrue((work_dir / "big.mp4").is_file())
        self.assertFalse((work_dir / "small.flv").exists())
        self.assertFalse((work_dir / "small.xml").exists())
        self.assertFalse((work_dir / "orphan.xml").exists())
        self.assertFalse(source.exists())

    # ---- 投稿文件选择 ----
    def test_upload_candidates_prefers_encoded(self) -> None:
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None

        work_dir = self.root / "work" / "a"
        work_dir.mkdir(parents=True)
        raw = work_dir / "raw.mp4"
        raw.write_bytes(b"x")
        encoded = work_dir / "投稿版-raw.mp4"
        encoded.write_bytes(b"x")

        job = Job(
            id="a-test",
            streamer_id="a",
            platform="录播姬",
            streamer="主播A",
            stream_title="测试",
            start_time="2026年11月13日19点56分57秒",
            source_dir=str(self.root / "src"),
            work_dir=str(work_dir),
        )
        job.outputs["encoded"] = [str(encoded)]

        self.assertEqual(upload_candidates(job, streamer), [encoded])

    # ---- 时长校验 ----
    def _duration_job(self) -> Job:
        work_dir = self.root / "work"
        work_dir.mkdir(parents=True)
        (work_dir / "v.mp4").write_bytes(b"x")
        (work_dir / "v.xml").write_bytes(b"<i></i>")
        return Job(
            id="t",
            streamer_id="a",
            platform="录播姬",
            streamer="主播A",
            stream_title="标题",
            start_time="2026年11月17日19点55分29秒",
            source_dir="",
            work_dir=str(work_dir),
        )

    def test_duration_check_allows_trailing_danmaku(self) -> None:
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None
        job = self._duration_job()

        with mock.patch("stream_archive.pipeline.duration_diff", return_value=-36.8), \
             mock.patch("stream_archive.pipeline.max_packet_gap", return_value=0.067):
            detail, skipped = step_duration_check(self.config, streamer, job, LOGGER)

        self.assertFalse(skipped)
        self.assertEqual(detail, "1/1 个视频通过时长校验")
        self.assertTrue(job.outputs["pairs"][0]["ok"])
        self.assertEqual(job.outputs["pairs"][0]["trailing"], 36.8)

    def test_duration_check_rejects_internal_gap(self) -> None:
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None
        job = self._duration_job()

        with mock.patch("stream_archive.pipeline.duration_diff", return_value=-36.8), \
             mock.patch("stream_archive.pipeline.max_packet_gap", return_value=42.0):
            detail, _ = step_duration_check(self.config, streamer, job, LOGGER)

        self.assertEqual(detail, "0/1 个视频通过时长校验")
        pair = job.outputs["pairs"][0]
        self.assertFalse(pair["ok"])
        self.assertIn("断流", pair["reason"])

    def test_duration_check_rejects_long_video(self) -> None:
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None
        job = self._duration_job()

        with mock.patch("stream_archive.pipeline.duration_diff", return_value=600.0), \
             mock.patch("stream_archive.pipeline.max_packet_gap", return_value=0.0) as gap:
            detail, _ = step_duration_check(self.config, streamer, job, LOGGER)

        self.assertEqual(detail, "0/1 个视频通过时长校验")
        gap.assert_not_called()

    # ---- 旧录播清理 ----
    def test_cleanup_only_when_new_job_starts(self) -> None:
        directory = self.root / "rec" / "2026-11-13"
        directory.mkdir(parents=True)
        (directory / "录播姬_2026年11月13日19点56分57秒_标题_昵称A.mp4").write_bytes(b"x")

        with mock.patch(
            "stream_archive.pipeline.cleanup_archives", return_value=0
        ) as cleanup, mock.patch(
            "stream_archive.pipeline.process_job", return_value=True
        ) as process:
            process_source_dir(self.config, directory, LOGGER, cleanup_first=True)
            # 第二天同一个目录（任务已存在）不应再次触发清理
            process_source_dir(self.config, directory, LOGGER, cleanup_first=True)

        cleanup.assert_called_once()
        self.assertEqual(process.call_count, 2)

    def test_cleanup_keeps_unreviewed_job(self) -> None:
        base = self.root / "videos" / "主播A" / "原文件" / "2000" / "01" / "2000-01-01"
        base.mkdir(parents=True)
        (base / "old.mp4").write_bytes(b"x")

        job = Job(
            id="a-20000101_190000",
            streamer_id="a",
            platform="录播姬",
            streamer="主播A",
            stream_title="标题",
            start_time="2000年01月01日19点00分00秒",
            source_dir="",
            work_dir=str(self.root / "work" / "主播A" / "x"),
        )
        job.outputs["review"] = {"kind": "reviewing"}
        data_dir = Path(self.config.general.data_dir)
        save_job(data_dir, job)

        deleted = cleanup_archives(self.config, LOGGER)
        self.assertEqual(deleted, 0)
        self.assertTrue((base / "old.mp4").exists())

        # 审核通过后不再保护，下一轮正常删除
        job.outputs["review"] = {"kind": "published"}
        save_job(data_dir, job)
        deleted = cleanup_archives(self.config, LOGGER)
        self.assertGreaterEqual(deleted, 1)
        self.assertFalse((base / "old.mp4").exists())

    def test_cleanup_archives_keep_xml(self) -> None:
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None
        streamer.archive.keep_xml = True

        base = self.root / "videos" / "主播A" / "原文件" / "2000" / "01" / "2000-01-01"
        base.mkdir(parents=True)
        (base / "old.mp4").write_bytes(b"x")
        (base / "old.xml").write_bytes(b"<i></i>")

        deleted = cleanup_archives(self.config, LOGGER)

        self.assertGreaterEqual(deleted, 1)
        self.assertFalse((base / "old.mp4").exists())
        self.assertTrue((base / "old.xml").exists())

    def test_cleanup_archives(self) -> None:
        streamer = self.config.streamer_by_id("a")
        assert streamer is not None

        base = self.root / "videos" / "主播A" / "原文件" / "2000" / "01" / "2000-01-01"
        base.mkdir(parents=True)
        (base / "old.mp4").write_bytes(b"x")

        today = date.today()
        today_dir = (
            self.root
            / "videos"
            / "主播A"
            / "原文件"
            / today.strftime("%Y")
            / today.strftime("%m")
            / today_str()
        )
        today_dir.mkdir(parents=True)
        (today_dir / "new.mp4").write_bytes(b"x")

        deleted = cleanup_archives(self.config, LOGGER)

        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(base.exists())
        self.assertTrue(today_dir.exists())


def today_str() -> str:
    today = date.today()
    return today.strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()
