"""配置解析测试：多主播、默认值继承、错误提示。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stream_archive.config import ConfigError, load_config

CONFIG_TEMPLATE = """
[general]
server_name = "test"
work_dir = "{root}/work"
videos_dir = "{root}/videos"
data_dir = "{root}/data"
log_dir = "{root}/logs"

[watch]
source_folders = ["{root}/rec"]
poll_seconds = 60

[danmaku]
max_diff_seconds = 25

[archive]
retention_days = 15

[append]
source = "raw"

[webdav]
enabled = true
url = "https://pan.example.com/dav"
path = "/弹幕/{streamer}/{date}"

[asr]
enabled = false
server = "http://127.0.0.1:8286"

[[streamers]]
id = "a"
name = "主播A"
aliases = ["昵称A"]
room_id = 1001

[streamers.danmaku]
max_diff_seconds = 10

[streamers.encode]
progress_bar = true

[streamers.append]
enabled = true

[streamers.webdav]
only_appended = true

[streamers.asr]
enabled = true

[[streamers]]
id = "b"
name = "主播B"
aliases = ["昵称B"]
enabled = false
"""


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "config.toml"
        self.path.write_text(CONFIG_TEMPLATE.replace("{root}", str(self.root)), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loads_multiple_streamers(self) -> None:
        config = load_config(self.path)

        self.assertEqual([streamer.id for streamer in config.streamers], ["a", "b"])
        self.assertEqual(config.general.server_name, "test")

    def test_streamer_inherits_and_overrides_defaults(self) -> None:
        config = load_config(self.path)
        a = config.streamer_by_id("a")
        b = config.streamer_by_id("b")

        assert a is not None and b is not None
        # a 覆盖了弹幕时间差，b 继承全局默认
        self.assertEqual(a.danmaku.max_diff_seconds, 10)
        self.assertEqual(b.danmaku.max_diff_seconds, 25)
        self.assertEqual(a.archive.retention_days, 15)
        self.assertFalse(b.enabled)

        # 按月投稿：a 覆盖打开，b 继承全局默认（关闭）
        self.assertTrue(a.append.enabled)
        self.assertFalse(b.append.enabled)
        self.assertEqual(a.append.source, "raw")
        self.assertIn("{month_cn}", a.append.title_template)

        # WebDAV：全局开启，a 单独设为只传已追加的 XML
        self.assertTrue(a.webdav.enabled)
        self.assertTrue(a.webdav.only_appended)
        self.assertFalse(b.webdav.only_appended)
        self.assertEqual(a.webdav.url, "https://pan.example.com/dav")

        # 语音转文本：a 开启，b 继承全局默认（关闭）
        self.assertTrue(a.asr.enabled)
        self.assertFalse(b.asr.enabled)
        self.assertEqual(a.asr.server, "http://127.0.0.1:8286")

        # 高能进度条：a 开启，b 继承全局默认（关闭）
        self.assertTrue(a.encode.progress_bar)
        self.assertFalse(b.encode.progress_bar)
        self.assertEqual(a.encode.progress_bar_color, "0x2ED6FF")

    def test_matching_and_source_folders(self) -> None:
        config = load_config(self.path)
        a = config.streamer_by_id("a")
        assert a is not None

        self.assertTrue(a.matches("昵称A"))
        self.assertTrue(a.matches("主播A"))
        self.assertTrue(a.matches("a"))
        self.assertFalse(a.matches("昵称B"))
        # 主播没有单独声明 source_folders 时使用全局
        self.assertEqual(config.source_folders_of(a), config.watch.source_folders)

    def test_requires_streamers(self) -> None:
        self.path.write_text(
            CONFIG_TEMPLATE.replace("{root}", str(self.root)).split("[[streamers]]")[0],
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_rejects_bad_type(self) -> None:
        text = CONFIG_TEMPLATE.replace("{root}", str(self.root)).replace(
            "poll_seconds = 60", "poll_seconds = \"60\""
        )
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)


if __name__ == "__main__":
    unittest.main()
