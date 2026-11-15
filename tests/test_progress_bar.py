"""高能进度条测试：密度、颜色、条带帧、ffmpeg 滤镜。

不需要真实 ffmpeg：编码函数会被替换成写空文件。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_archive.progress_bar import (
    BLUE,
    GRAY,
    GREEN,
    RED,
    BarImages,
    build_filter,
    build_images,
    density_series,
    highlight_kinds,
    load_danmaku,
    render_frames,
)


def write_xml(
    path: Path,
    counts: dict[int, int],
    sc: list[float] | None = None,
    guard: list[float] | None = None,
) -> None:
    elements = []
    for second, count in counts.items():
        for index in range(count):
            elements.append(
                f'<d p="{second}.{index:03d},1,25,16777215,0,0,0,0">弹幕</d>'
            )
    for timestamp in sc or []:
        elements.append(f'<sc ts="{timestamp}" user="a" uid="1">SC</sc>')
    for timestamp in guard or []:
        elements.append(f'<guard ts="{timestamp}" user="b" uid="2" level="3" count="1"/>')
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<i>\n'
        + "\n".join(elements)
        + "\n</i>\n",
        encoding="utf-8",
    )


def column_colors(frame: bytes, x: int, width: int, height: int) -> set[tuple[int, int, int]]:
    colors = set()
    for y in range(height):
        offset = (y * width + x) * 4
        if frame[offset + 3]:
            colors.add(tuple(frame[offset : offset + 3]))
    return colors


class ProgressBarTest(unittest.TestCase):
    def test_density_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            xml = Path(temp) / "a.xml"
            write_xml(xml, {5: 10, 8: 1})
            series = density_series(xml, duration=10, smooth_seconds=3)

        self.assertEqual(len(series), 11)
        self.assertAlmostEqual(max(series), 1.0)
        self.assertGreater(series[5], series[8])
        self.assertGreater(series[8], series[0])

    def test_load_highlights(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            xml = Path(temp) / "a.xml"
            write_xml(xml, {1: 1}, sc=[10.5], guard=[20.0])
            bullets, sc, guard = load_danmaku(xml)

        self.assertEqual(len(bullets), 1)
        self.assertEqual(sc, [10.5])
        self.assertEqual(guard, [20.0])

    def test_highlight_kinds_priority(self) -> None:
        kinds = highlight_kinds(60, sc_times=[10.0], guard_times=[10.0, 30.0])
        # SC ±30s 覆盖 0~40，舰长只在 0~60，SC 优先
        self.assertEqual(kinds[10], 1)
        self.assertEqual(kinds[40], 1)
        self.assertEqual(kinds[0], 1)
        # 舰长 30 ±30 覆盖 0~60，但 SC 优先的区域仍是 1
        self.assertEqual(kinds[50], 2)

    def test_render_frames_played_green_unplayed_gray(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            xml = Path(temp) / "a.xml"
            write_xml(xml, {5: 20})
            frames = list(render_frames(xml, duration=10, width=100, height=20, smooth_seconds=2))

        self.assertEqual(len(frames), 10)
        first = frames[0]
        last = frames[-1]
        # 第一帧全部未播放，应该是灰色
        self.assertEqual(column_colors(first, 10, 100, 20), {GRAY})
        # 最后一帧左侧已播放变绿，右侧仍是灰色
        self.assertEqual(column_colors(last, 10, 100, 20), {GREEN})
        self.assertEqual(column_colors(last, 95, 100, 20), {GRAY})

    def test_render_frames_sc_red_guard_blue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            xml = Path(temp) / "a.xml"
            write_xml(xml, {5: 5}, sc=[10.0], guard=[80.0])
            frames = list(render_frames(xml, duration=100, width=100, height=20, smooth_seconds=2))

        first = frames[0]
        # x=10 -> 10 秒，SC 红色
        self.assertEqual(column_colors(first, 10, 100, 20), {RED})
        # x=80 -> 80 秒，舰长蓝色
        self.assertEqual(column_colors(first, 80, 100, 20), {BLUE})

    def test_truncated_xml_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            xml = Path(temp) / "a.xml"
            xml.write_text(
                '<?xml version="1.0"?>\n<i>\n<d p="1.0,1,25,0,0,0,0,0">x</d>\n',
                encoding="utf-8",
            )
            bullets, _sc, _guard = load_danmaku(xml)

        self.assertEqual(len(bullets), 1)

    def test_build_filter(self) -> None:
        text = build_filter(
            duration=3600,
            opacity=0.5,
            ass_path=Path("/tmp/弹幕.ass"),
        )
        self.assertIn("format=rgba", text)
        self.assertIn("overlay=0:H-h:eof_action=repeat", text)
        self.assertIn("ass=", text)
        self.assertNotIn("colorchannelmixer", text)
        self.assertNotIn("overlay=x=", text)

    def test_build_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xml = root / "a.xml"
            write_xml(xml, {5: 5})

            def fake_encode(frames, width, height, output: Path) -> Path:
                for _ in frames:
                    pass
                output.write_bytes(b"fake")
                return output

            with patch("stream_archive.progress_bar._encode_bar", side_effect=fake_encode):
                images = build_images(
                    xml,
                    duration=10,
                    width=100,
                    height=20,
                    color="0x2ED6FF",
                    smooth_seconds=2,
                    directory=root,
                    stem="bar",
                )

            self.assertIsInstance(images, BarImages)
            self.assertTrue(images.bar.is_file())
            self.assertTrue(images.bar.name.endswith("-bar.mkv"))
            self.assertEqual(len(images.paths()), 1)


if __name__ == "__main__":
    unittest.main()
