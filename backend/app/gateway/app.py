"""
FastAPI装配 + lifespan
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from agentkit.runtime.runs.services import build_run_services
from agentkit.runtime.config import get_app_config
from agentkit.runtime.task import cancel_detached
from agentkit.runtime.checkpointer import close_checkpointer

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.run_services = services = build_run_services(get_app_config())
    try:
        yield
    finally:
        timeout = get_app_config().runs.shutdown_timeout_seconds
        await services.run_manager.shutdown(timeout=timeout)          # ① 拒新准入、置 abort、等 worker 收尾（§5.9）
        cancel_detached()                                             # ② 还没到点的桥缓冲清理，随进程一起丢
        # M4：关事件账本；M9 §3：停心跳、抽干孤儿回收、停扩展 / 通道 / 调度器
        await close_checkpointer(services.run_context.checkpointer)    # ③ 最后才关 checkpointer 连接池