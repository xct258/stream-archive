"""高能进度条：把弹幕密度曲线叠到画面底部。

设计：
    - 纯 Python 生成三张 PPM 图片（P6），不依赖 matplotlib / Pillow；
    - 横轴是整场时长，纵轴是平滑后的弹幕密度；
    - ffmpeg overlay 到画面底部，再用黑色遮罩压暗未播放区域、白色竖线画播放头。

这样只需要一张图片，不需要为整场视频生成几千帧 PNG，
4 小时的录播也只占几百 KB 临时空间。
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .log import get_logger

# 背景色（会被 colorchannelmixer 做成半透明）
BACKGROUND = (8, 10, 14)


class ProgressBarError(RuntimeError):
    """弹幕密度读取或图片生成失败。"""


@dataclass
class BarImages:
    """高能进度条需要的三张图。

    bar:  弹幕密度曲线
    mask: 全宽黑色遮罩，用于把播放头之后的区域压暗
    line: 2px 宽白色竖线，播放头
    """

    bar: Path
    mask: Path
    line: Path

    def paths(self) -> list[Path]:
        return [self.bar, self.mask, self.line]


def parse_color(value: str) -> tuple[int, int, int]:
    """把 0xRRGGBB / #RRGGBB 转成 RGB 三元组。"""
    text = value.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    elif text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        raise ProgressBarError(f"颜色格式不正确：{value}")
    try:
        return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as exc:
        raise ProgressBarError(f"颜色格式不正确：{value}") from exc


def density_series(
    xml_path: Path, duration: float, smooth_seconds: int
) -> list[float]:
    """返回按秒采样的归一化弹幕密度（0~1）。

    处理步骤：
        1. 统计每秒弹幕条数；
        2. 开平方，抑制刷屏造成的极端尖峰；
        3. 用前缀和做滑动平均，让曲线圆滑；
        4. 除以峰值归一化。
    """
    length = max(1, int(math.ceil(max(duration, 0))) + 1)
    raw = [0.0] * length

    if xml_path.is_file():
        try:
            for _, element in ET.iterparse(str(xml_path), events=("end",)):
                if element.tag != "d":
                    continue
                attribute = element.get("p")
                if attribute:
                    try:
                        second = int(float(attribute.split(",", 1)[0]))
                    except ValueError:
                        second = -1
                    if 0 <= second < length:
                        raw[second] += 1.0
                element.clear()
        except ET.ParseError as exc:
            raise ProgressBarError(f"弹幕文件解析失败：{xml_path}") from exc

    # 开平方抑制尖峰
    transformed = [math.sqrt(value) for value in raw]

    # 前缀和，O(n) 滑动平均
    prefix = [0.0]
    for value in transformed:
        prefix.append(prefix[-1] + value)

    half = max(0, smooth_seconds // 2)
    smoothed: list[float] = []
    for index in range(length):
        left = max(0, index - half)
        right = min(length - 1, index + half)
        total = prefix[right + 1] - prefix[left]
        smoothed.append(total / (right - left + 1))

    peak = max(smoothed) if smoothed else 0.0
    if peak <= 0:
        return [0.0] * length
    return [value / peak for value in smoothed]


def build_ppm(
    xml_path: Path,
    duration: float,
    width: int,
    height: int,
    color: str,
    smooth_seconds: int,
    output: Path,
) -> Path:
    """生成高能进度条图片，返回 PPM 路径。

    参数：
        xml_path: 弹幕 XML
        duration: 视频时长（秒）
        width:    图片宽度，必须与视频宽度一致（overlay 不缩放）
        height:   图片高度（进度条高度）
        color:    曲线颜色 0xRRGGBB
        output:   输出 PPM 路径
    """
    logger = get_logger("stream_archive.progress_bar")

    width = max(1, int(width))
    height = max(8, int(height))
    series = density_series(xml_path, duration, max(1, smooth_seconds))
    bar_color = parse_color(color)
    # 曲线顶部稍微提亮，视觉上更清晰
    top_color = tuple(min(255, int(channel * 0.6 + 255 * 0.4)) for channel in bar_color)

    # 先铺背景
    data = bytearray(BACKGROUND * (width * height))

    for x in range(width):
        # 把像素横坐标映射到视频时间
        timestamp = duration * x / max(1, width - 1)
        index = min(len(series) - 1, max(0, int(timestamp)))
        level = series[index]
        bar_height = max(1, int(level * (height - 4)))

        for y in range(height - bar_height, height):
            offset = (y * width + x) * 3
            r, g, b = top_color if y == height - bar_height else bar_color
            data[offset] = r
            data[offset + 1] = g
            data[offset + 2] = b

    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + bytes(data))

    logger.debug("高能进度条已生成：%s（%sx%s）", output, width, height)
    return output


def _write_solid(path: Path, width: int, height: int, color: tuple[int, int, int]) -> Path:
    """写一张纯色 PPM，用于遮罩和播放头。"""
    pixels = bytearray(color * (width * height))
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(pixels))
    return path


def build_images(
    xml_path: Path,
    duration: float,
    width: int,
    height: int,
    color: str,
    smooth_seconds: int,
    directory: Path,
    stem: str,
) -> BarImages:
    """一次生成进度条需要的三张图。"""
    bar = build_ppm(
        xml_path,
        duration,
        width,
        height,
        color,
        smooth_seconds,
        directory / f"{stem}-bar.ppm",
    )
    mask = _write_solid(directory / f"{stem}-mask.ppm", width, height, (0, 0, 0))
    line = _write_solid(directory / f"{stem}-line.ppm", 2, height, (255, 255, 255))
    return BarImages(bar=bar, mask=mask, line=line)


def build_filter(
    duration: float,
    opacity: float,
    ass_path: Path,
) -> str:
    """构造 ffmpeg filter_complex。

    输入约定：
        [0:v] 原视频
        [1:v] 高能进度条图片（单帧，overlay 用 eof_action=repeat 重复）
        [2:v] 全宽黑色遮罩（把播放头之后压暗）
        [3:v] 2px 白色竖线（播放头）

    处理顺序：
        1. 进度条图片转 rgba 并设置整体透明度；
        2. overlay 到画面底部；
        3. 遮罩从播放头位置开始覆盖到右边缘，形成“未播放变暗”；
        4. 白色竖线作为播放头；
        5. 最后叠加 ASS 弹幕。

    注意：
        drawbox 的 t 是“线宽”，不是时间戳，不能用它做动画；
        所以动画全部交给 overlay 的 x 表达式（overlay 支持时间变量 t）。
    """
    # 延迟导入，避免 danmaku 与 progress_bar 的循环依赖
    from .danmaku import escape_filter_path

    safe_duration = max(duration, 1.0)
    alpha = min(1.0, max(0.0, float(opacity)))

    return (
        f"[1:v]format=rgba,colorchannelmixer=aa={alpha:.3f}[bar];"
        f"[0:v][bar]overlay=0:H-h:eof_action=repeat[vbar];"
        f"[2:v]format=rgba,colorchannelmixer=aa=0.350[mask];"
        f"[vbar][mask]overlay=x='W*t/{safe_duration:.3f}':y=H-h:eof_action=repeat[vplayed];"
        f"[3:v]format=rgba[line];"
        f"[vplayed][line]overlay=x='W*t/{safe_duration:.3f}':y=H-h:eof_action=repeat[vline];"
        f"[vline]ass={escape_filter_path(ass_path)}[vout]"
    )
