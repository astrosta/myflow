"""
FastAPI装配 + lifespan
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentkit.config import AppConfig, get_app_config
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from agentkit.persistence.recovery import gateway_worker_count, recover_inflight_runs
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.runtime import cancel_detached
from agentkit.runtime.checkpointer import make_checkpointer
from agentkit.persistence.events import make_run_event_store
from agentkit.runtime.run_services import build_run_services
from agentkit.persistence.run import make_run_store
from backend.app.gateway.auth.routers import router as auth_router
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.settings import AuthSettings, cors_origins_from_env
from backend.app.gateway.auth.store import make_user_store
from backend.app.gateway.auth_middleware import AuthGateMiddleware
from backend.app.gateway.csrf_middleware import CsrfMiddleware
from backend.app.gateway.errors import install_error_handlers
from backend.app.gateway.middleware.trace import TraceMiddleware
from backend.app.gateway.routers import (
    artifacts,
    mcp,
    models,
    skills,
    thread_runs,
    threads,
    uploads,
)

logger = logging.getLogger(__name__)


def preflight_auth(app_config: AppConfig) -> AuthSettings:
    """认证相关的启动预检（§2.5 / §2.11）。失败 = 拒绝启动，不是运行期 500。

    两条：
    - 认证开着就必须有像样的 `jwt_secret`（缺失或短于 32 字节直接报错）。
    - `GATEWAY_WORKERS>1` 且认证关着 → 拒绝启动：所有 worker 会共用同一个
      `default` 桶，"多用户"这个前提根本不成立。
    """
    settings = AuthSettings.from_config(app_config)
    settings.validate_for_startup()
    workers = gateway_worker_count()
    if not settings.enabled and workers > 1:
        raise RuntimeError(
            "auth.enabled=false with GATEWAY_WORKERS>1: every worker would share the same "
            "auth-disabled bucket. Enable auth or run a single worker (M9 §2.11)."
        )
    return settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the database before the checkpointer, and close it after."""
    app_config = get_app_config()
    preflight_auth(app_config)
    await init_engine_from_config(app_config.database)
    try:
        session_factory = get_session_factory()
        thread_meta = make_thread_meta_store(session_factory)
        run_store = make_run_store(session_factory)
        workers = gateway_worker_count()
        event_store = make_run_event_store(app_config.run_events, gateway_workers=workers)
        async with make_checkpointer(app_config) as checkpointer:
            services = build_run_services(
                app_config,
                checkpointer,
                thread_meta=thread_meta,
                run_store=run_store,
                event_store=event_store,
            )
            app.state.run_services = services
            # 账号表在 app 层（§1.2）：harness 不认识 UserRecord，只认识 CurrentUser Protocol。
            app.state.auth_service = AuthService(make_user_store(session_factory))
            recovered = await recover_inflight_runs(
                run_store, thread_meta, gateway_workers=workers
            )
            if recovered:
                logger.warning("recovered %s inflight runs as error after restart", recovered)
            try:
                yield
            finally:
                timeout = app_config.runs.shutdown_timeout_seconds
                await services.run_manager.shutdown(timeout=timeout if timeout is not None else 10.0)
                cancel_detached()
    finally:
        await close_engine()


def install_middleware(app: FastAPI, *, auth_settings: AuthSettings | None = None) -> None:
    """Starlette runs the last added middleware first（§12.1）。

    执行顺序是**倒过来**的：CORS 最先看到请求，Trace 最后包装。

    - `auth_settings` 显式钉住（测试 / 嵌入）时写进 `app.state`，
      否则中间件按请求解析 `get_app_config().auth`（配置热重载仍然生效）。
    - 读不到配置时中间件 fail-closed 返回 401，不会静默放行。
    """
    if auth_settings is not None:
        app.state.auth_settings = auth_settings
    app.add_middleware(TraceMiddleware)          # ① 最先加 = 最内层：后面所有日志都要 trace_id
    app.add_middleware(AuthGateMiddleware)       # ② M9：认证结果进 request.state.auth
    app.add_middleware(CsrfMiddleware)           # ③ M9：只对 Cookie 会话生效
    app.add_middleware(
        CORSMiddleware,
        # 默认 same-origin（nginx :2026）。拆 origin 的浏览器客户端必须配
        # GATEWAY_CORS_ORIGINS——CSRFMiddleware 读的是同一个变量（§2.6）。
        allow_origins=list(cors_origins_from_env()),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Location", "X-Trace-Id"],
    )


def create_app() -> FastAPI:
    application = FastAPI(
        title="agentkit gateway",
        version="1.0.0",
        redirect_slashes=False,
        lifespan=lifespan,
    )
    install_middleware(application)
    install_error_handlers(application)
    application.include_router(threads.router)
    application.include_router(thread_runs.router)
    application.include_router(models.router)
    application.include_router(uploads.router)
    application.include_router(artifacts.router)
    # M7 §2.4：两个 router 各自 prefix，/api 加在这里。
    application.include_router(mcp.router, prefix="/api")
    application.include_router(skills.router, prefix="/api")
    # M9 §2.12：认证路由 prefix=/v1/auth，/api 同样加在这里。
    application.include_router(auth_router, prefix="/api")
    return application


app = create_app()
