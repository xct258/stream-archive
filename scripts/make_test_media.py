#!/usr/bin/env python3
"""生成用于本地联调的录播视频 + 弹幕 XML。

模拟录播姬的真实产物：
    <output>/<主播昵称>/<日期>/
        录播姬_YYYY年MM月DD日HH点MM分SS秒_测试直播_<昵称>.flv
        录播姬_YYYY年MM月DD日HH点MM分SS秒_测试直播_<昵称>.xml

视频用 ffmpeg 的 testsrc2 + sine 生成，画面上带时间码；
XML 按“安静 -> 升温 -> 高能 -> 收尾”的密度曲线生成中文弹幕。

用法：
    python3 scripts/make_test_media.py --output /tmp/demo-media
    python3 scripts/make_test_media.py --output /tmp/demo-media --durations 60,30
"""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
from pathlib import Path

TEXTS = [
    "哈哈哈哈哈", "前面的让让", "高能预警", "233333", "这波操作可以",
    "名场面来了", "awsl", "弹幕护体", "第一次看直播", "主播牛啊",
    "这也能行？", "笑死我了", "打卡", "晚上好", "好活", "再来一遍",
    "bgm 是什么", "泪目", "全体起立", "就等这一段", "经典重放",
]

COLORS = [16777215, 16777215, 16711680, 65280, 255, 16776960]


def density(second: int, duration: int) -> float:
    """按视频进度返回该秒的期望弹幕数。"""
    ratio = second / max(duration, 1)
    if ratio < 0.15:
        return 0.3
    if ratio < 0.30:
        return 1.5
    if ratio < 0.65:
        return 6.0
    if ratio < 0.85:
        return 3.0
    return 0.8


def make_xml(duration: int, path: Path) -> int:
    rng = random.Random(duration * 1000 + 7)
    rows: list[str] = []

    for second in range(duration):
        expected = density(second, duration)
        count = int(expected)
        if rng.random() < expected - count:
            count += 1

        for _ in range(count):
            timestamp = min(duration - 0.1, second + rng.random())
            mode = rng.choice([1, 1, 1, 1, 4, 5])
            color = rng.choice(COLORS)
            user = f"{rng.randrange(16 ** 8):08x}"
            rows.append(
                f'<d p="{timestamp:.3f},{mode},25,{color},0,0,{user},{len(rows)}">'
                f"{rng.choice(TEXTS)}</d>"
            )

    # 保证最后一条弹幕靠近结尾，时长校验能通过
    rows.append(
        f'<d p="{max(0, duration - 3):.3f},1,25,16777215,0,0,deadbeef,99999">'
        f"最后一条弹幕</d>"
    )

    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<i>\n'
        + "\n".join(rows)
        + "\n</i>\n"
    )
    path.write_text(content, encoding="utf-8")
    return len(rows)


def find_font() -> str | None:
    """找一个能显示英文时间码的字体；找不到就不加 drawtext。"""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path

    fc_match = shutil.which("fc-match")
    if fc_match:
        result = subprocess.run(
            [fc_match, "-f", "%{file}", "sans"],
            capture_output=True,
            text=True,
        )
        candidate = result.stdout.strip()
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def make_video(
    duration: int,
    path: Path,
    width: int,
    height: int,
    fps: int,
    label: str,
) -> None:
    font = find_font()
    filters = []
    if font:
        text = f"DEMO {label} %{{pts\\:hms}}"
        filters.append(
            "drawtext="
            f"fontfile={font}:text='{text}':x=30:y=30:"
            "fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6"
        )

    command = [
        "ffmpeg",
        "-v", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}",
    ]
    if filters:
        command += ["-vf", ",".join(filters)]
    command += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", "3M",
        "-maxrate", "4M",
        "-bufsize", "6M",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-t", str(duration),
        "-f", "flv",
        "-y", str(path),
    ]
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成录播测试视频与弹幕 XML")
    parser.add_argument("--output", type=Path, default=Path("./demo-media"))
    parser.add_argument("--streamer", default="示例昵称", help="录播姬里的昵称")
    parser.add_argument("--date", default="2026-11-15", help="目录日期 YYYY-MM-DD")
    parser.add_argument("--durations", default="60,30", help="每段时长（秒），逗号分隔")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    durations = [int(item) for item in args.durations.split(",") if item.strip()]
    if not durations:
        parser.error("--durations 不能为空")

    year, month, day = args.date.split("-")
    target = args.output / args.streamer / args.date
    target.mkdir(parents=True, exist_ok=True)

    for index, duration in enumerate(durations):
        hour = 20
        minute = index * 2
        stem = (
            f"录播姬_{year}年{month}月{day}日{hour:02d}点{minute:02d}分00秒"
            f"_测试直播_{args.streamer}"
        )
        video = target / f"{stem}.flv"
        xml = target / f"{stem}.xml"

        print(f"[{index + 1}/{len(durations)}] 生成视频 {video.name}（{duration}s）")
        make_video(duration, video, args.width, args.height, args.fps, f"P{index + 1}")

        entries = make_xml(duration, xml)
        print(f"           生成弹幕 {xml.name}（{entries} 条）")

    print(f"\n完成：{target}")
    print("用以下命令测试：")
    print(f'  python3 -m stream_archive --config config.toml once --dir "{target}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
