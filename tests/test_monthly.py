"""按月投稿测试：月份、映射文件、新建月稿件、追加分P、重跑幂等。

用假 biliup 脚本，不访问 B 站。
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_archive.config import (
    AppendConfig,
    Config,
    GeneralConfig,
    StreamerConfig,
    UploadConfig,
    WebdavConfig,
)
from stream_archive.monthly import (
    load_vid_map,
    map_key,
    month_of,
    render_template,
    save_vid_map,
    step_append_monthly,
)
from stream_archive.state import Job

FAKE_BILIUP = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_MARK"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")

if "append" in args:
    print("稿件修改成功")
elif "list" in args:
    print("BV1AAAAAAAAA 2026年11月 直播回放合集")
else:
    print("投稿成功")
    print("BV1AAAAAAAAA")
"""


class MonthlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.biliup = self.bin_dir / "biliup"
        self.biliup.write_text(FAKE_BILIUP, encoding="utf-8")
        self.biliup.chmod(0o755)

        self.cookies = self.root / "cookies.json"
        self.cookies.write_text("{}", encoding="utf-8")

        self.marker = self.root / "calls.txt"
        self.env = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_MARK": str(self.marker),
        }

        self.map_file = self.root / "map" / "append_month_vid.map"

        self.config = Config(
            general=GeneralConfig(
                work_dir=self.root / "work",
                videos_dir=self.root / "videos",
                data_dir=self.root / "data",
                log_dir=self.root / "logs",
            )
        )
        self.streamer = StreamerConfig(
            id="demo",
            name="演示主播",
            upload=UploadConfig(
                enabled=True,
                biliup=self.biliup,
                cookies=self.cookies,
                tags=["直播回放"],
            ),
            append=AppendConfig(
                enabled=True,
                map_file=str(self.map_file),
                source="raw",
                title_template="{streamer} {month_cn} 直播回放合集",
                desc_template="{streamer} {month} 合集",
            ),
        )

        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self.temp_dir.cleanup()

    def _job(self, job_id: str, start_time: str, names: list[str]) -> Job:
        work_dir = self.root / "work" / "演示主播" / start_time
        work_dir.mkdir(parents=True)
        for name in names:
            (work_dir / name).write_bytes(b"x")
        return Job(
            id=job_id,
            streamer_id="demo",
            platform="录播姬",
            streamer="演示主播",
            stream_title="测试直播",
            start_time=start_time,
            source_dir=str(self.root / "src"),
            work_dir=str(work_dir),
        )

    # ---- 纯函数 ----
    def test_month_of(self) -> None:
        self.assertEqual(
            month_of("2026年11月14日12点00分00秒"), ("2026-11", "2026年11月")
        )

    def test_map_roundtrip(self) -> None:
        mapping = {map_key("演示主播", "2026-11"): "BV1AAAAAAAAA"}
        save_vid_map(self.map_file, mapping)
        self.assertEqual(load_vid_map(self.map_file), mapping)
        # 坏行和注释要能忽略
        self.map_file.write_text(
            "# comment\n坏行\n演示主播|2026-12=BV1BBBBBBBBB\n", encoding="utf-8"
        )
        self.assertEqual(
            load_vid_map(self.map_file), {"演示主播|2026-12": "BV1BBBBBBBBB"}
        )

    def test_render_template(self) -> None:
        text = render_template(
            "{streamer} {month} {month_cn}", "演示主播", "2026-11", "2026年11月"
        )
        self.assertEqual(text, "演示主播 2026-11 2026年11月")

    # ---- 新建 + 追加 ----
    def test_create_then_append(self) -> None:
        job = self._job(
            "demo-1",
            "2026年11月14日12点00分00秒",
            ["a.mp4", "b.mp4"],
        )

        with patch.dict(os.environ, self.env):
            detail, skipped = step_append_monthly(self.config, self.streamer, job)

        self.assertFalse(skipped)
        self.assertIn("新建并追加", detail)
        self.assertIn("BV1AAAAAAAAA", detail)

        calls = self.marker.read_text(encoding="utf-8").splitlines()
        # 一次新建（upload）、一次追加（append --vid）
        self.assertEqual(len(calls), 2)
        self.assertIn("upload", calls[0])
        self.assertIn("append --vid BV1AAAAAAAAA", calls[1])
        # 分P名称沿用旧逻辑：时间戳，第二批起带 _1 后缀
        self.assertIn("2026年11月14日12点00分00秒.mp4", calls[0])
        self.assertIn("2026年11月14日12点00分00秒_1.mp4", calls[1])

        # 映射文件已落盘
        mapping = load_vid_map(self.map_file)
        self.assertEqual(mapping["演示主播|2026-11"], "BV1AAAAAAAAA")

        # 重跑同一任务：已追加的文件不再重复
        with patch.dict(os.environ, self.env):
            detail2, skipped2 = step_append_monthly(self.config, self.streamer, job)

        self.assertTrue(skipped2)
        self.assertIn("已全部追加", detail2)
        self.assertEqual(len(self.marker.read_text(encoding="utf-8").splitlines()), 2)

    # ---- 同月后续场次：只追加 ----
    def test_later_job_appends_existing_month(self) -> None:
        save_vid_map(
            self.map_file,
            {map_key("演示主播", "2026-11"): "BV1AAAAAAAAA"},
        )
        job = self._job(
            "demo-2",
            "2026年11月20日20点00分00秒",
            ["c.mp4"],
        )

        with patch.dict(os.environ, self.env):
            detail, skipped = step_append_monthly(self.config, self.streamer, job)

        self.assertFalse(skipped)
        self.assertIn("追加 1 个文件", detail)

        calls = self.marker.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("append --vid BV1AAAAAAAAA", calls[0])
        self.assertIn("2026年11月20日20点00分00秒.mp4", calls[0])

    def test_append_uses_filename_timestamp_and_keeps_original(self) -> None:
        save_vid_map(
            self.map_file,
            {map_key("演示主播", "2026-11"): "BV1AAAAAAAAA"},
        )
        name = "录播姬_2026年11月20日19点59分13秒_测试标题_演示主播.mp4"
        job = self._job("demo-ts", "2026年11月20日19点00分00秒", [name])

        with patch.dict(os.environ, self.env):
            step_append_monthly(self.config, self.streamer, job)

        calls = self.marker.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("2026年11月20日19点59分13秒.mp4", calls[0])
        work_dir = Path(job.work_dir)
        self.assertTrue((work_dir / name).is_file())
        self.assertFalse((work_dir / "2026年11月20日19点59分13秒.mp4").exists())

    def test_disabled_is_skipped(self) -> None:
        self.streamer.append.enabled = False
        job = self._job("demo-3", "2026年11月14日12点00分00秒", ["a.mp4"])

        _, skipped = step_append_monthly(self.config, self.streamer, job)
        self.assertTrue(skipped)

    def test_pan_url_is_rendered(self) -> None:
        # 开启 WebDAV 且配置 public_url 时，月稿件简介里的 {pan_url} 要替换成月份地址
        self.streamer.webdav = WebdavConfig(
            enabled=True,
            url="http://127.0.0.1:1/dav",
            path="/弹幕/{streamer}/{yyyy}/{mm}/{date}",
            public_url="https://pan.example.com/弹幕",
        )
        self.streamer.append.desc_template = "弹幕网盘：{pan_url}"
        job = self._job("demo-4", "2026年11月14日12点00分00秒", ["a.mp4"])

        with patch.dict(os.environ, self.env):
            step_append_monthly(self.config, self.streamer, job)

        calls = self.marker.read_text(encoding="utf-8").splitlines()
        self.assertIn("https://pan.example.com/弹幕/演示主播/2026/11", calls[0])


if __name__ == "__main__":
    unittest.main()
