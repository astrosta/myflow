"""
FastAPI装配 + lifespan
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext, suppress
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentkit.agents.memory.openviking.client import OpenVikingClientFactory
from agentkit.agents.memory.openviking.profile import render_profile_prompt
from agentkit.agents.memory.openviking.profile_prompt import (
    configure_profile_prompt_provider,
)
from agentkit.agents.memory.openviking.profile_tools import configure_profile_updater
from agentkit.agents.memory.openviking.runtime import (
    OpenVikingMemoryRuntime,
    build_profile_services,
    configure_openviking_memory,
)
from agentkit.contextforge.runtime import (
    ContextForgeRuntime,
    build_contextforge_runtime,
    reset_contextforge_runtime,
)
from agentkit.config import AppConfig, get_app_config
from agentkit.observability import configure_observability, shutdown_observability
from agentkit.knowledge.service import configure_knowledge_service, reset_knowledge_service
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from agentkit.persistence.recovery import gateway_worker_count, recover_inflight_runs
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.runtime import cancel_detached
from agentkit.runtime.checkpointer import make_checkpointer
from agentkit.sandbox.runtime import shutdown_sandbox_provider, start_sandbox_provider
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
    memory_items,
    models,
    openviking_admin,
    openviking_profile,
    skill_drafts,
    skills,
    thread_runs,
    threads,
    tools,
    uploads,
)
from backend.app.gateway.skills_catalog import (
    SkillRuntime,
    build_skill_runtime,
    configure_skill_runtime,
    reset_skill_runtime,
)

logger = logging.getLogger(__name__)


async def start_skill_runtime(app: FastAPI, app_config: AppConfig) -> SkillRuntime | None:
    """Build and start the Git-backed catalog runtime (lifespan-owned).

    filesystem 来源只保留 M7 的扫描器：不建 runtime、不读 Git、不挂 active 树。
    Git 来源在数据库就绪后构建运行时，先刷新快照再启动 outbox worker；任何一步
    失败都让启动失败——半启动的技能目录比不启动更难排查。所以 start() 抛错时
    先把已经装上的运行时停掉并清空注册表，再让异常继续向上冒（否则 outbox 任务
    会活到事件循环关闭，对着已经释放的 engine 轮询）。
    """
    if app_config.skills.source != "git":
        app.state.skill_runtime = None
        return None
    runtime = build_skill_runtime(app_config)
    app.state.skill_runtime = runtime
    app.state.skill_author_service = runtime.author
    app.state.skill_catalog = runtime.catalog
    app.state.skill_search = runtime.search
    configure_skill_runtime(runtime)
    try:
        await runtime.start()
    except BaseException:
        logger.warning("Skill runtime failed to start; tearing it down", exc_info=True)
        try:
            await stop_skill_runtime(app)
        except Exception:  # noqa: BLE001 - cleanup must not mask the startup failure
            logger.exception("Stopping the failed skill runtime also failed")
        raise
    return runtime


async def start_contextforge_runtime(app: FastAPI, app_config: AppConfig) -> None:
    """Build the process tool runtime. Failing to reach ContextForge is non-fatal."""
    runtime: ContextForgeRuntime | None = None
    try:
        runtime = build_contextforge_runtime(app_config.contextforge)
        app.state.contextforge = runtime
        if runtime is not None:
            await runtime.start()
    except Exception:  # noqa: BLE001 - tools degrade, the gateway still starts
        logger.warning("ContextForge runtime failed to start", exc_info=True)
        app.state.contextforge = runtime


async def stop_contextforge_runtime(app: FastAPI) -> None:
    runtime = getattr(app.state, "contextforge", None)
    try:
        if runtime is not None:
            await runtime.stop()
    finally:
        app.state.contextforge = None
        reset_contextforge_runtime()


def start_profile_services(
    app: FastAPI,
    app_config: AppConfig,
    session_factory: Any,
) -> None:
    """Build the DB-backed profile/onboarding surface even when memory is off.

    The profile is user-owned identity data, not an OpenViking memory type. It
    stays available with the OpenViking switch disabled; suggestions simply have
    no memory index to inspect in that mode.
    """

    if session_factory is None:
        configure_profile_prompt_provider(None)
        configure_profile_updater(None)
        app.state.profile_service = None
        app.state.onboarding_service = None
        app.state.suggestion_service = None
        return
    services = build_profile_services(app_config.memory.openviking, session_factory)
    app.state.profile_service = services.profile
    app.state.onboarding_service = services.onboarding
    app.state.suggestion_service = services.suggestions

    async def prompt(user_id: str) -> str:
        return render_profile_prompt(
            await services.profile.get_snapshot(user_id),
            max_tokens=app_config.memory.openviking.personalization.max_tokens,
        )

    configure_profile_prompt_provider(prompt)
    # 画像工具写的是同一行；这里显式装 provider，让 memory 开关关闭时也能记录
    # 用户明确说出的称呼/语言。
    configure_profile_updater(services.profile)


async def stop_observability() -> None:
    """Flush/close the Langfuse exporter without blocking process teardown."""

    with suppress(Exception):
        await asyncio.wait_for(asyncio.to_thread(shutdown_observability), timeout=5.0)


async def stop_skill_runtime(app: FastAPI) -> None:
    """Stop the outbox worker, close the projection, and reset catalog state."""
    runtime = getattr(app.state, "skill_runtime", None)
    try:
        if runtime is not None:
            await runtime.stop()
    finally:
        # 清理是无条件的：outbox.stop() 或 projection.aclose() 抛错也不能把一个
        # 半死的运行时留在 app.state / 进程注册表里。
        app.state.skill_runtime = None
        app.state.skill_author_service = None
        app.state.skill_catalog = None
        app.state.skill_search = None
        reset_skill_runtime()


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


async def start_openviking_memory(
    app: FastAPI,
    app_config: AppConfig,
    session_factory: Any,
) -> tuple[OpenVikingMemoryRuntime | None, Any]:
    """Build and start the OpenViking runtime when the explicit switch is on."""

    config = app_config.memory.openviking
    if not config.enabled:
        return None, None
    if not config.capture_stream.redis_url:
        raise RuntimeError(
            "memory.openviking.enabled=true requires memory.openviking.capture_stream.redis_url"
        )
    if session_factory is None:
        raise RuntimeError("memory.openviking.enabled=true requires a persistent database")
    if len(config.user_id_salt) < 32:
        raise RuntimeError(
            "memory.openviking.enabled=true requires memory.openviking.user_id_salt "
            "with at least 32 characters"
        )

    from redis.asyncio import Redis

    redis = Redis.from_url(config.capture_stream.redis_url, decode_responses=True)
    try:
        await redis.ping()
    except BaseException:
        await redis.aclose()
        raise
    client_factory = OpenVikingClientFactory(config)
    runtime = OpenVikingMemoryRuntime.build(
        config,
        session_factory,
        client_factory,
        redis=redis,
        skill_author=getattr(app.state, "skill_author_service", None),
    )
    try:
        await runtime.start()
    except BaseException:
        await runtime.stop()
        await redis.aclose()
        raise
    app.state.openviking_memory = runtime
    app.state.profile_service = runtime.profile
    app.state.onboarding_service = runtime.onboarding
    app.state.suggestion_service = runtime.suggestions

    async def prompt(user_id: str) -> str:
        return render_profile_prompt(
            await runtime.profile.get_snapshot(user_id),
            max_tokens=runtime.config.personalization.max_tokens,
        )

    configure_profile_prompt_provider(prompt)
    configure_profile_updater(runtime.profile)
    configure_openviking_memory(runtime)
    logger.info("OpenViking long-term memory runtime started")
    return runtime, redis


async def stop_openviking_memory(
    app: FastAPI,
    runtime: OpenVikingMemoryRuntime | None,
    redis: Any,
) -> None:
    try:
        if runtime is not None:
            await runtime.stop()
    finally:
        if redis is not None:
            await redis.aclose()
        configure_profile_prompt_provider(None)
        configure_profile_updater(None)
        configure_openviking_memory(None)
        app.state.openviking_memory = None
        app.state.profile_service = None
        app.state.onboarding_service = None
        app.state.suggestion_service = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the database before the checkpointer, and close it after."""
    app_config = get_app_config()
    preflight_auth(app_config)
    await init_engine_from_config(app_config.database)
    try:
        app.state.observability = configure_observability(app_config)
        session_factory = get_session_factory()
        thread_meta = make_thread_meta_store(session_factory)
        run_store = make_run_store(session_factory)
        workers = gateway_worker_count()
        event_store = make_run_event_store(app_config.run_events, gateway_workers=workers)
        async with make_checkpointer(app_config) as checkpointer:
            with nullcontext():
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
                # M11 Task 15：数据库就绪后构建 Git 技能目录运行时——刷新快照、物化
                # active 挂载树、启动 outbox worker。filesystem 来源在这里是 no-op。
                await start_skill_runtime(app, app_config)
                # 运行时一旦装进 app.state，后面任何一步启动失败都必须把它拆掉：
                # 否则 outbox worker 会活到事件循环关闭，对着已经释放的 engine 轮询。
                try:
                    # M12：进程级 sandbox provider 在这里冻结并做启动自检（远程集群健康、
                    # lease Redis、挂载源）。启动**不建**沙箱——第一个沙箱由第一次 acquire
                    # 建；自检失败就让启动失败（skill runtime 由下面这层 finally 拆掉），
                    # 而不是等第一个 run 报错。
                    start_sandbox_provider(app_config=app_config)
                    # M10 §2.5.3 / §2.9.1：memory store 和 checkpointer 同一个 database 选择；
                    # maintenance 是独立服务，不复用定时任务服务（那个会创建 thread/run）。
                    app.state.memory_manager = None
                    app.state.memory_service = None
                    app.state.memory_projector = None
                    openviking_runtime = None
                    openviking_redis = None
                    if app_config.memory.openviking.enabled:
                        openviking_runtime, openviking_redis = await start_openviking_memory(
                            app, app_config, session_factory
                        )
                    else:
                        start_profile_services(app, app_config, session_factory)
                    # Knowledge metadata shares the app database; providers are built once.
                    app.state.knowledge_config = app_config.knowledge_base
                    app.state.knowledge_service = await configure_knowledge_service(
                        app_config.knowledge_base
                    )
                    await start_contextforge_runtime(app, app_config)
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
                        if openviking_runtime is not None:
                            await stop_openviking_memory(
                                app, openviking_runtime, openviking_redis
                            )
                        if openviking_runtime is None:
                            configure_profile_prompt_provider(None)
                            configure_profile_updater(None)
                            app.state.profile_service = None
                            app.state.onboarding_service = None
                            app.state.suggestion_service = None
                        await reset_knowledge_service()
                        app.state.knowledge_service = None
                        timeout = app_config.runs.shutdown_timeout_seconds
                        # 先排空 run manager，再停技能运行时：优雅停机期间还在跑的 run
                        # 仍要读快照和检索服务，先拆掉会让它们半路失明。
                        await services.run_manager.shutdown(timeout=timeout if timeout is not None else 10.0)
                        # run manager 排空之后才拆 provider：还在收尾的 run 仍可能要
                        # release 自己的沙箱。reset 顺带 shutdown（本地杀进程，远程 kill 容器），
                        # 那是网络 IO——必须在线程池里跑，不能占着正在收尾的事件循环。
                        await shutdown_sandbox_provider()
                        await stop_contextforge_runtime(app)
                        await stop_skill_runtime(app)
                        cancel_detached()
                        await services.bridge.aclose()  # 最后放掉桥的连接池（Redis 后端才有事可做）
                        # run manager 已排空；现在才 flush/关掉 Langfuse exporter 线程。
                        await stop_observability()
                finally:
                    await stop_skill_runtime(app)
    finally:
        await stop_observability()
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


def openviking_memory_enabled() -> bool:
    """Return the explicit long-term-memory switch.

    The ASGI module builds `app` at import time, and several tests import it
    without a complete config (missing file or unresolved environment
    variables). Route inventory must still be constructible; those cases keep
    the profile-only surface and no OpenViking admin route. The lifespan runs
    the same loader and still fails loudly before serving a single request, so
    a broken production config cannot silently run with the wrong backend.
    """

    try:
        return get_app_config().memory.openviking.enabled
    except (FileNotFoundError, ValueError):
        return False


def create_app(*, use_openviking_memory: bool | None = None) -> FastAPI:
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
    # Git-backed author API. Must be registered before the generic skills router:
    # /api/skills/mine has to win over any /skills/{skill_id} route.
    application.include_router(skill_drafts.router, prefix="/api")
    application.include_router(skills.router, prefix="/api")
    # Human-facing knowledge base; same /api prefix as the rest of the browser API.
    application.include_router(tools.router, prefix="/api")
    application.include_router(knowledge.router, prefix="/api")
    # The profile/onboarding surface is DB-backed identity data and is always
    # available. The OpenViking switch additionally mounts admin status/flush;
    # the legacy canonical memory routes are retired.
    if use_openviking_memory is None:
        use_openviking_memory = openviking_memory_enabled()
    application.include_router(openviking_profile.router, prefix="/api")
    application.include_router(memory_items.router, prefix="/api")
    if use_openviking_memory:
        application.include_router(openviking_admin.router, prefix="/api")
    # M9 §2.12：认证路由 prefix=/v1/auth，/api 同样加在这里。
    application.include_router(auth_router, prefix="/api")
    return application


app = create_app()