"""B 站审核状态查询测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_archive.config import Config, GeneralConfig, StreamerConfig, UploadConfig
from stream_archive.review import (
    ReviewState,
    classify_state,
    fetch_archive_state,
    find_bvid,
    refresh_pending_reviews,
    wait_for_review,
)
from stream_archive.state import Job, job_file, load_job, save_job


def write_cookie(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "cookie_info": {
                    "cookies": [
                        {"name": "SESSDATA", "value": "abc"},
                        {"name": "bili_jct", "value": "def"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


class ReviewTest(unittest.TestCase):
    def test_classify_state(self) -> None:
        self.assertEqual(classify_state(0)[0], "published")
        self.assertEqual(classify_state(-30)[0], "reviewing")
        self.assertEqual(classify_state(-2)[0], "rejected")
        self.assertEqual(classify_state(-50)[0], "self_only")
        self.assertEqual(classify_state(-100)[0], "rejected")
        self.assertEqual(classify_state(123)[0], "unknown")
        self.assertEqual(classify_state(None)[0], "not_found")

    def test_find_bvid(self) -> None:
        self.assertEqual(
            find_bvid("投稿成功 BV1HcsE6QE9y 谢谢"), "BV1HcsE6QE9y"
        )
        self.assertIsNone(find_bvid("没有稿件号"))

    def test_fetch_archive_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cookies = Path(temp) / "cookies.json"
            write_cookie(cookies)

            payload = {
                "code": 0,
                "data": {
                    "arc_audits": [
                        {
                            "Archive": {
                                "bvid": "BV1HcsE6QE9y",
                                "state": -30,
                                "title": "2026年11月14日 [暗区科科币tv]",
                            }
                        }
                    ]
                },
            }
            with patch("stream_archive.review._request_json", return_value=payload):
                state = fetch_archive_state(cookies, "BV1HcsE6QE9y")

        self.assertEqual(state.kind, "reviewing")
        self.assertEqual(state.state, -30)
        self.assertEqual(state.title, "2026年11月14日 [暗区科科币tv]")

    def test_fetch_rejected_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cookies = Path(temp) / "cookies.json"
            write_cookie(cookies)
            payload = {
                "code": 0,
                "data": {
                    "arc_audits": [
                        {
                            "Archive": {
                                "bvid": "BV1TUMNzeE5F",
                                "state": -2,
                                "state_desc": "退回仅自见",
                                "title": "回放",
                                "reject_reason": "该视频【P1(02:42:02-02:43:37)】不予审核通过",
                                "problem_description_title": "关于法律法规",
                                "problem_description": "禁止发布违法内容",
                                "modify_advise": "建议删除或修改片段",
                                "reject_reason_url": "http://member.bilibili.com/studio/convention/",
                            }
                        }
                    ]
                },
            }
            with patch("stream_archive.review._request_json", return_value=payload):
                state = fetch_archive_state(cookies, "BV1TUMNzeE5F")

        self.assertEqual(state.kind, "rejected")
        self.assertIn("审核退回", state.message)
        self.assertIn("02:42:02", state.reject_reason)
        self.assertEqual(state.problem_title, "关于法律法规")
        self.assertEqual(state.modify_advise, "建议删除或修改片段")
        self.assertTrue(state.reject_reason_url.startswith("http"))

    def test_fetch_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cookies = Path(temp) / "cookies.json"
            write_cookie(cookies)
            with patch(
                "stream_archive.review._request_json",
                return_value={"code": 0, "data": {"arc_audits": []}},
            ):
                state = fetch_archive_state(cookies, "BV1HcsE6QE9y")

        self.assertEqual(state.kind, "not_found")

    def test_fetch_bad_cookies(self) -> None:
        state = fetch_archive_state(Path("/nonexistent/cookies.json"), "BV1HcsE6QE9y")
        self.assertEqual(state.kind, "error")

    def test_wait_stops_on_published(self) -> None:
        reviewing = ReviewState("BV1HcsE6QE9y", -30, "reviewing", "审核中")
        published = ReviewState("BV1HcsE6QE9y", 0, "published", "已公开")
        updates = []

        with (
            patch(
                "stream_archive.review.fetch_archive_state",
                side_effect=[reviewing, published],
            ),
            patch("stream_archive.review.time.sleep"),
        ):
            state = wait_for_review(
                Path("/unused"),
                "BV1HcsE6QE9y",
                timeout=3600,
                interval=10,
                on_update=updates.append,
            )

        self.assertEqual(state.kind, "published")
        self.assertEqual(len(updates), 2)

    def test_refresh_pending_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            cookies = data / "cookies.json"
            write_cookie(cookies)

            config = Config(general=GeneralConfig(data_dir=data))
            config.streamers = [
                StreamerConfig(
                    id="gaojidong",
                    name="高机动持盾军官",
                    upload=UploadConfig(cookies=cookies),
                )
            ]

            job = Job(
                id="j1",
                streamer_id="gaojidong",
                platform="录播姬",
                streamer="高机动持盾军官",
                stream_title="测试",
                start_time="2026-11-14 19:48:08",
                source_dir="",
                work_dir="",
            )
            job.outputs["upload"] = {
                "ok": True,
                "bvid": "BV1HcsE6QE9y",
                "detail": "投稿成功 BV1HcsE6QE9y",
            }
            save_job(data, job)

            published = ReviewState(
                "BV1HcsE6QE9y", 0, "published", "已通过审核，公开可见"
            )
            with patch(
                "stream_archive.review.fetch_archive_state", return_value=published
            ) as fetch:
                first = refresh_pending_reviews(config)
                second = refresh_pending_reviews(config)

            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            self.assertEqual(fetch.call_count, 1)

            loaded = load_job(job_file(data, "j1"))
            self.assertEqual(loaded.outputs["review"]["kind"], "published")

    def test_wait_stops_on_rejected(self) -> None:
        rejected = ReviewState("BV1HcsE6QE9y", -100, "rejected", "未通过")
        with (
            patch(
                "stream_archive.review.fetch_archive_state",
                return_value=rejected,
            ),
            patch("stream_archive.review.time.sleep"),
        ):
            state = wait_for_review(
                Path("/unused"), "BV1HcsE6QE9y", timeout=3600, interval=10
            )
        self.assertEqual(state.kind, "rejected")


if __name__ == "__main__":
    unittest.main()
