"""FastAPI 监控界面。

M1 功能：
    - 总览：watcher 心跳、磁盘、任务统计、录制目录、最近任务
    - 任务列表 / 任务详情（步骤、产物、日志尾部）
    - 日志查看

认证：不做，默认绑定 127.0.0.1；对外暴露请放在 Nginx/Caddy 后面。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config, ConfigError, load_config
from .web_data import (
    health,
    job_cover_path,
    job_detail,
    job_log_path,
    list_job_summaries,
    list_logs,
    load_job_safe,
    log_path,
    masked_config,
    overview,
    tail_text,
)

WEBUI_DIR = Path(__file__).parent / "webui"


def create_app(config: Config) -> FastAPI:
    app = FastAPI(
        title="stream-archive",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def no_cache_api(request, call_next):
        """API 和首页不缓存；静态资源每次都向服务端确认（304 即可）。

        否则反向代理/浏览器缓存了旧版 index.html，会和新版 app.js 对不上，
        页面会一直停在“加载中”。
        """
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/") or path == "/":
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    if WEBUI_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=WEBUI_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index() -> FileResponse:
        index_file = WEBUI_DIR / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=500, detail="webui/index.html 不存在")
        return FileResponse(index_file)

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        icon = WEBUI_DIR / "favicon.svg"
        if not icon.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(icon, media_type="image/svg+xml")

    # ------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        data = health(config)
        data["status"] = "ok" if data["watcher"]["alive"] else "degraded"
        return data

    @app.get("/api/health")
    def api_health() -> dict:
        return health(config)

    # ------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------
    @app.get("/api/overview")
    def api_overview() -> dict:
        return overview(config)

    # ------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs(
        status: str = "",
        streamer: str = "",
        q: str = "",
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict:
        jobs = list_job_summaries(config)
        streamers = sorted({job["streamer"] for job in jobs if job["streamer"]})

        if status:
            jobs = [job for job in jobs if job["status"] == status]
        if streamer:
            jobs = [
                job
                for job in jobs
                if job["streamer"] == streamer or job["streamer_id"] == streamer
            ]
        if q:
            needle = q.lower()
            jobs = [
                job
                for job in jobs
                if needle in job["id"].lower()
                or needle in job["stream_title"].lower()
                or needle in job["streamer"].lower()
            ]

        return {"jobs": jobs[:limit], "total": len(jobs), "streamers": streamers}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str, tail: int = Query(default=200, ge=1, le=2000)) -> dict:
        raw = load_job_safe(config, job_id)
        if not raw:
            raise HTTPException(status_code=404, detail="任务不存在")

        detail = job_detail(raw)
        path = job_log_path(config, job_id)
        detail["log_tail"] = tail_text(path, tail) if path else ""
        return detail

    @app.get("/api/jobs/{job_id}/log")
    def api_job_log(job_id: str, tail: int = Query(default=500, ge=1, le=5000)) -> dict:
        path = job_log_path(config, job_id)
        if not path or not path.is_file():
            raise HTTPException(status_code=404, detail="任务日志不存在")
        return {
            "job_id": job_id,
            "content": tail_text(path, tail),
            "size": path.stat().st_size,
        }

    @app.get("/api/jobs/{job_id}/cover")
    def api_job_cover(job_id: str) -> FileResponse:
        path = job_cover_path(config, job_id)
        if not path:
            raise HTTPException(status_code=404, detail="封面不存在")
        return FileResponse(path, media_type="image/jpeg")

    # ------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------
    @app.get("/api/logs")
    def api_logs() -> dict:
        return {"logs": list_logs(config)}

    @app.get("/api/logs/{name}")
    def api_log(name: str, tail: int = Query(default=500, ge=1, le=5000)) -> dict:
        path = log_path(config, name)
        if not path or not path.is_file():
            raise HTTPException(status_code=404, detail="日志不存在")
        return {
            "name": name,
            "content": tail_text(path, tail),
            "size": path.stat().st_size,
        }

    # ------------------------------------------------------------
    # 配置（脱敏）
    # ------------------------------------------------------------
    @app.get("/api/config")
    def api_config() -> dict:
        return masked_config(config)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stream-archive-web", description="stream-archive 监控界面"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("/data/config.toml"), help="配置文件"
    )
    parser.add_argument("--host", default="", help="覆盖 web.host")
    parser.add_argument("--port", type=int, default=0, help="覆盖 web.port")
    parser.add_argument(
        "--force", action="store_true", help="即使 [web].enabled=false 也启动"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2

    if not config.web.enabled and not args.force:
        print("配置里 [web].enabled=false，未启动。加 --force 可强制启动。")
        return 2

    import uvicorn

    host = args.host or config.web.host
    port = args.port or config.web.port
    print(f"stream-archive 监控界面：http://{host}:{port}")
    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
