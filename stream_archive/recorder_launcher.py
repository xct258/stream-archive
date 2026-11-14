"""录播姬启动器。

职责：
    1. 按 config.toml 生成/更新录播姬的 config.json；
    2. 生成管理面板密码（配置留空时）；
    3. exec BililiveRecorder.Cli，让 supervisor 直接管理录制进程。

这个进程由 startup/services.sh 启动，日志写到
/data/logs/service/recorder.log。
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config
from .recorder import sync_recorder_config
from .util import ensure_dir

# 镜像里的默认安装位置；可用 RECORDER_BIN 覆盖
DEFAULT_BINARY = Path(
    os.environ.get("RECORDER_BIN", "/opt/bililive/recorder/BililiveRecorder.Cli")
)
PASSWORD_FILE_NAME = ".http_password"


class RecorderLaunchError(RuntimeError):
    pass


def ensure_password(config: Config) -> tuple[str, Path | None]:
    """返回 (密码, 密码文件)；配置里给了密码时不写文件。"""
    if config.recorder.http_password:
        return config.recorder.http_password, None

    work_dir = ensure_dir(config.recorder.work_dir)
    password_file = work_dir / PASSWORD_FILE_NAME
    if password_file.is_file():
        value = password_file.read_text(encoding="utf-8").strip()
        if value:
            return value, password_file

    value = secrets.token_urlsafe(18)
    password_file.write_text(value + "\n", encoding="utf-8")
    password_file.chmod(0o600)
    return value, password_file


def build_command(
    config: Config, binary: Path | None = None
) -> tuple[list[str], Path | None]:
    """生成 BililiveRecorder.Cli 命令，并保证配置/密码已就绪。"""
    binary = Path(binary or DEFAULT_BINARY)
    if not binary.is_file():
        raise RecorderLaunchError(
            f"找不到录播姬可执行文件：{binary}（构建镜像时 INSTALL_RECORDER=true）"
        )
    if not os.access(binary, os.X_OK):
        raise RecorderLaunchError(f"录播姬不可执行：{binary}")

    if not config.recorder.enabled:
        raise RecorderLaunchError("配置里 [recorder].enabled=false")

    # 每次启动都同步一次，确保房间/cookie 与 config.toml 一致
    sync_recorder_config(config)

    password, password_file = ensure_password(config)
    work_dir = ensure_dir(config.recorder.work_dir)

    command = [
        str(binary),
        "run",
        str(work_dir),
        "--bind", config.recorder.http_bind,
        "--http-basic-user", config.recorder.http_user,
        "--http-basic-pass", password,
        "--log", "Information",
    ]
    # 绑定非 loopback 时，录播姬要求显式允许外网访问
    if "127.0.0.1" not in config.recorder.http_bind and "localhost" not in config.recorder.http_bind:
        command.append("--http-open-access")
    return command, password_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stream-archive-recorder", description="启动录播姬（配置由 stream-archive 维护）"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("/data/config.toml"), help="配置文件"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if not config.recorder.enabled:
        print("[recorder] enabled=false，未启动录播姬")
        return 0

    try:
        command, password_file = build_command(config)
    except (RecorderLaunchError, OSError) as exc:
        print(f"启动录播姬失败：{exc}", file=sys.stderr)
        return 2

    print(f"录播姬工作目录：{config.recorder.work_dir}")
    print(f"管理面板：{config.recorder.http_bind}（用户 {config.recorder.http_user}）")
    if password_file:
        print(f"密码文件：{password_file}")
    elif config.recorder.http_password:
        print("密码：使用 config.toml 中的 recorder.http_password")
    print(f"录制输出：{config.recorder.work_dir}/video")

    # 直接用 exec，进程 PID 不变，supervisor 能正确管理/停止
    os.execv(command[0], command)
    return 0  # 不会执行到


if __name__ == "__main__":
    raise SystemExit(main())
