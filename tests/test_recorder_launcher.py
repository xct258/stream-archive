"""录播姬启动器测试：命令组装、密码文件、配置生成。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from stream_archive.config import (
    Config,
    GeneralConfig,
    RecorderConfig,
    StreamerConfig,
)
from stream_archive.recorder_launcher import (
    RecorderLaunchError,
    build_command,
    ensure_password,
)


def write_cookie(path: Path) -> None:
    path.write_text(
        json.dumps({"cookie_info": {"cookies": [{"name": "SESSDATA", "value": "x"}]}}),
        encoding="utf-8",
    )


class RecorderLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cookie = self.root / "cookie.json"
        write_cookie(self.cookie)

        self.binary = self.root / "BililiveRecorder.Cli"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binary.chmod(0o755)

        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self.temp_dir.cleanup()

    def _config(self, **recorder) -> Config:
        params = {
            "work_dir": self.root / "rec",
            "record_cookie_json": self.cookie,
            "http_bind": "http://127.0.0.1:2356",
            "http_user": "admin",
        }
        params.update(recorder)
        return Config(
            general=GeneralConfig(data_dir=self.root / "state"),
            recorder=RecorderConfig(**params),
            streamers=[StreamerConfig(id="a", name="主播A", room_id=111)],
        )

    def test_build_command(self) -> None:
        config = self._config()
        command, password_file = build_command(config, binary=self.binary)

        self.assertEqual(command[0], str(self.binary))
        self.assertEqual(command[1], "run")
        self.assertEqual(command[2], str(config.recorder.work_dir))
        self.assertIn("--bind", command)
        self.assertIn("http://127.0.0.1:2356", command)
        self.assertIn("--http-basic-user", command)
        self.assertIn("admin", command)

        self.assertIsNotNone(password_file)
        self.assertTrue(password_file.is_file())  # type: ignore[union-attr]
        self.assertGreater(len(password_file.read_text().strip()), 10)  # type: ignore[union-attr]
        self.assertTrue(config.recorder.config_path.is_file())

    def test_configured_password_has_no_file(self) -> None:
        config = self._config(http_password="secret")
        command, password_file = build_command(config, binary=self.binary)

        self.assertIsNone(password_file)
        self.assertIn("secret", command)
        self.assertFalse((config.recorder.work_dir / ".http_password").exists())

    def test_password_is_stable(self) -> None:
        config = self._config()
        first, first_file = ensure_password(config)
        second, second_file = ensure_password(config)
        self.assertEqual(first, second)
        self.assertEqual(first_file, second_file)

    def test_missing_binary(self) -> None:
        config = self._config()
        with self.assertRaises(RecorderLaunchError):
            build_command(config, binary=self.root / "missing")

    def test_disabled(self) -> None:
        config = self._config(enabled=False)
        with self.assertRaises(RecorderLaunchError):
            build_command(config, binary=self.binary)


if __name__ == "__main__":
    unittest.main()
