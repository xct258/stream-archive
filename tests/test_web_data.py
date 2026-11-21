"""Web 数据层测试：任务摘要、总览、日志、路径校验、脱敏。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from stream_archive.config import (
    AsrConfig,
    Config,
    GeneralConfig,
    StreamerConfig,
    UploadConfig,
    WatchConfig,
    WebConfig,
    WebdavConfig,
)
from stream_archive.state import STEPS, Job, save_job, write_heartbeat
from stream_archive.web_data import (
    health,
    heartbeat_status,
    job_cover_path,
    job_detail,
    job_log_path,
    job_summary,
    list_job_summaries,
    log_path,
    masked_config,
    overview,
    scan_recordings,
    tail_text,
)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class WebDataTest(unittest.TestCase):
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
            watch=WatchConfig(
                source_folders=[self.root / "rec"],
                poll_seconds=60,
                idle_minutes=20,
            ),
            web=WebConfig(enabled=True, refresh_seconds=5),
            streamers=[
                StreamerConfig(
                    id="demo",
                    name="主播",
                    aliases=["昵称"],
                    upload=UploadConfig(
                        enabled=True,
                        cookies=Path("/secret/cookies.json"),
                    ),
                    webdav=WebdavConfig(enabled=True, password="SECRET_PASS"),
                    asr=AsrConfig(enabled=True, model="small"),
                )
            ],
        )
        for directory in (
            self.config.general.work_dir,
            self.config.general.videos_dir,
            self.config.general.data_dir,
            self.config.general.log_dir,
            self.config.watch.source_folders[0],
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _job(
        self,
        job_id: str,
        statuses: dict[str, str],
        outputs: dict | None = None,
        created_at: str | None = None,
    ) -> Job:
        job = Job(
            id=job_id,
            streamer_id="demo",
            platform="录播姬",
            streamer="主播",
            stream_title="测试直播",
            start_time="2026年11月15日20点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work" / job_id),
            created_at=created_at or now_text(),
        )
        for step in STEPS:
            status = statuses.get(step, "done")
            job.steps[step] = {
                "status": status,
                "started_at": job.created_at,
                "finished_at": job.created_at if status in ("done", "skipped") else None,
                "detail": f"{step} {status}",
            }
        job.outputs = outputs or {}
        save_job(self.config.general.data_dir, job)
        return job

    # ---- 任务摘要 ----
    def test_job_summary_done(self) -> None:
        job = self._job(
            "demo-done",
            {},
            outputs={
                "upload": {"ok": True, "detail": "投稿成功 BV1AAAAAAAAA"},
                "asr": {"done": ["a.mp4"]},
                "cover": {"path": str(self.root / "cover.jpg")},
            },
        )
        summary = job_summary(job.to_dict())

        self.assertEqual(summary["status"], "done")
        self.assertEqual(summary["progress"], 100)
        self.assertEqual(summary["video_id"], "BV1AAAAAAAAA")
        self.assertEqual(summary["asr_done"], 1)
        self.assertEqual(summary["current_step"], "")
        self.assertTrue(summary["upload_ok"])

    def test_job_summary_failed_and_running(self) -> None:
        failed = self._job("demo-failed", {"encode": "failed"})
        self.assertEqual(job_summary(failed.to_dict())["status"], "failed")

        running = self._job("demo-running", {"scan": "done", "remux": "running"})
        summary = job_summary(running.to_dict())
        self.assertEqual(summary["status"], "running")
        self.assertEqual(summary["current_step"], "remux")

    def test_job_summary_step_progress(self) -> None:
        job = self._job("demo-step", {"scan": "done", "encode": "running"})
        job.steps["encode"]["progress"] = {
            "percent": 42.5,
            "detail": "1:00:00 / 2:00:00，速度 0.9x",
            "speed": "0.9x",
            "eta_seconds": 4800,
        }
        summary = job_summary(job.to_dict())

        self.assertEqual(summary["current_step"], "encode")
        self.assertEqual(summary["current_step_progress"]["percent"], 42.5)
        self.assertEqual(summary["current_step_progress"]["speed"], "0.9x")
        self.assertEqual(summary["current_step_progress"]["eta_seconds"], 4800)
        # 总进度仍保留，供兼容和整体判断
        self.assertEqual(summary["progress"], int((len(STEPS) - 1) / len(STEPS) * 100))

    def test_job_detail_step_progress(self) -> None:
        job = self._job("demo-step-detail", {"encode": "running"})
        job.steps["encode"]["progress"] = {"percent": 12.0, "detail": "压制中"}
        detail = job_detail(job.to_dict())
        encode = next(step for step in detail["steps"] if step["name"] == "encode")
        self.assertEqual(encode["progress"]["percent"], 12.0)
        self.assertEqual(encode["progress"]["detail"], "压制中")

    def test_running_job_elapsed_keeps_growing(self) -> None:
        from datetime import datetime, timedelta

        now = datetime.now()
        created = (now - timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
        finished = (now - timedelta(seconds=240)).strftime("%Y-%m-%d %H:%M:%S")

        job = self._job("demo-elapsed", {"scan": "done", "encode": "running"}, created_at=created)
        job.steps["scan"]["finished_at"] = finished

        summary = job_summary(job.to_dict())
        # 运行中：按“现在”计算，而不是停在 scan 的完成时间
        self.assertGreaterEqual(summary["elapsed_seconds"], 280)

        # 完成/失败：定格在最后一步
        done = self._job("demo-elapsed-done", {})
        done.created_at = created
        for step in STEPS:
            done.steps[step]["finished_at"] = finished
        self.assertEqual(job_summary(done.to_dict())["elapsed_seconds"], 60)

    def test_old_job_without_steps_expected(self) -> None:
        # 旧版本状态文件没有 steps_expected，只按实际步骤统计，不能被新步骤拖成 pending
        steps = {
            step: {"status": "done", "finished_at": "2026-11-14 10:00:00"}
            for step in STEPS[:-1]
        }
        raw = {
            "id": "old-job",
            "streamer": "主播",
            "stream_title": "旧任务",
            "created_at": "2026-11-14 09:00:00",
            "steps": steps,
            "outputs": {},
        }
        summary = job_summary(raw)
        self.assertEqual(summary["status"], "done")
        self.assertEqual(summary["progress"], 100)
        self.assertEqual(len(summary["steps_expected"]), len(STEPS) - 1)

    def test_list_job_summaries_sorted(self) -> None:
        self._job("old", {}, created_at="2026-11-14 10:00:00")
        self._job("new", {}, created_at="2026-11-15 10:00:00")

        jobs = list_job_summaries(self.config)
        self.assertEqual([job["id"] for job in jobs], ["new", "old"])

    def test_job_detail(self) -> None:
        pressed = self.root / "videos" / "主播" / "压制版"
        raw = self.root / "videos" / "主播" / "原文件"
        pressed.mkdir(parents=True)
        raw.mkdir(parents=True)
        (pressed / "a.mp4").write_bytes(b"x")
        (raw / "a.xml").write_text("<i/>", encoding="utf-8")

        job = self._job(
            "demo-detail",
            {},
            outputs={
                "archive": {"pressed_dir": str(pressed), "raw_dir": str(raw)},
                "asr": {
                    "done": ["a.mp4"],
                    "files": [{"video": "a.mp4", "txt": "/x/a.txt", "srt": "/x/a.srt"}],
                },
            },
        )
        detail = job_detail(job.to_dict())

        self.assertEqual(len(detail["steps"]), len(STEPS))
        self.assertEqual(len(detail["files"]), 2)
        self.assertEqual(detail["asr_files"][0]["video"], "a.mp4")

    # ---- 总览 ----
    def test_overview_and_heartbeat(self) -> None:
        self._job("demo-1", {})
        write_heartbeat(
            self.config.general.data_dir,
            {
                "updated_ts": time.time(),
                "last_scan": now_text(),
                "directories": 2,
                "pid": os.getpid(),
            },
        )

        data = overview(self.config)
        self.assertEqual(data["server_name"], "test-nas")
        self.assertTrue(data["watcher"]["alive"])
        self.assertEqual(data["jobs"]["total"], 1)
        self.assertIsNotNone(data["disk"])

        write_heartbeat(
            self.config.general.data_dir,
            {"updated_ts": time.time() - 3600, "last_scan": "old"},
        )
        self.assertFalse(heartbeat_status(self.config)["alive"])

    def test_scan_recordings(self) -> None:
        directory = self.config.watch.source_folders[0] / "昵称" / "2026-11-15"
        directory.mkdir(parents=True)
        video = directory / "a.flv"
        video.write_bytes(b"x" * 1024)
        old = time.time() - 3600
        os.utime(video, (old, old))

        recordings = scan_recordings(self.config)
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0]["status"], "idle")
        self.assertEqual(recordings[0]["idle_seconds"], 1200)
        self.assertEqual(recordings[0]["remaining_seconds"], 0)

        os.utime(video, (time.time(), time.time()))
        recordings = scan_recordings(self.config)
        self.assertEqual(recordings[0]["status"], "recording")
        self.assertEqual(recordings[0]["idle_seconds"], 1200)
        self.assertGreater(recordings[0]["remaining_seconds"], 0)

    # ---- 日志 ----
    def test_tail_text(self) -> None:
        path = self.root / "test.log"
        path.write_text("\n".join(f"line-{i}" for i in range(1, 1001)), encoding="utf-8")
        tail = tail_text(path, lines=10)
        self.assertIn("line-1000", tail)
        self.assertNotIn("line-900", tail)

    def test_log_path_whitelist(self) -> None:
        self.assertIsNotNone(log_path(self.config, "main"))
        self.assertIsNotNone(log_path(self.config, "watcher"))
        self.assertIsNone(log_path(self.config, "../../etc/passwd"))

        self.assertIsNotNone(job_log_path(self.config, "demo-1"))
        self.assertIsNone(job_log_path(self.config, "../x"))

    # ---- 封面 ----
    def test_job_cover_path(self) -> None:
        cover = self.config.general.videos_dir / "主播" / "cover.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"fake")

        self._job("demo-cover", {}, outputs={"cover": {"path": str(cover)}})
        self.assertEqual(job_cover_path(self.config, "demo-cover"), cover)

        self._job("demo-bad-cover", {}, outputs={"cover": {"path": "/etc/passwd"}})
        self.assertIsNone(job_cover_path(self.config, "demo-bad-cover"))

    # ---- 脱敏 ----
    def test_masked_config(self) -> None:
        text = json.dumps(masked_config(self.config), ensure_ascii=False)
        self.assertIn("test-nas", text)
        self.assertIn("主播", text)
        self.assertNotIn("SECRET_PASS", text)
        self.assertNotIn("/secret/cookies.json", text)
        self.assertNotIn("cookies", text)

    def test_health(self) -> None:
        data = health(self.config)
        self.assertIn(data["status"], ("ok", "degraded"))
        self.assertEqual(data["refresh_seconds"], 5)


if __name__ == "__main__":
    unittest.main()
