"""录播姬配置生成/更新测试。"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from stream_archive.config import (
    Config,
    GeneralConfig,
    RecorderConfig,
    StreamerConfig,
)
from stream_archive.recorder import sync_recorder_config


def write_cookie(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "cookie_info": {
                    "cookies": [
                        {"name": "SESSDATA", "value": "sess"},
                        {"name": "bili_jct", "value": "jct"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


class RecorderConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cookie = self.root / "cookie.json"
        write_cookie(self.cookie)
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self.temp_dir.cleanup()

    def _config(self, **recorder) -> Config:
        params = {
            "work_dir": self.root / "rec",
            "record_cookie_json": self.cookie,
        }
        params.update(recorder)
        return Config(
            general=GeneralConfig(data_dir=self.root / "state"),
            recorder=RecorderConfig(**params),
            streamers=[
                StreamerConfig(id="a", name="主播A", room_id=111),
                StreamerConfig(id="b", name="主播B", room_id=222),
            ],
        )

    def test_creates_config_from_toml(self) -> None:
        config = self._config()
        changed = sync_recorder_config(config)

        self.assertTrue(changed)
        path = config.recorder.config_path
        self.assertTrue(path.is_file())

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 3)
        self.assertTrue(data["global"]["RecordDanmaku"]["Value"])
        self.assertTrue(data["global"]["RecordDanmakuGift"]["Value"])
        self.assertIn("video/", data["global"]["FileNameRecordTemplate"]["Value"])
        self.assertEqual(
            data["global"]["Cookie"]["Value"], "SESSDATA=sess; bili_jct=jct"
        )

        room_ids = {room["RoomId"]["Value"] for room in data["rooms"]}
        self.assertEqual(room_ids, {111, 222})
        self.assertTrue(all(room["AutoRecord"]["Value"] for room in data["rooms"]))

    def test_preserves_existing_rooms(self) -> None:
        config = self._config()
        sync_recorder_config(config)

        path = config.recorder.config_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["rooms"].append(
            {
                "RoomId": {"HasValue": True, "Value": 333},
                "AutoRecord": {"HasValue": True, "Value": False},
            }
        )
        path.write_text(json.dumps(data), encoding="utf-8")

        sync_recorder_config(config)
        room_ids = {
            room["RoomId"]["Value"]
            for room in json.loads(path.read_text(encoding="utf-8"))["rooms"]
        }
        self.assertIn(333, room_ids)
        self.assertEqual(room_ids, {111, 222, 333})

    def test_missing_cookie_still_creates_config(self) -> None:
        config = self._config(record_cookie_json=self.root / "missing.json")
        changed = sync_recorder_config(config)

        self.assertTrue(changed)
        data = json.loads(config.recorder.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["global"]["Cookie"]["Value"], "")
        self.assertEqual(len(data["rooms"]), 2)

    def test_disabled_does_nothing(self) -> None:
        config = self._config(enabled=False)
        changed = sync_recorder_config(config)

        self.assertFalse(changed)
        self.assertFalse(config.recorder.config_path.exists())


if __name__ == "__main__":
    unittest.main()
