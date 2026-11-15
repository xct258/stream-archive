"""高能进度条：复刻 khx-live 的曲线样式。

视觉（和旧项目一致）：
    - 透明背景，不填充，只用 2~3px 细曲线；
    - 已播放绿色、未播放灰色；
    - SC 前后 30 秒红色，舰长前后 30 秒蓝色（SC 优先）；
    - 弹幕密度开平方后做高斯平滑。

实现（比旧项目轻）：
    - 只渲染画面底部条带（宽 x height），不生成 1920x1080 全屏帧；
    - 每秒一帧，直接管道给 ffmpeg 编码成带 alpha 的 PNG/MKV；
    - 主编码时 overlay 到画面底部，不依赖 matplotlib / numpy。

旧项目每 5 秒生成一张全屏 PNG，这里改成每秒一张小条带：
    - 临时文件从几百 MB 降到几十 MB；
    - 不增加主编码的 CPU 压力，只多一次很轻的条带编码。
"""

from __future__ import annotations

import math
import os
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .log import get_logger

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

# 旧项目 matplotlib 的颜色
GREEN = (0, 128, 0)
GRAY = (128, 128, 128)
RED = (255, 0, 0)
BLUE = (0, 0, 255)

# SC / 舰长高亮范围（秒）
HIGHLIGHT_SECONDS = 30

# 进度条视频帧率；每秒一帧足够，旧项目是每 5 秒
BAR_FPS = 1


class ProgressBarError(RuntimeError):
    """弹幕密度读取或图片生成失败。"""


@dataclass
class BarImages:
    """高能进度条需要的输入文件（一条带 alpha 的视频）。"""

    bar: Path

    def paths(self) -> list[Path]:
        return [self.bar]


# ---------------------------------------------------------------------------
# XML 解析
# ---------------------------------------------------------------------------


def load_danmaku(xml_path: Path) -> tuple[list[float], list[float], list[float]]:
    """读取 (弹幕时间, SC 时间, 舰长时间)。

    截断的 XML（旧容器被强杀时可能出现）不直接报错，
    能读到多少用多少，避免整场进度条丢失。
    """
    bullets: list[float] = []
    sc_times: list[float] = []
    guard_times: list[float] = []
    if not xml_path.is_file():
        return bullets, sc_times, guard_times

    logger = get_logger("stream_archive.progress_bar")
    try:
        for _, element in ET.iterparse(str(xml_path), events=("end",)):
            tag = element.tag
            if tag == "d":
                raw = element.get("p")
                if raw:
                    try:
                        bullets.append(float(raw.split(",", 1)[0]))
                    except ValueError:
                        pass
            elif tag == "sc":
                raw = element.get("ts")
                if raw:
                    try:
                        sc_times.append(float(raw))
                    except ValueError:
                        pass
            elif tag == "guard":
                raw = element.get("ts")
                if raw:
                    try:
                        guard_times.append(float(raw))
                    except ValueError:
                        pass
            element.clear()
    except ET.ParseError as exc:
        logger.warning("弹幕 XML 解析中断，使用已读取的数据：%s", exc)

    return bullets, sc_times, guard_times


# ---------------------------------------------------------------------------
# 密度曲线
# ---------------------------------------------------------------------------


def _gaussian_kernel(window: int) -> list[float]:
    """和旧项目一致：std = window / 3，权重归一化。"""
    window = max(3, int(window))
    if window % 2 == 0:
        window += 1
    std = window / 3.0
    half = window // 2
    weights = [math.exp(-0.5 * (offset / std) ** 2) for offset in range(-half, half + 1)]
    total = sum(weights)
    return [weight / total for weight in weights]


def _density_from_times(
    bullet_times: list[float], duration: float, smooth_seconds: int
) -> list[float]:
    """每秒弹幕数 -> sqrt -> 高斯平滑 -> 归一化到 0~1。"""
    length = max(1, int(math.ceil(max(duration, 0))) + 1)
    raw = [0.0] * length
    for value in bullet_times:
        second = int(value)
        if 0 <= second < length:
            raw[second] += 1.0

    # 开平方抑制刷屏造成的极端尖峰
    transformed = [math.sqrt(value) for value in raw]

    kernel = _gaussian_kernel(max(3, int(smooth_seconds)))
    half = len(kernel) // 2
    smoothed = [0.0] * length
    for index in range(length):
        total = 0.0
        for offset, weight in enumerate(kernel):
            source = index + offset - half
            if 0 <= source < length:
                total += transformed[source] * weight
        smoothed[index] = total

    peak = max(smoothed) if smoothed else 0.0
    if peak <= 0:
        return [0.0] * length
    return [value / peak for value in smoothed]


def density_series(xml_path: Path, duration: float, smooth_seconds: int) -> list[float]:
    """兼容旧接口：直接从 XML 生成归一化密度。"""
    bullets, _sc, _guard = load_danmaku(xml_path)
    return _density_from_times(bullets, duration, smooth_seconds)


def highlight_kinds(
    length: int, sc_times: list[float], guard_times: list[float]
) -> bytearray:
    """每秒的颜色标记：0 普通，1 SC（红），2 舰长（蓝）。

    SC 优先于舰长，和旧项目一致。
    """
    kinds = bytearray(max(1, length))

    def mark(times: list[float], value: int) -> None:
        for timestamp in times:
            low = max(0, int(timestamp) - HIGHLIGHT_SECONDS)
            high = min(len(kinds) - 1, int(timestamp) + HIGHLIGHT_SECONDS)
            for second in range(low, high + 1):
                if kinds[second] == 0:
                    kinds[second] = value

    mark(sc_times, 1)
    mark(guard_times, 2)
    return kinds


# ---------------------------------------------------------------------------
# 条带渲染
# ---------------------------------------------------------------------------


def _level_at(series: list[float], timestamp: float) -> float:
    if not series:
        return 0.0
    position = max(0.0, timestamp)
    index = int(position)
    if index + 1 >= len(series):
        return series[-1]
    fraction = position - index
    return series[index] * (1.0 - fraction) + series[index + 1] * fraction


def _geometry(height: int) -> tuple[int, int]:
    """返回 (基线 y, 顶部 y)。

    旧项目 ylim = [min - 0.2r, max + 20r]，曲线落在画面底部约 5.7% 高度：
    1080p 时基线在底部上方约 10px，峰值约 61px。
    """
    baseline = height - max(1, round(height * 0.156))
    top = max(1, round(height * 0.047))
    if top >= baseline:
        top = max(0, baseline - 1)
    return baseline, top


def _layout(
    duration: float,
    width: int,
    height: int,
    series: list[float],
    kinds: bytearray,
) -> tuple[bytearray, list[list[int]], list[int]]:
    """生成基础帧（未播放灰色 + SC/舰长高亮）和每列像素位置。"""
    width = max(1, int(width))
    height = max(2, int(height))
    baseline, top = _geometry(height)
    span = max(1, baseline - top)

    base = bytearray(width * height * 4)
    columns: list[list[int]] = []
    column_kinds: list[int] = []
    previous_y: int | None = None

    for x in range(width):
        timestamp = duration * x / max(1, width - 1)
        level = _level_at(series, timestamp)
        y = int(round(baseline - level * span))
        y = max(top, min(baseline, y))

        if previous_y is None:
            low, high = y, y
        else:
            low, high = min(y, previous_y), max(y, previous_y)
        low = max(0, low - 1)
        high = min(height - 1, high + 1)

        index = min(len(kinds) - 1, max(0, int(timestamp)))
        kind = kinds[index]
        color = RED if kind == 1 else BLUE if kind == 2 else GRAY

        offsets: list[int] = []
        for row in range(low, high + 1):
            offset = (row * width + x) * 4
            base[offset : offset + 4] = bytes((*color, 255))
            offsets.append(offset)
        columns.append(offsets)
        column_kinds.append(kind)
        previous_y = y

    return base, columns, column_kinds


def render_frames(
    xml_path: Path,
    duration: float,
    width: int,
    height: int,
    smooth_seconds: int,
) -> Iterator[bytes]:
    """逐秒生成 RGBA 帧（bytes）。"""
    bullets, sc_times, guard_times = load_danmaku(xml_path)
    series = _density_from_times(bullets, duration, smooth_seconds)
    kinds = highlight_kinds(len(series), sc_times, guard_times)
    base, columns, column_kinds = _layout(duration, width, height, series, kinds)

    green = bytes((*GREEN, 255))
    frame = bytearray(base)
    played_until = 0
    total = max(1, int(math.ceil(max(duration, 0.0))))

    for second in range(total):
        target = min(
            len(columns),
            int(round(len(columns) * second / max(duration, 1e-6))),
        )
        for x in range(played_until, target):
            if column_kinds[x] == 0:
                for offset in columns[x]:
                    frame[offset : offset + 4] = green
        played_until = max(played_until, target)
        yield bytes(frame)


def _encode_bar(
    frames: Iterator[bytes],
    width: int,
    height: int,
    output: Path,
) -> Path:
    """把 RGBA 帧管道给 ffmpeg，编码成带 alpha 的 PNG/MKV。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{width}x{height}",
        "-r",
        str(BAR_FPS),
        "-i",
        "-",
        "-c:v",
        "png",
        "-pix_fmt",
        "rgba",
        str(output),
    ]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    try:
        for frame in frames:
            process.stdin.write(frame)
    finally:
        process.stdin.close()

    error = b""
    if process.stderr is not None:
        error = process.stderr.read()
        process.stderr.close()
    return_code = process.wait()
    if return_code != 0:
        message = error.decode("utf-8", errors="replace")[-500:]
        raise ProgressBarError(f"进度条视频生成失败：{message}")
    return output


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
    """生成高能进度条视频。

    color 参数保留是为了兼容旧配置；经典样式固定使用绿色/灰色/红/蓝。
    """
    del color  # 旧样式颜色固定

    output = directory / f"{stem}-bar.mkv"
    frames = render_frames(
        xml_path,
        duration,
        max(1, int(width)),
        max(8, int(height)),
        max(1, int(smooth_seconds)),
    )
    _encode_bar(frames, max(1, int(width)), max(8, int(height)), output)
    get_logger("stream_archive.progress_bar").debug(
        "高能进度条已生成：%s（%sx%s，%s fps）",
        output,
        width,
        height,
        BAR_FPS,
    )
    return BarImages(bar=output)


# ---------------------------------------------------------------------------
# ffmpeg 滤镜
# ---------------------------------------------------------------------------


def build_filter(duration: float, opacity: float, ass_path: Path) -> str:
    """构造 ffmpeg filter_complex。

    输入约定：
        [0:v] 原视频
        [1:v] 高能进度条视频（带 alpha，1 fps）

    进度条视频只有一帧每秒，overlay 会把上一帧保持到下一帧，
    所以条带不需要跟主视频同帧率。
    """
    del duration, opacity  # 峰值/播放位置已经烘焙在条带里

    from .danmaku import escape_filter_path

    return (
        "[1:v]format=rgba[bar];"
        "[0:v][bar]overlay=0:H-h:eof_action=repeat[vbar];"
        f"[vbar]ass={escape_filter_path(ass_path)}[vout]"
    )
