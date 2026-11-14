"""DanmakuFactory 调用测试：字体参数与输出检查。

用假 DanmakuFactory 记录收到的参数，不需要真实二进制。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_archive.config import DanmakuConfig
from stream_archive.danmaku import xml_to_ass

FAKE_FACTORY = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
output = args[args.index("-o") + 1]
with open(output, "w", encoding="utf-8") as handle:
    handle.write("[Script Info]\\n")

with open(os.environ["ARGS_LOG"], "w", encoding="utf-8") as handle:
    handle.write(" ".join(args))
"""


class DanmakuTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.factory = self.root / "DanmakuFactory"
        self.factory.write_text(FAKE_FACTORY, encoding="utf-8")
        self.factory.chmod(0o755)

        self.xml = self.root / "demo.xml"
        self.xml.write_text("<i></i>", encoding="utf-8")

        self.args_log = self.root / "args.log"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_passes_font_name(self) -> None:
        config = DanmakuConfig(
            factory=self.factory,
            font_name="Noto Sans CJK SC",
            font_size=50,
            opacity=200,
        )
        with patch.dict(os.environ, {"ARGS_LOG": str(self.args_log)}):
            ass = xml_to_ass(self.xml, config)

        self.assertTrue(ass.is_file())
        args = self.args_log.read_text(encoding="utf-8")
        self.assertIn("-S 50", args)
        self.assertIn("-O 200", args)
        self.assertIn("-N Noto Sans CJK SC", args)

    def test_empty_font_name_omits_flag(self) -> None:
        config = DanmakuConfig(
            factory=self.factory, font_name="", font_size=50, opacity=200
        )
        with patch.dict(os.environ, {"ARGS_LOG": str(self.args_log)}):
            xml_to_ass(self.xml, config)

        args = self.args_log.read_text(encoding="utf-8")
        self.assertNotIn("-N", args)


if __name__ == "__main__":
    unittest.main()
