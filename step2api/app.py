"""FastAPI 应用装配。

路由分工：

* ``/api/*``            —— 管理接口（令牌 ``STEP2API_ADMIN_TOKEN``）
* ``/step_plan/v1/*``   —— Step Plan 订阅通道，转发到
  ``https://api.stepfun.ai/step_plan/v1/*``
* ``/v1/*``             —— 按量计费通道，转发到 ``https://api.stepfun.ai/v1/*``
* ``/``                 —— Web 控制台（静态单页）
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import gateway_dep, router as manage_router
from .config import PORTAL_URL, get_settings
from .gateway import Gateway, GatewayResult
from .router import RouteContext, Router, select_channel
from .scheduler import Scheduler
from .store import Store, get_store

log = logging.getLogger("step2api")


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def build_app() -> FastAPI:
    settings = get_settings()
    _configure_logging()
    store: Store = get_store(settings)

    router_ = Router(store, settings)
    gateway = Gateway(store, settings, router_)
    scheduler = Scheduler(store, settings, router_)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await scheduler.start()
        log.info("step2api %s 已启动 → http://%s:%s", __version__, settings.host, settings.port)
        log.info("Step Plan 通道：%s  按量通道：%s", settings.plan_base, settings.upstream_base)
        try:
            yield
        finally:
            await scheduler.stop()
            await gateway.aclose()

    app = FastAPI(
        title="step2api",
        version=__version__,
        description="StepFun Step Plan 多账号聚合网关",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.state.settings = settings
    app.state.store = store
    app.state.router = router_
    app.state.gateway = gateway
    app.state.scheduler = scheduler
    app.state.version = __version__
    app.state.started_at = datetime.now(timezone.utc)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "x-step2api-account",
            "x-step2api-proxy",
            "x-step2api-attempt",
        ],
    )

    app.include_router(manage_router)

    # ------------------------------------------------------------------
    # 网关转发
    # ------------------------------------------------------------------

    def _log_result(
        result: GatewayResult, ctx: RouteContext, method: str, path: str
    ) -> None:
        if not settings.log_requests:
            return
        try:
            usage = result.usage or {}
            store.log_usage(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "account_id": result.account_id,
                    "account_name": result.account_name,
                    "session_key": ctx.session_key,
                    "method": method,
                    "path": path,
                    "model": ctx.model,
                    "channel": result.channel,
                    "status_code": result.status_code,
                    "duration_ms": round(result.duration_ms, 1),
                    "attempts": result.attempts,
                    "proxy_label": result.proxy_label,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "error": (result.error or "")[:500] or None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 记日志失败不能影响转发
            log.debug("写日志失败：%s", exc)

    async def _forward(request: Request, path: str):
        return await gateway.forward(request, path, log_callback=_log_result)

    # 注意：这里必须是带 Request 类型标注的具名函数。用 lambda 或省略注解时，
    # FastAPI 会把 request 当成查询参数，导致所有转发请求 422。

    async def _openai_route(request: Request, path: str):
        """按量计费通道：/v1/*"""
        return await _forward(request, f"/v1/{path}")

    async def _plan_v1_route(request: Request, path: str):
        """Step Plan 订阅通道：/step_plan/v1/*"""
        return await _forward(request, f"/step_plan/v1/{path}")

    async def _plan_root_route(request: Request, path: str):
        """不带 /v1 的 Step Plan 根路径，兼容直接把 Base URL 指向网关的客户端"""
        suffix = f"/{path}" if path else ""
        return await _forward(request, f"/step_plan{suffix}")

    app.add_api_route(
        "/v1/{path:path}", _openai_route, methods=["POST", "GET"],
        dependencies=gateway_dep, include_in_schema=False,
    )
    app.add_api_route(
        "/step_plan/v1/{path:path}", _plan_v1_route, methods=["POST", "GET"],
        dependencies=gateway_dep, include_in_schema=False,
    )
    app.add_api_route(
        "/step_plan/{path:path}", _plan_root_route, methods=["POST", "GET"],
        dependencies=gateway_dep, include_in_schema=False,
    )

    # ------------------------------------------------------------------
    # 静态控制台
    # ------------------------------------------------------------------
    static_dir = settings.static_dir
    if static_dir.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir)),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(str(static_dir / "index.html"))

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon():
            icon = static_dir / "favicon.svg"
            if icon.exists():
                return FileResponse(str(icon), media_type="image/svg+xml")
            return JSONResponse(status_code=204, content=None)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("未处理异常 %s %s：%s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"type": "internal_error", "message": str(exc)}},
        )

    return app


app = None  # 由 __main__ 或 uvicorn 工厂创建


def create_app() -> FastAPI:
    return build_app()


__all__ = ["build_app", "create_app", "PORTAL_URL", "select_channel"]
