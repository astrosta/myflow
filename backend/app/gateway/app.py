"""
FastAPI装配 + lifespan
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentkit.agents.memory.backends.langmem.store import make_memory_store
from agentkit.agents.memory.maintenance.service import MemoryMaintenanceService
from agentkit.agents.memory.manager import (
    configure_memory_store,
    get_memory_manager,
    reset_memory_manager,
)
from agentkit.config import AppConfig, get_app_config
from agentkit.knowledge.service import configure_knowledge_service, reset_knowledge_service
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from agentkit.persistence.recovery import gateway_worker_count, recover_inflight_runs
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.runtime import cancel_detached
from agentkit.runtime.checkpointer import make_checkpointer
from agentkit.subagents.runtime import get_subagent_runtime
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
    knowledge,
    mcp,
    memory,
    models,
    skill_drafts,
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
            with make_memory_store(
                app_config.database,
                app_config.memory.backend_config if app_config.memory.enabled else {},
            ) as memory_store:
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
                # M10 §2.5.3 / §2.9.1：memory store 和 checkpointer 同一个 database 选择；
                # maintenance 是独立服务，不复用定时任务服务（那个会创建 thread/run）。
                configure_memory_store(memory_store)
                memory_manager = get_memory_manager(
                    store=memory_store, app_config=app_config
                )
                app.state.memory_manager = memory_manager
                # Knowledge metadata shares the app database; providers are built once.
                app.state.knowledge_config = app_config.knowledge_base
                app.state.knowledge_service = await configure_knowledge_service(
                    app_config.knowledge_base
                )
                maintenance = MemoryMaintenanceService(memory_manager)
                app.state.memory_maintenance = maintenance
                if app_config.memory.enabled and app_config.memory.maintenance.enabled:
                    maintenance.start()
                # M8 §10.1：进程级子代理容量控制器在这里冻结一次（subagent_runtime.* 是
                # startup-only）。建在启动时而不是"第一个启用子代理的 run"，是为了让 YAML
                # 里那个值在启动那一刻就定下来——热重载不能悄悄抬高已经宣告出去的容量。
                get_subagent_runtime(app_config)
                recovered = await recover_inflight_runs(
                    run_store, thread_meta, gateway_workers=workers
                )
                if recovered:
                    logger.warning("recovered %s inflight runs as error after restart", recovered)
                try:
                    yield
                finally:
                    maintenance.stop()
                    if not memory_manager.flush(
                        app_config.memory.ingest.shutdown_flush_timeout_seconds
                    ):
                        logger.warning("Memory extraction flush exceeded its shutdown budget")
                    reset_memory_manager()
                    await reset_knowledge_service()
                    app.state.knowledge_service = None
                    timeout = app_config.runs.shutdown_timeout_seconds
                    await services.run_manager.shutdown(timeout=timeout if timeout is not None else 10.0)
                    cancel_detached()
                    await services.bridge.aclose()  # 最后放掉桥的连接池（Redis 后端才有事可做）
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
    # 统一约定：router 只声明资源段（/threads、/models、/uploads…），/api 在这里加。
    # 少加一个就是 404 —— 浏览器请求的是 /api/models、/api/threads/{id}/uploads。
    application.include_router(threads.router, prefix="/api")
    application.include_router(thread_runs.router, prefix="/api")
    application.include_router(models.router, prefix="/api")
    application.include_router(uploads.router, prefix="/api")
    application.include_router(artifacts.router, prefix="/api")
    # M7 §2.4：两个 router 各自 prefix，/api 加在这里。
    application.include_router(mcp.router, prefix="/api")
    # Git-backed author API。放在旧 skills router 前：/api/skills/mine 必须先
    # 于任何 {skill_id} 路由注册，旧文件系统路由在 Task 15 前继续保留。
    application.include_router(skill_drafts.router, prefix="/api")
    application.include_router(skills.router, prefix="/api")
    # Human-facing knowledge base; same /api prefix as the rest of the browser API.
    application.include_router(knowledge.router, prefix="/api")
    # M10 §2.12：memory routes，/api 同样加在这里。
    application.include_router(memory.router, prefix="/api")
    # M9 §2.12：认证路由 prefix=/v1/auth，/api 同样加在这里。
    application.include_router(auth_router, prefix="/api")
    return application


app = create_app()
