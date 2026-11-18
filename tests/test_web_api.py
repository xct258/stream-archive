"""Web API 测试（FastAPI TestClient）。

如果环境里没有 FastAPI/httpx，整组测试自动跳过。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from stream_archive.config import (
    Config,
    GeneralConfig,
    StreamerConfig,
    WatchConfig,
    WebConfig,
)
from stream_archive.state import STEPS, Job, now_text, save_job, write_heartbeat

try:
    from fastapi.testclient import TestClient

    from stream_archive.web import create_app

    HAS_WEB = True
except Exception:  # pragma: no cover - 没装 FastAPI 的环境
    HAS_WEB = False


@unittest.skipUnless(HAS_WEB, "需要 FastAPI + httpx")
class WebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.config = Config(
            general=GeneralConfig(
                server_name="test-nas",
                work_dir=self.root / "work",
                videos_dir=self.root / "videos",
                data_dir=self.root / "state",
                log_dir=self.root / "logs",
            ),
            watch=WatchConfig(source_folders=[self.root / "rec"], poll_seconds=60),
            web=WebConfig(enabled=True, refresh_seconds=5),
            streamers=[StreamerConfig(id="demo", name="主播")],
        )
        for directory in (
            self.config.general.work_dir,
            self.config.general.videos_dir,
            self.config.general.data_dir,
            self.config.general.log_dir,
            self.config.general.log_dir / "jobs",
            self.config.general.log_dir / "service",
            self.config.watch.source_folders[0],
        ):
            directory.mkdir(parents=True, exist_ok=True)

        cover = self.config.general.videos_dir / "主播" / "cover.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"fake-jpeg")

        job = Job(
            id="demo-1",
            streamer_id="demo",
            platform="录播姬",
            streamer="主播",
            stream_title="测试直播",
            start_time="2026年11月15日20点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work" / "demo-1"),
        )
        for step in STEPS:
            job.steps[step] = {
                "status": "done",
                "started_at": now_text(),
                "finished_at": now_text(),
                "detail": "ok",
            }
        job.outputs = {
            "cover": {"path": str(cover)},
            "upload": {"ok": True, "title": "测试", "detail": "投稿成功 BV1AAAAAAAAA"},
        }
        save_job(self.config.general.data_dir, job)

        (self.config.general.log_dir / "stream-archive.log").write_text(
            "line-1\nline-2\nline-3\n", encoding="utf-8"
        )
        (self.config.general.log_dir / "jobs" / "demo-1.log").write_text(
            "job-log-1\njob-log-2\n", encoding="utf-8"
        )
        (self.config.general.log_dir / "service" / "watcher.log").write_text(
            "watcher\n", encoding="utf-8"
        )
        write_heartbeat(
            self.config.general.data_dir,
            {"updated_ts": time.time(), "last_scan": now_text(), "directories": 1},
        )

        self.client = TestClient(create_app(self.config))

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_index(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("stream-archive", response.text)

    def test_cache_headers(self) -> None:
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertEqual(index.headers.get("cache-control"), "no-store")

        asset = self.client.get("/assets/app.js")
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.headers.get("cache-control"), "no-cache")

    def test_health(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["watcher"]["alive"], True)

    def test_overview(self) -> None:
        data = self.client.get("/api/overview").json()
        self.assertEqual(data["server_name"], "test-nas")
        self.assertEqual(data["jobs"]["total"], 1)
        self.assertIn("recordings", data)

    def test_jobs_and_filters(self) -> None:
        data = self.client.get("/api/jobs").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["jobs"][0]["id"], "demo-1")

        empty = self.client.get("/api/jobs?status=failed").json()
        self.assertEqual(empty["total"], 0)

        detail = self.client.get("/api/jobs/demo-1").json()
        self.assertEqual(len(detail["steps"]), len(STEPS))
        self.assertIn("job-log-1", detail["log_tail"])

        missing = self.client.get("/api/jobs/bad!id")
        self.assertEqual(missing.status_code, 404)

    def test_job_log_and_cover(self) -> None:
        log = self.client.get("/api/jobs/demo-1/log?tail=10")
        self.assertEqual(log.status_code, 200)
        self.assertIn("job-log-2", log.json()["content"])

        cover = self.client.get("/api/jobs/demo-1/cover")
        self.assertEqual(cover.status_code, 200)
        self.assertEqual(cover.headers["content-type"], "image/jpeg")

    def test_logs(self) -> None:
        listing = self.client.get("/api/logs").json()["logs"]
        names = {item["name"] for item in listing}
        self.assertTrue({"main", "watcher"} <= names)

        main = self.client.get("/api/logs/main?tail=10")
        self.assertEqual(main.status_code, 200)
        self.assertIn("line-3", main.json()["content"])

        missing = self.client.get("/api/logs/../../etc/passwd")
        self.assertEqual(missing.status_code, 404)

    def test_config_is_masked(self) -> None:
        text = self.client.get("/api/config").text
        self.assertIn("test-nas", text)
        self.assertNotIn("password", text.lower())
        self.assertNotIn("cookies", text.lower())


if __name__ == "__main__":
    unittest.main()
