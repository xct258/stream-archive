"""高能进度条测试：颜色、密度、PPM 图片、ffmpeg 滤镜。

不需要 ffmpeg：只验证生成结果和滤镜字符串。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stream_archive.progress_bar import (
    BACKGROUND,
    ProgressBarError,
    build_filter,
    build_images,
    build_ppm,
    density_series,
    parse_color,
)


def write_xml(path: Path, counts: dict[int, int]) -> None:
    elements = []
    for second, count in counts.items():
        for index in range(count):
            elements.append(
                f'<d p="{second}.{index:03d},1,25,16777215,0,0,0,0">弹幕</d>'
            )
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<i>\n'
        + "\n".join(elements)
        + "\n</i>\n",
        encoding="utf-8",
    )


class ProgressBarTest(unittest.TestCase):
    def test_parse_color(self) -> None:
        self.assertEqual(parse_color("0x2ED6FF"), (0x2E, 0xD6, 0xFF))
        self.assertEqual(parse_color("#2ED6FF"), (0x2E, 0xD6, 0xFF))
        with self.assertRaises(ProgressBarError):
            parse_color("blue")

    def test_density_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            xml = Path(temp) / "a.xml"
            # 第 5 秒 10 条，第 8 秒 1 条，其余为 0
            write_xml(xml, {5: 10, 8: 1})
            series = density_series(xml, duration=10, smooth_seconds=3)

        self.assertEqual(len(series), 11)
        self.assertAlmostEqual(max(series), 1.0)
        self.assertGreater(series[5], series[8])
        self.assertGreater(series[8], series[0])

    def test_build_ppm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xml = root / "a.xml"
            write_xml(xml, {5: 20})
            ppm = root / "bar.ppm"
            build_ppm(
                xml,
                duration=10,
                width=100,
                height=20,
                color="0x2ED6FF",
                smooth_seconds=2,
                output=ppm,
            )
            data = ppm.read_bytes()

        self.assertTrue(data.startswith(b"P6\n"))
        magic, dimensions, maxval, pixels = data.split(b"\n", 3)
        width, height = (int(part) for part in dimensions.split())
        self.assertEqual((width, height), (100, 20))
        self.assertEqual(maxval, b"255")
        self.assertEqual(len(pixels), width * height * 3)

        def colored_pixels(column: int) -> int:
            total = 0
            for row in range(height):
                offset = (row * width + column) * 3
                if tuple(pixels[offset : offset + 3]) != BACKGROUND:
                    total += 1
            return total

        # 第 5 秒对应 x≈49，弹幕多；第 9 秒对应 x≈89，基本为空
        self.assertGreater(colored_pixels(49), colored_pixels(89))

    def test_build_filter(self) -> None:
        text = build_filter(
            duration=3600,
            opacity=0.5,
            ass_path=Path("/tmp/弹幕.ass"),
        )
        self.assertIn("overlay=0:H-h:eof_action=repeat", text)
        self.assertIn("colorchannelmixer=aa=0.500", text)
        self.assertIn("colorchannelmixer=aa=0.350", text)
        self.assertIn("overlay=x='W*t/3600.000'", text)
        self.assertIn("ass=", text)

    def test_build_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xml = root / "a.xml"
            write_xml(xml, {5: 5})
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

            self.assertTrue(images.bar.is_file())
            self.assertTrue(images.mask.is_file())
            self.assertTrue(images.line.is_file())
            self.assertEqual(len(images.paths()), 3)

            # 遮罩和播放头的尺寸
            header = images.mask.read_bytes().split(b"\n", 2)
            self.assertEqual(header[1], b"100 20")
            header = images.line.read_bytes().split(b"\n", 2)
            self.assertEqual(header[1], b"2 20")


if __name__ == "__main__":
    unittest.main()
