#!/bin/bash
# ============================================================
# 自定义安装（构建时执行一次）
#   - DanmakuFactory：XML 弹幕 -> ASS
#   - biliup：B 站投稿
#   - 本项目：从 APP_REPO/APP_REF 下载源码并用 pip 安装
#   apt / pip 能装的东西已经由 install.sh 处理，这里只做二进制下载。
# ============================================================
set -euo pipefail

# 构建期配置还在 /tmp，读取应用仓库与引用
# shellcheck disable=SC1091
source /tmp/app.conf

# 目标架构：buildx 注入 TARGETARCH；单独执行时回退 uname
target_arch="${TARGETARCH:-}"
if [ -z "$target_arch" ]; then
    case "$(uname -m)" in
        x86_64) target_arch=amd64 ;;
        aarch64 | arm64) target_arch=arm64 ;;
        *) target_arch="$(uname -m)" ;;
    esac
fi
echo "[custom] 目标架构: ${target_arch}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# ------------------------------------------------------------
# 1. DanmakuFactory CLI
#    官方 release 的 Linux CLI 包按架构命名：
#      DanmakuFactory-dev-linux-x86_64-CLI.tar.gz
#      DanmakuFactory-dev-linux-arm64-CLI.tar.gz
# ------------------------------------------------------------
case "$target_arch" in
    amd64) df_arch=x86_64 ;;
    arm64) df_arch=arm64 ;;
    *)
        echo "[custom] DanmakuFactory 不支持架构: ${target_arch}" >&2
        exit 1
        ;;
esac

df_url="https://github.com/hihkm/DanmakuFactory/releases/download/dev/DanmakuFactory-dev-linux-${df_arch}-CLI.tar.gz"
echo "[custom] 下载 DanmakuFactory: ${df_url}"
curl -fsSL --retry 3 --retry-delay 5 -o "${tmp}/df.tar.gz" "$df_url"
tar -xzf "${tmp}/df.tar.gz" -C "${tmp}"

df_bin=$(find "${tmp}" -type f -name 'DanmakuFactory' | head -1)
if [ -z "$df_bin" ]; then
    echo "[custom] 未找到 DanmakuFactory 可执行文件" >&2
    exit 1
fi
install -m 755 "$df_bin" /usr/local/bin/DanmakuFactory
echo "[custom] DanmakuFactory 安装完成"

# ------------------------------------------------------------
# 2. biliup
#    固定版本，按架构选择 release 资源；musl 版在 Debian 上可直接运行。
# ------------------------------------------------------------
case "$target_arch" in
    amd64) bili_asset=x86_64-linux-musl ;;
    arm64) bili_asset=aarch64-linux ;;
    *)
        echo "[custom] biliup 不支持架构: ${target_arch}" >&2
        exit 1
        ;;
esac

bili_ver=v1.3.5
bili_url="https://github.com/biliup/biliup/releases/download/${bili_ver}/biliupR-${bili_ver}-${bili_asset}.tar.xz"
echo "[custom] 下载 biliup: ${bili_url}"
curl -fsSL --retry 3 --retry-delay 5 -o "${tmp}/biliup.tar.xz" "$bili_url"
tar -xJf "${tmp}/biliup.tar.xz" -C "${tmp}"

bili_bin=$(find "${tmp}" -type f -name biliup | head -1)
if [ -z "$bili_bin" ]; then
    echo "[custom] 未找到 biliup 可执行文件" >&2
    exit 1
fi
install -m 755 "$bili_bin" /usr/local/bin/biliup
echo "[custom] biliup 安装完成"

# ------------------------------------------------------------
# 2.1 BililiveRecorder（默认安装）
#     release 资产名：BililiveRecorder-CLI-linux-x64.zip / linux-arm64.zip
# ------------------------------------------------------------
if [ "${INSTALL_RECORDER:-true}" = "true" ]; then
    case "$target_arch" in
        amd64) br_arch=x64 ;;
        arm64) br_arch=arm64 ;;
        *)
            echo "[custom] 录播姬不支持架构: ${target_arch}" >&2
            exit 1
            ;;
    esac

    if [ -n "${RECORDER_VERSION:-}" ]; then
        br_base="https://github.com/BililiveRecorder/BililiveRecorder/releases/download/${RECORDER_VERSION}"
    else
        br_base="https://github.com/BililiveRecorder/BililiveRecorder/releases/latest/download"
    fi
    br_url="${br_base}/BililiveRecorder-CLI-linux-${br_arch}.zip"
    echo "[custom] 下载录播姬: ${br_url}"
    curl -fsSL --retry 3 --retry-delay 5 -o "${tmp}/br.zip" "$br_url"
    rm -rf /opt/bililive/recorder
    mkdir -p /opt/bililive/recorder
    unzip -q -o "${tmp}/br.zip" -d /opt/bililive/recorder
    chmod +x /opt/bililive/recorder/BililiveRecorder.Cli
    echo "[custom] 录播姬安装完成"
else
    echo "[custom] INSTALL_RECORDER=false，跳过录播姬"
fi

# ------------------------------------------------------------
# 3. 本项目：安装到独立 venv
#    不碰 Debian 的 python3-xxx 包，避免 pip 因缺少 RECORD 无法卸载
#    同时保留 config.example.toml，供 once.sh 首次启动时复制。
# ------------------------------------------------------------
repo="${APP_REPO:?APP_REPO 未配置}"
ref="${APP_REF:-main}"
src_url="https://github.com/${repo}/archive/${ref}.tar.gz"

echo "[custom] 下载应用源码: ${src_url}"
mkdir -p "${tmp}/src"
curl -fsSL --retry 3 --retry-delay 5 "${src_url}" | tar -xz -C "${tmp}/src"

pkg_dir=$(find "${tmp}/src" -maxdepth 1 -mindepth 1 -type d | head -1)
if [ -z "$pkg_dir" ]; then
    echo "[custom] 应用源码解压失败" >&2
    exit 1
fi

VENV=/opt/stream-archive/venv
echo "[custom] 创建 venv: ${VENV}"
python3 -m venv "$VENV"
VENV_PIP="${VENV}/bin/pip"

echo "[custom] 升级 venv 内的 pip/setuptools/wheel"
"$VENV_PIP" install --no-cache-dir --disable-pip-version-check \
    --upgrade pip setuptools wheel

echo "[custom] 安装 stream-archive"
"$VENV_PIP" install --no-cache-dir --disable-pip-version-check "$pkg_dir"

mkdir -p /opt/stream-archive
cp "${pkg_dir}/config.example.toml" /opt/stream-archive/config.example.toml

# 把入口脚本链接到 /usr/local/bin，services.sh 按 PATH 调用
for command in stream-archive stream-archive-web stream-archive-asr stream-archive-recorder; do
    ln -sf "${VENV}/bin/${command}" "/usr/local/bin/${command}"
done

# 构建期自检：命令能启动即认为安装成功
stream-archive --help >/dev/null

# ------------------------------------------------------------
# 4. 内置 ASR（可选）
# ------------------------------------------------------------
if [ "${INSTALL_ASR:-false}" = "true" ]; then
    echo "[custom] 安装 faster-whisper..."
    "$VENV_PIP" install --no-cache-dir --disable-pip-version-check \
        "faster-whisper==1.2.1" "huggingface-hub<2" "av==13.1.0"
    echo "[custom] faster-whisper 安装完成"
else
    echo "[custom] INSTALL_ASR=false，跳过 faster-whisper"
fi

# ------------------------------------------------------------
# 5. 监控界面（可选）
# ------------------------------------------------------------
if [ "${INSTALL_WEB:-false}" = "true" ]; then
    echo "[custom] 安装监控界面依赖..."
    "$VENV_PIP" install --no-cache-dir --disable-pip-version-check \
        "fastapi>=0.110,<1" "uvicorn>=0.27,<1"
    echo "[custom] FastAPI/uvicorn 安装完成"
else
    echo "[custom] INSTALL_WEB=false，跳过监控界面依赖"
fi

echo "[custom] 自定义安装全部完成"
