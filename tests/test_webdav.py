"""WebDAV 弹幕上传测试：路径模板、MKCOL/PUT、Basic Auth、重跑幂等。

用本地http.server 模拟 WebDAV，不访问外网。
"""

from __future__ import annotations

import base64
import logging
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from stream_archive.config import (
    Config,
    GeneralConfig,
    StreamerConfig,
    WebdavConfig,
)
from stream_archive.state import Job
from stream_archive.webdav import (
    month_public_url,
    render_path,
    step_upload_danmaku,
    upload_file,
)

XML_BODY = '<i><d p="1.0,1,25,16777215,0,0,0,0">弹幕</d></i>'


class FakeWebdavHandler(BaseHTTPRequestHandler):
    """最小 WebDAV：记录 MKCOL/PUT，全部返回成功。"""

    def do_MKCOL(self) -> None:  # noqa: N802 - http.server 要求的方法名
        self.server.calls.append(  # type: ignore[attr-defined]
            {"method": "MKCOL", "path": urllib.parse.unquote(self.path), "body": b""}
        )
        self.send_response(201)
        self.end_headers()

    def do_PUT(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.calls.append(  # type: ignore[attr-defined]
            {
                "method": "PUT",
                "path": urllib.parse.unquote(self.path),
                "body": body,
                "auth": self.headers.get("Authorization"),
            }
        )
        self.send_response(201)
        self.end_headers()

    def log_message(self, *args) -> None:
        # 测试里不需要访问日志
        pass


class WebdavTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWebdavHandler)
        self.server.calls = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.dav = WebdavConfig(
            enabled=True,
            url=f"http://127.0.0.1:{self.server.server_address[1]}/dav",
            username="tester",
            password="secret",
            path="/弹幕/{streamer}/{yyyy}/{mm}/{date}",
            public_url="https://pan.example.com/弹幕",
            timeout=10,
        )

        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    # ---- 纯函数 ----
    def test_render_path(self) -> None:
        path = render_path(self.dav.path, "演示主播", "2026年11月14日12点00分00秒")
        self.assertEqual(path, "弹幕/演示主播/2026/11/2026-11-14")

    def test_month_public_url(self) -> None:
        url = month_public_url(self.dav, "演示主播", "2026年11月14日12点00分00秒")
        self.assertEqual(url, "https://pan.example.com/弹幕/演示主播/2026/11")

    def test_month_public_url_disabled(self) -> None:
        self.dav.enabled = False
        self.assertEqual(
            month_public_url(self.dav, "演示主播", "2026年11月14日12点00分00秒"), ""
        )

    # ---- HTTP ----
    def test_upload_file_creates_dirs_and_puts(self) -> None:
        local = self.root / "弹幕.xml"
        local.write_text(XML_BODY, encoding="utf-8")

        upload_file(self.dav, "弹幕/演示主播/2026/11/2026-11-14/弹幕.xml", local)

        calls = self.server.calls  # type: ignore[attr-defined]
        methods = [call["method"] for call in calls]
        self.assertEqual(methods, ["MKCOL"] * 5 + ["PUT"])

        put = calls[-1]
        self.assertTrue(put["path"].startswith("/dav/弹幕/演示主播/2026/11/2026-11-14/"))
        self.assertEqual(put["body"], XML_BODY.encode("utf-8"))

        expected = base64.b64encode(b"tester:secret").decode("ascii")
        self.assertEqual(put["auth"], f"Basic {expected}")

    # ---- 流水线步骤 ----
    def test_step_uploads_all_xml_and_is_idempotent(self) -> None:
        raw_dir = self.root / "videos" / "演示主播" / "原文件"
        raw_dir.mkdir(parents=True)
        names = [
            "录播姬_2026年11月14日12点00分00秒_直播_演示主播.xml",
            "录播姬_2026年11月14日20点00分00秒_直播_演示主播.xml",
        ]
        for name in names:
            (raw_dir / name).write_text(XML_BODY, encoding="utf-8")

        config = Config(
            general=GeneralConfig(
                work_dir=self.root / "work",
                videos_dir=self.root / "videos",
                data_dir=self.root / "data",
                log_dir=self.root / "logs",
            )
        )
        streamer = StreamerConfig(id="demo", name="演示主播", webdav=self.dav)
        job = Job(
            id="demo-1",
            streamer_id="demo",
            platform="录播姬",
            streamer="演示主播",
            stream_title="直播",
            start_time="2026年11月14日12点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work"),
        )
        job.outputs["archive"] = {"raw_dir": str(raw_dir)}

        detail, skipped = step_upload_danmaku(config, streamer, job)
        self.assertFalse(skipped)
        self.assertIn("2/2", detail)

        put_paths = [
            call["path"]
            for call in self.server.calls  # type: ignore[attr-defined]
            if call["method"] == "PUT"
        ]
        self.assertEqual(len(put_paths), 2)
        self.assertTrue(any("2026-11-14" in path for path in put_paths))

        # 重跑不再重复上传
        before = len(self.server.calls)  # type: ignore[attr-defined]
        detail2, skipped2 = step_upload_danmaku(config, streamer, job)
        self.assertTrue(skipped2)
        self.assertIn("已全部上传", detail2)
        self.assertEqual(len(self.server.calls), before)  # type: ignore[attr-defined]

    def test_only_appended_filters_xml(self) -> None:
        raw_dir = self.root / "videos" / "演示主播" / "原文件"
        raw_dir.mkdir(parents=True)
        kept = raw_dir / "录播姬_2026年11月14日12点00分00秒_直播_演示主播.xml"
        ignored = raw_dir / "录播姬_2026年11月15日12点00分00秒_直播_演示主播.xml"
        for path in (kept, ignored):
            path.write_text(XML_BODY, encoding="utf-8")

        dav = WebdavConfig(
            enabled=True,
            url=self.dav.url,
            path=self.dav.path,
            only_appended=True,
        )
        config = Config(
            general=GeneralConfig(
                work_dir=self.root / "work",
                videos_dir=self.root / "videos",
                data_dir=self.root / "data",
                log_dir=self.root / "logs",
            )
        )
        streamer = StreamerConfig(id="demo", name="演示主播", webdav=dav)
        job = Job(
            id="demo-2",
            streamer_id="demo",
            platform="录播姬",
            streamer="演示主播",
            stream_title="直播",
            start_time="2026年11月14日12点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work"),
        )
        job.outputs["archive"] = {"raw_dir": str(raw_dir)}
        job.outputs["append"] = {"done": [kept.with_suffix(".mp4").name]}

        step_upload_danmaku(config, streamer, job)

        put_paths = [
            call["path"]
            for call in self.server.calls  # type: ignore[attr-defined]
            if call["method"] == "PUT"
        ]
        self.assertEqual(len(put_paths), 1)
        self.assertIn(kept.name, put_paths[0])

    def test_disabled_is_skipped(self) -> None:
        streamer = StreamerConfig(id="demo", name="演示主播")
        job = Job(
            id="demo-3",
            streamer_id="demo",
            platform="录播姬",
            streamer="演示主播",
            stream_title="直播",
            start_time="2026年11月14日12点00分00秒",
            source_dir=str(self.root / "src"),
            work_dir=str(self.root / "work"),
        )
        config = Config(general=GeneralConfig(data_dir=self.root / "data"))
        _, skipped = step_upload_danmaku(config, streamer, job)
        self.assertTrue(skipped)


if __name__ == "__main__":
    unittest.main()
