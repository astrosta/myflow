"""
FastAPI装配 + lifespan
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentkit.config import get_app_config
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from agentkit.persistence.recovery import gateway_worker_count, recover_inflight_runs
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.runtime import cancel_detached
from agentkit.runtime.checkpointer import make_checkpointer
from agentkit.runtime.events.store import make_run_event_store
from agentkit.runtime.run_services import build_run_services
from agentkit.runtime.runs.store import make_run_store
from backend.app.gateway.errors import install_error_handlers
from backend.app.gateway.middleware.trace import TraceMiddleware
from backend.app.gateway.routers import (
    artifacts,
    models,
    thread_runs,
    threads,
    uploads,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the database before the checkpointer, and close it after."""
    app_config = get_app_config()
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


def install_middleware(app: FastAPI) -> None:
    """Starlette runs the last added middleware first. Trace stays inside CORS."""
    app.add_middleware(TraceMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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
    return application


app = create_app()
