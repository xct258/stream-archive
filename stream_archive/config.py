"""配置加载与校验（通用多主播）。

配置文件使用 TOML。Python 3.11+ 标准库自带 tomllib，不需要第三方包。

结构：
    [general] / [watch] / [recorder] / [github]   全局设置
    [danmaku] / [encode] / [upload] / [archive]   全局默认参数
    [[streamers]]                                 主播列表（可覆盖上面的默认参数）

加载流程：
    TOML -> dict -> 合并全局默认与主播覆盖 -> Config 对象
所有路径在这里统一转成 pathlib.Path，后续模块不再处理字符串路径。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """配置缺失或取值非法。错误信息会直接打印给用户。"""


# ---------------------------------------------------------------------------
# 参数段（既是全局默认，也是每个主播解析后的结果）
# ---------------------------------------------------------------------------


@dataclass
class GeneralConfig:
    server_name: str = "nas"
    work_dir: Path = Path("/data/work")
    videos_dir: Path = Path("/data/videos")
    data_dir: Path = Path("/data/state")
    log_dir: Path = Path("/data/logs")


@dataclass
class WatchConfig:
    source_folders: list[Path] = field(default_factory=lambda: [Path("/data/rec")])
    poll_seconds: int = 300
    idle_minutes: int = 20


@dataclass
class DanmakuConfig:
    enabled: bool = True
    factory: Path = Path("/usr/local/bin/DanmakuFactory")
    font_name: str = "Noto Sans CJK SC"
    font_size: int = 50
    opacity: int = 200
    max_diff_seconds: int = 25


@dataclass
class EncodeConfig:
    crf: int = 21
    preset: str = "veryfast"
    profile: str = "high"
    threads: int = 0
    try_qsv: bool = True
    qsv_quality: int = 20
    qsv_preset: str = "medium"

    # 可选高能进度条：把弹幕密度曲线叠在画面底部
    progress_bar: bool = False
    progress_bar_height: int = 64
    progress_bar_opacity: float = 0.6
    progress_bar_color: str = "0x2ED6FF"
    progress_bar_smooth: int = 60


@dataclass
class UploadConfig:
    enabled: bool = True
    biliup: Path = Path("/usr/local/bin/biliup")
    cookies: Path = Path("/data/cookies/bilibili/cookies-默认账号.json")
    tid: int = 17
    copyright: int = 2
    source: str = ""
    tags: list[str] = field(default_factory=list)
    title_template: str = "{date} [{title}]"
    desc_template: str = ""


@dataclass
class ArchiveConfig:
    cleanup_enabled: bool = True
    retention_days: int = 15
    max_delete_per_run: int = 8
    delete_after_upload: bool = False


@dataclass
class AppendConfig:
    """按月投稿：把同一主播同一自然月的视频追加到同一个 B 站稿件。"""

    enabled: bool = False
    # 映射文件；空字符串表示 <data_dir>/append_month_vid.map
    map_file: str = ""
    # 追加哪个版本：raw=原文件（默认），pressed=投稿版
    source: str = "raw"
    # 标题/简介模板，可用 {streamer} {month} {month_cn}
    title_template: str = "{streamer} {month_cn} 直播回放合集"
    desc_template: str = "本稿件为{streamer}{month_cn}原版直播回放合集，每次直播追加为一个分P。"
    # 月稿件封面；留空则不传
    cover: str = ""


@dataclass
class AsrConfig:
    """语音转文本：本地一次性加载 faster-whisper，转写完退出并释放模型。"""

    enabled: bool = False
    model: str = "large-v3-turbo"
    language: str = "zh"
    device: str = "auto"            # auto / cpu / cuda
    compute_type: str = "auto"      # auto / int8 / float16 / ...
    model_dir: Path = Path("/data/asr/models")
    beam_size: int = 5
    # HuggingFace 镜像地址，例如 https://hf-mirror.com；留空使用官方
    hf_endpoint: str = ""
    # worker 单次运行的最长秒数
    timeout: int = 14400


@dataclass
class WebdavConfig:
    """WebDAV：把 XML 弹幕文件上传到网盘。

    参数对应旧版 rclone 的 webdav remote：
        url/vendor/user/pass  -> url/username/password
        remote 路径模板       -> path
    直接用标准库发 HTTP PUT，不需要安装 rclone，也不需要 rclone.conf。
    """

    enabled: bool = False
    # WebDAV 根地址，例如 https://openlist.example.com/dav
    url: str = ""
    username: str = ""
    password: str = ""
    # 目录模板，可用 {streamer} {yyyy} {mm} {dd} {month} {date}
    path: str = "/直播回放弹幕/{streamer}/{yyyy}/{mm}/{date}"
    # 对外访问前缀，用于月稿件简介里的 {pan_url}；留空则不生成
    public_url: str = ""
    # 只上传成功追加到月稿件的 XML；false 表示本场全部 XML 都传
    only_appended: bool = False
    # 自签名证书时设为 true
    insecure: bool = False
    timeout: int = 60


@dataclass
class RecorderConfig:
    config_path: Path = Path("/data/recorder/config.json")
    record_cookie_json: Path = Path("/data/cookies/bilibili/cookies-录播账号.json")


@dataclass
class GithubSource:
    """一条“仓库内文件 -> 本地文件”的同步规则。"""

    remote: str
    local: Path


@dataclass
class GithubConfig:
    enabled: bool = False
    repo: str = ""
    branch: str = "main"
    token_file: Path = Path("/app/.github_token")
    sources: list[GithubSource] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 主播
# ---------------------------------------------------------------------------


@dataclass
class StreamerConfig:
    """一个主播的完整配置（全局默认已合并进来）。"""

    id: str
    name: str                          # 归档目录名
    aliases: list[str] = field(default_factory=list)
    enabled: bool = True
    room_id: int | None = None         # 录播姬房间号，None 表示不同步录制
    source_folders: list[Path] = field(default_factory=list)  # 空表示用全局 watch

    danmaku: DanmakuConfig = field(default_factory=DanmakuConfig)
    encode: EncodeConfig = field(default_factory=EncodeConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    append: AppendConfig = field(default_factory=AppendConfig)
    webdav: WebdavConfig = field(default_factory=WebdavConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)

    def matches(self, raw_name: str) -> bool:
        """录制文件名里的主播名是否属于这个主播。"""
        if not raw_name:
            return False
        return raw_name in self.aliases or raw_name in (self.name, self.id)


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    streamers: list[StreamerConfig] = field(default_factory=list)

    # ---- 查询辅助 ----
    def streamer_by_id(self, streamer_id: str) -> StreamerConfig | None:
        for streamer in self.streamers:
            if streamer.id == streamer_id:
                return streamer
        return None

    def enabled_streamers(self) -> list[StreamerConfig]:
        return [streamer for streamer in self.streamers if streamer.enabled]

    def source_folders_of(self, streamer: StreamerConfig) -> list[Path]:
        """主播实际使用的监控目录：自己声明了就用自己声明的，否则用全局。"""
        return streamer.source_folders or self.watch.source_folders

    # ---- 校验 ----
    def validate(self) -> None:
        if not self.watch.source_folders:
            raise ConfigError("watch.source_folders 不能为空")
        if self.watch.poll_seconds <= 0:
            raise ConfigError("watch.poll_seconds 必须大于 0")
        if self.watch.idle_minutes <= 0:
            raise ConfigError("watch.idle_minutes 必须大于 0")
        if not self.streamers:
            raise ConfigError("至少需要配置一个 [[streamers]]")

        if self.github.enabled and not self.github.repo.strip():
            raise ConfigError("github.enabled=true 时 github.repo 不能为空")

        seen_ids: set[str] = set()
        for streamer in self.streamers:
            if not streamer.id:
                raise ConfigError("streamers[].id 不能为空")
            if streamer.id in seen_ids:
                raise ConfigError(f"重复的主播 id：{streamer.id}")
            seen_ids.add(streamer.id)
            if not streamer.name:
                raise ConfigError(f"主播 {streamer.id} 的 name 不能为空")
            if streamer.room_id is not None and streamer.room_id <= 0:
                raise ConfigError(f"主播 {streamer.id} 的 room_id 必须大于 0")

            _validate_danmaku(streamer.danmaku, streamer.id)
            _validate_encode(streamer.encode, streamer.id)
            _validate_archive(streamer.archive, streamer.id)
            _validate_append(streamer.append, streamer.id)
            _validate_webdav(streamer.webdav, streamer.id)
            _validate_asr(streamer.asr, streamer.id)


def _validate_danmaku(config: DanmakuConfig, owner: str) -> None:
    if not 0 <= config.opacity <= 255:
        raise ConfigError(f"{owner}.danmaku.opacity 必须在 0-255 之间")
    if config.font_size <= 0:
        raise ConfigError(f"{owner}.danmaku.font_size 必须大于 0")
    if config.max_diff_seconds < 0:
        raise ConfigError(f"{owner}.danmaku.max_diff_seconds 不能为负数")


def _validate_encode(config: EncodeConfig, owner: str) -> None:
    if not 0 <= config.crf <= 51:
        raise ConfigError(f"{owner}.encode.crf 必须在 0-51 之间")
    if config.threads < 0:
        raise ConfigError(f"{owner}.encode.threads 不能为负数")
    if not config.progress_bar:
        return
    if not 8 <= config.progress_bar_height <= 400:
        raise ConfigError(f"{owner}.encode.progress_bar_height 必须在 8-400 之间")
    if not 0 <= config.progress_bar_opacity <= 1:
        raise ConfigError(f"{owner}.encode.progress_bar_opacity 必须在 0-1 之间")
    if config.progress_bar_smooth < 1:
        raise ConfigError(f"{owner}.encode.progress_bar_smooth 至少为 1")
    if not re.fullmatch(r"(0x|#)[0-9A-Fa-f]{6}", config.progress_bar_color):
        raise ConfigError(
            f"{owner}.encode.progress_bar_color 必须是 0xRRGGBB 或 #RRGGBB"
        )


def _validate_archive(config: ArchiveConfig, owner: str) -> None:
    if config.retention_days < 1:
        raise ConfigError(f"{owner}.archive.retention_days 至少为 1")
    if config.max_delete_per_run < 1:
        raise ConfigError(f"{owner}.archive.max_delete_per_run 至少为 1")


def _validate_append(config: AppendConfig, owner: str) -> None:
    if config.source not in ("raw", "pressed"):
        raise ConfigError(f"{owner}.append.source 只能是 raw 或 pressed")
    if config.enabled and not config.title_template:
        raise ConfigError(f"{owner}.append.title_template 不能为空")


def _validate_asr(config: AsrConfig, owner: str) -> None:
    if not config.enabled:
        return
    if not config.model.strip():
        raise ConfigError(f"{owner}.asr.model 不能为空")
    if config.device not in ("auto", "cpu", "cuda"):
        raise ConfigError(f"{owner}.asr.device 只能是 auto / cpu / cuda")
    if config.beam_size < 1:
        raise ConfigError(f"{owner}.asr.beam_size 至少为 1")
    if config.timeout <= 0:
        raise ConfigError(f"{owner}.asr.timeout 必须大于 0")


def _validate_webdav(config: WebdavConfig, owner: str) -> None:
    if not config.enabled:
        return
    if not config.url.strip():
        raise ConfigError(f"{owner}.webdav.url 不能为空")
    if not config.path.strip():
        raise ConfigError(f"{owner}.webdav.path 不能为空")
    if config.timeout <= 0:
        raise ConfigError(f"{owner}.webdav.timeout 必须大于 0")


# ---------------------------------------------------------------------------
# 取值辅助函数
#
# 每个函数都做类型检查，错误信息带上 “section.key”，方便定位。
# ---------------------------------------------------------------------------


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是一个表")
    return value


def _merge(base: dict[str, Any], override: Any, label: str) -> dict[str, Any]:
    """把主播子表合并到全局默认之上；override 为空时直接用默认。"""
    if override is None:
        return dict(base)
    if not isinstance(override, dict):
        raise ConfigError(f"{label} 必须是一个表")
    return {**base, **override}


def _get_str(section: dict[str, Any], section_name: str, key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{section_name}.{key} 必须是字符串")
    return value


def _get_required_str(section: dict[str, Any], section_name: str, key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section_name}.{key} 必须是非空字符串")
    return value


def _get_optional_int(
    section: dict[str, Any], section_name: str, key: str
) -> int | None:
    if key not in section:
        return None
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section_name}.{key} 必须是整数")
    return value


def _get_int(section: dict[str, Any], section_name: str, key: str, default: int) -> int:
    value = section.get(key, default)
    # bool 是 int 的子类，这里要排除，避免 true/false 被当成 0/1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section_name}.{key} 必须是整数")
    return value


def _get_bool(section: dict[str, Any], section_name: str, key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{section_name}.{key} 必须是 true/false")
    return value


def _get_float(
    section: dict[str, Any], section_name: str, key: str, default: float
) -> float:
    value = section.get(key, default)
    # bool 是 int 的子类，要排除；int 可以当 float 用
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{section_name}.{key} 必须是数字")
    return float(value)


def _get_str_list(
    section: dict[str, Any], section_name: str, key: str, default: list[str]
) -> list[str]:
    value = section.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{section_name}.{key} 必须是字符串数组")
    return list(value)


def _get_path(section: dict[str, Any], section_name: str, key: str, default: str) -> Path:
    return Path(_get_str(section, section_name, key, default))


def _get_path_list(
    section: dict[str, Any], section_name: str, key: str, default: list[str]
) -> list[Path]:
    return [Path(item) for item in _get_str_list(section, section_name, key, default)]


# ---------------------------------------------------------------------------
# 各段解析
# ---------------------------------------------------------------------------


def _parse_danmaku(section: dict[str, Any], label: str) -> DanmakuConfig:
    return DanmakuConfig(
        enabled=_get_bool(section, label, "enabled", True),
        factory=_get_path(section, label, "factory", "/usr/local/bin/DanmakuFactory"),
        font_name=_get_str(section, label, "font_name", "Noto Sans CJK SC"),
        font_size=_get_int(section, label, "font_size", 50),
        opacity=_get_int(section, label, "opacity", 200),
        max_diff_seconds=_get_int(section, label, "max_diff_seconds", 25),
    )


def _parse_encode(section: dict[str, Any], label: str) -> EncodeConfig:
    return EncodeConfig(
        crf=_get_int(section, label, "crf", 21),
        preset=_get_str(section, label, "preset", "veryfast"),
        profile=_get_str(section, label, "profile", "high"),
        threads=_get_int(section, label, "threads", 0),
        try_qsv=_get_bool(section, label, "try_qsv", True),
        qsv_quality=_get_int(section, label, "qsv_quality", 20),
        qsv_preset=_get_str(section, label, "qsv_preset", "medium"),
        progress_bar=_get_bool(section, label, "progress_bar", False),
        progress_bar_height=_get_int(section, label, "progress_bar_height", 64),
        progress_bar_opacity=_get_float(section, label, "progress_bar_opacity", 0.6),
        progress_bar_color=_get_str(section, label, "progress_bar_color", "0x2ED6FF"),
        progress_bar_smooth=_get_int(section, label, "progress_bar_smooth", 60),
    )


def _parse_upload(section: dict[str, Any], label: str) -> UploadConfig:
    return UploadConfig(
        enabled=_get_bool(section, label, "enabled", True),
        biliup=_get_path(section, label, "biliup", "/usr/local/bin/biliup"),
        cookies=_get_path(
            section, label, "cookies", "/data/cookies/bilibili/cookies-默认账号.json"
        ),
        tid=_get_int(section, label, "tid", 17),
        copyright=_get_int(section, label, "copyright", 2),
        source=_get_str(section, label, "source", ""),
        tags=_get_str_list(section, label, "tags", []),
        title_template=_get_str(section, label, "title_template", "{date} [{title}]"),
        desc_template=_get_str(section, label, "desc_template", ""),
    )


def _parse_archive(section: dict[str, Any], label: str) -> ArchiveConfig:
    return ArchiveConfig(
        cleanup_enabled=_get_bool(section, label, "cleanup_enabled", True),
        retention_days=_get_int(section, label, "retention_days", 15),
        max_delete_per_run=_get_int(section, label, "max_delete_per_run", 8),
        delete_after_upload=_get_bool(section, label, "delete_after_upload", False),
    )


def _parse_append(section: dict[str, Any], label: str) -> AppendConfig:
    return AppendConfig(
        enabled=_get_bool(section, label, "enabled", False),
        map_file=_get_str(section, label, "map_file", ""),
        source=_get_str(section, label, "source", "raw"),
        title_template=_get_str(
            section, label, "title_template", "{streamer} {month_cn} 直播回放合集"
        ),
        desc_template=_get_str(
            section,
            label,
            "desc_template",
            "本稿件为{streamer}{month_cn}原版直播回放合集，每次直播追加为一个分P。",
        ),
        cover=_get_str(section, label, "cover", ""),
    )


def _parse_asr(section: dict[str, Any], label: str) -> AsrConfig:
    return AsrConfig(
        enabled=_get_bool(section, label, "enabled", False),
        model=_get_str(section, label, "model", "large-v3-turbo"),
        language=_get_str(section, label, "language", "zh"),
        device=_get_str(section, label, "device", "auto"),
        compute_type=_get_str(section, label, "compute_type", "auto"),
        model_dir=_get_path(section, label, "model_dir", "/data/asr/models"),
        beam_size=_get_int(section, label, "beam_size", 5),
        hf_endpoint=_get_str(section, label, "hf_endpoint", ""),
        timeout=_get_int(section, label, "timeout", 14400),
    )


def _parse_webdav(section: dict[str, Any], label: str) -> WebdavConfig:
    return WebdavConfig(
        enabled=_get_bool(section, label, "enabled", False),
        url=_get_str(section, label, "url", ""),
        username=_get_str(section, label, "username", ""),
        password=_get_str(section, label, "password", ""),
        path=_get_str(
            section, label, "path", "/直播回放弹幕/{streamer}/{yyyy}/{mm}/{date}"
        ),
        public_url=_get_str(section, label, "public_url", ""),
        only_appended=_get_bool(section, label, "only_appended", False),
        insecure=_get_bool(section, label, "insecure", False),
        timeout=_get_int(section, label, "timeout", 60),
    )


def _parse_streamers(
    raw_streamers: Any,
    default_danmaku: dict[str, Any],
    default_encode: dict[str, Any],
    default_upload: dict[str, Any],
    default_archive: dict[str, Any],
    default_append: dict[str, Any],
    default_webdav: dict[str, Any],
    default_asr: dict[str, Any],
) -> list[StreamerConfig]:
    if not isinstance(raw_streamers, list):
        raise ConfigError("[[streamers]] 必须是数组")

    streamers: list[StreamerConfig] = []
    for index, item in enumerate(raw_streamers):
        label = f"streamers[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} 必须是表")

        streamer_id = _get_required_str(item, label, "id")
        streamers.append(
            StreamerConfig(
                id=streamer_id,
                name=_get_str(item, label, "name", streamer_id),
                aliases=_get_str_list(item, label, "aliases", []),
                enabled=_get_bool(item, label, "enabled", True),
                room_id=_get_optional_int(item, label, "room_id"),
                source_folders=_get_path_list(item, label, "source_folders", []),
                danmaku=_parse_danmaku(
                    _merge(default_danmaku, item.get("danmaku"), f"{label}.danmaku"),
                    f"{label}.danmaku",
                ),
                encode=_parse_encode(
                    _merge(default_encode, item.get("encode"), f"{label}.encode"),
                    f"{label}.encode",
                ),
                upload=_parse_upload(
                    _merge(default_upload, item.get("upload"), f"{label}.upload"),
                    f"{label}.upload",
                ),
                archive=_parse_archive(
                    _merge(default_archive, item.get("archive"), f"{label}.archive"),
                    f"{label}.archive",
                ),
                append=_parse_append(
                    _merge(default_append, item.get("append"), f"{label}.append"),
                    f"{label}.append",
                ),
                webdav=_parse_webdav(
                    _merge(default_webdav, item.get("webdav"), f"{label}.webdav"),
                    f"{label}.webdav",
                ),
                asr=_parse_asr(
                    _merge(default_asr, item.get("asr"), f"{label}.asr"),
                    f"{label}.asr",
                ),
            )
        )
    return streamers


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def load_config(path: Path) -> Config:
    """读取并校验配置文件。

    参数：
        path: TOML 文件路径

    异常：
        ConfigError: 文件不存在、TOML 语法错误或字段类型不对
    """
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件格式错误（{path}）：{exc}") from exc

    # ---- 全局 ----
    general_raw = _section(raw, "general")
    general = GeneralConfig(
        server_name=_get_str(general_raw, "general", "server_name", "nas"),
        work_dir=_get_path(general_raw, "general", "work_dir", "/data/work"),
        videos_dir=_get_path(general_raw, "general", "videos_dir", "/data/videos"),
        data_dir=_get_path(general_raw, "general", "data_dir", "/data/state"),
        log_dir=_get_path(general_raw, "general", "log_dir", "/data/logs"),
    )

    watch_raw = _section(raw, "watch")
    watch = WatchConfig(
        source_folders=_get_path_list(
            watch_raw, "watch", "source_folders", ["/data/rec"]
        ),
        poll_seconds=_get_int(watch_raw, "watch", "poll_seconds", 300),
        idle_minutes=_get_int(watch_raw, "watch", "idle_minutes", 20),
    )

    recorder_raw = _section(raw, "recorder")
    recorder = RecorderConfig(
        config_path=_get_path(
            recorder_raw, "recorder", "config_path", "/data/recorder/config.json"
        ),
        record_cookie_json=_get_path(
            recorder_raw,
            "recorder",
            "record_cookie_json",
            "/data/cookies/bilibili/cookies-录播账号.json",
        ),
    )

    github_raw = _section(raw, "github")
    sources_raw = github_raw.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigError("github.sources 必须是数组")
    sources: list[GithubSource] = []
    for index, item in enumerate(sources_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"github.sources[{index}] 必须是表")
        remote = item.get("remote")
        local = item.get("local")
        if not isinstance(remote, str) or not isinstance(local, str):
            raise ConfigError(f"github.sources[{index}] 需要 remote 和 local 两个字符串字段")
        sources.append(GithubSource(remote=remote, local=Path(local)))

    github = GithubConfig(
        enabled=_get_bool(github_raw, "github", "enabled", False),
        repo=_get_str(github_raw, "github", "repo", ""),
        branch=_get_str(github_raw, "github", "branch", "main"),
        token_file=_get_path(github_raw, "github", "token_file", "/app/.github_token"),
        sources=sources,
    )

    # ---- 主播（全局默认 + 覆盖） ----
    default_danmaku = _section(raw, "danmaku")
    default_encode = _section(raw, "encode")
    default_upload = _section(raw, "upload")
    default_archive = _section(raw, "archive")
    default_append = _section(raw, "append")
    default_webdav = _section(raw, "webdav")
    default_asr = _section(raw, "asr")

    streamers = _parse_streamers(
        raw.get("streamers", []),
        default_danmaku,
        default_encode,
        default_upload,
        default_archive,
        default_append,
        default_webdav,
        default_asr,
    )

    config = Config(
        general=general,
        watch=watch,
        recorder=recorder,
        github=github,
        streamers=streamers,
    )
    config.validate()
    return config
