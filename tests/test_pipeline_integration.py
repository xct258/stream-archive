"""本地端到端测试：用假 ffmpeg/ffprobe/DanmakuFactory/biliup 跑完整流水线。

目的：
    在没有真实视频、也不连接 NAS 的情况下，验证 8 个步骤的编排、
    文件移动、状态持久化、归档结构和投稿参数。

假程序都放在临时目录里，通过修改 PATH 生效。
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
    ArchiveConfig,
    Config,
    DanmakuConfig,
    EncodeConfig,
    GeneralConfig,
    StreamerConfig,
    UploadConfig,
    WatchConfig,
)
from stream_archive.monthly import load_vid_map
from stream_archive.pipeline import is_finished, process_source_dir
from stream_archive.state import job_file, load_job

FAKE_FFMPEG = """#!/usr/bin/env python3
import shutil
import sys

args = sys.argv[1:]

# 亮度统计：只要输出 YAVG 即可
if any("signalstats" in arg for arg in args):
    print("lavfi.signalstats.YAVG=120.0")
    sys.exit(0)

# 输入和输出
inputs = [args[index + 1] for index, arg in enumerate(args) if arg == "-i"]
source = inputs[0] if inputs else ""
output = args[-1]

# 抽封面
if output.lower().endswith((".jpg", ".jpeg")):
    with open(output, "wb") as handle:
        handle.write(b"fake-jpeg")
    sys.exit(0)

# 压制：复制输入，并输出 ffmpeg 进度格式
if "-progress" in args:
    shutil.copyfile(source, output)
    print("out_time_us=31000000")
    print("speed=2.0x")
    print("progress=end")
    sys.exit(0)

# remux：复制输入
shutil.copyfile(source, output)
"""

FAKE_FFPROBE = """#!/usr/bin/env python3
import json
import sys

print(json.dumps({
    "format": {"duration": "31.0"},
    "streams": [{
        "codec_type": "video",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "60/1",
    }],
}))
"""

FAKE_DANMAKU = """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
output = args[args.index("-o") + 1]
with open(output, "w", encoding="utf-8") as handle:
    handle.write("[Script Info]\\n")
"""

FAKE_BILIUP = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_BILIUP_MARK"], "a", encoding="utf-8") as handle:
    handle.write("CALL " + " ".join(args) + "\\n")

if "append" in args:
    print("稿件修改成功")
elif "list" in args:
    print("BV1AAAAAAAAA 2026年11月 直播回放合集")
else:
    print("投稿成功")
    print("BV1AAAAAAAAA")
"""

XML = """<?xml version="1.0" encoding="UTF-8"?>
<i>
<d p="1.0,1,25,16777215,0,0,0,0">一</d>
<d p="25.0,1,25,16777215,0,0,0,0">二</d>
</i>
"""


class PipelineIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # ---- 假外部程序 ----
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._write_exe("ffmpeg", FAKE_FFMPEG)
        self._write_exe("ffprobe", FAKE_FFPROBE)
        self.factory = self._write_exe("DanmakuFactory", FAKE_DANMAKU)
        self.biliup = self._write_exe("biliup", FAKE_BILIUP)

        # ---- 配置 ----
        self.src_root = self.root / "rec"
        source_dir = self.src_root / "别名" / "2026-11-14"
        source_dir.mkdir(parents=True)
        self.source_dir = source_dir

        # 大于 10MB，避免被当成录制碎片
        flv = source_dir / "录播姬_2026年11月14日12点00分00秒_测试直播_别名.flv"
        with flv.open("wb") as handle:
            handle.seek(11 * 1024 * 1024 - 1)
            handle.write(b"\0")
        flv.with_suffix(".xml").write_text(XML, encoding="utf-8")

        # 第二段：用于验证按月投稿的“新建 P1 + 追加 P2”
        flv2 = source_dir / "录播姬_2026年11月14日12点30分00秒_测试直播_别名.flv"
        with flv2.open("wb") as handle:
            handle.seek(11 * 1024 * 1024 - 1)
            handle.write(b"\0")
        flv2.with_suffix(".xml").write_text(XML, encoding="utf-8")

        cookies = self.root / "cookies.json"
        cookies.write_text("{}", encoding="utf-8")

        self.config = Config(
            general=GeneralConfig(
                work_dir=self.root / "work",
                videos_dir=self.root / "videos",
                data_dir=self.root / "data",
                log_dir=self.root / "logs",
            ),
            watch=WatchConfig(source_folders=[self.src_root]),
            streamers=[
                StreamerConfig(
                    id="demo",
                    name="演示主播",
                    aliases=["别名"],
                    danmaku=DanmakuConfig(factory=self.factory),
                    encode=EncodeConfig(crf=28, try_qsv=False, progress_bar=True),
                    upload=UploadConfig(
                        enabled=True,
                        biliup=self.biliup,
                        cookies=cookies,
                        tid=17,
                        copyright=2,
                        source="https://live.bilibili.com/1",
                        tags=["直播回放"],
                        title_template="{date} [{title}]",
                        desc_template="弹幕 {danmaku_count} 条",
                    ),
                    archive=ArchiveConfig(cleanup_enabled=False),
                    append=AppendConfig(
                        enabled=True,
                        map_file=str(self.root / "data" / "append_month_vid.map"),
                        source="raw",
                        title_template="{streamer} {month_cn} 直播回放合集",
                        desc_template="{streamer} {month} 原版合集",
                    ),
                )
            ],
        )

        self.marker = self.root / "biliup-called.txt"
        self.env = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_BILIUP_MARK": str(self.marker),
        }
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self.temp_dir.cleanup()

    def _write_exe(self, name: str, content: str) -> Path:
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_full_pipeline(self) -> None:
        with patch.dict(os.environ, self.env):
            ok = process_source_dir(self.config, self.source_dir)

        self.assertTrue(ok)

        job = load_job(
            job_file(self.config.general.data_dir, "demo-2026-11-14_120000")
        )
        self.assertTrue(is_finished(job))
        self.assertTrue((job.outputs.get("upload") or {}).get("ok"))

        # 归档结构
        pressed = (
            self.root
            / "videos"
            / "演示主播"
            / "压制版"
            / "2026"
            / "11"
            / "2026-11-14"
        )
        raw = (
            self.root
            / "videos"
            / "演示主播"
            / "原文件"
            / "2026"
            / "11"
            / "2026-11-14"
        )
        self.assertTrue((pressed / "投稿版-录播姬_2026年11月14日12点00分00秒_测试直播_别名.mp4").is_file())
        self.assertTrue((pressed / "投稿版-录播姬_2026年11月14日12点30分00秒_测试直播_别名.mp4").is_file())
        self.assertTrue((pressed / "录播姬_2026年11月14日12点00分00秒_测试直播_别名.jpg").is_file())
        self.assertTrue((raw / "录播姬_2026年11月14日12点00分00秒_测试直播_别名.mp4").is_file())
        self.assertTrue((raw / "录播姬_2026年11月14日12点30分00秒_测试直播_别名.mp4").is_file())
        self.assertTrue((raw / "录播姬_2026年11月14日12点00分00秒_测试直播_别名.xml").is_file())
        self.assertTrue((raw / "录播姬_2026年11月14日12点30分00秒_测试直播_别名.xml").is_file())

        # 源目录和缓存目录都应清理掉
        self.assertFalse(self.source_dir.exists())
        self.assertFalse(Path(job.work_dir).exists())

        # 普通投稿参数
        self.assertTrue(self.marker.is_file())
        calls = self.marker.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("--title" in line for line in calls))
        self.assertTrue(any("2026年11月14日 [测试直播]" in line for line in calls))
        self.assertTrue(any("--cover" in line for line in calls))
        self.assertTrue(any("--tag" in line for line in calls))

        # 按月投稿：新建月稿件 + 追加第二个分P，映射文件落盘
        self.assertTrue(any("2026年11月 直播回放合集" in line for line in calls))
        self.assertTrue(any("append --vid BV1AAAAAAAAA" in line for line in calls))
        mapping = load_vid_map(self.root / "data" / "append_month_vid.map")
        self.assertEqual(mapping, {"演示主播|2026-11": "BV1AAAAAAAAA"})

        # WebDAV 未启用时应显式跳过
        self.assertEqual(job.status_of("upload_danmaku"), "skipped")

        # 高能进度条开启时，压制仍应成功
        self.assertEqual(job.status_of("encode"), "done")
        self.assertFalse(list(pressed.glob("bar-*.ppm")))

    def test_small_files_are_removed(self) -> None:
        # 追加一个只有 1KB 的碎片，应该被整组清理
        small = self.source_dir / "录播姬_2026年11月14日12点01分00秒_碎片_别名.flv"
        small.write_bytes(b"x" * 1024)
        small.with_suffix(".xml").write_text(XML, encoding="utf-8")

        with patch.dict(os.environ, self.env):
            ok = process_source_dir(self.config, self.source_dir)

        self.assertTrue(ok)
        job = load_job(
            job_file(self.config.general.data_dir, "demo-2026-11-14_120000")
        )
        self.assertIn("清理 1 个碎片", job.steps["scan"]["detail"])


if __name__ == "__main__":
    unittest.main()
