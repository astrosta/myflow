import asyncio
import contextvars
from typing import Any, Coroutine

_DETACHED: set[asyncio.Task] = set()


def spawn_detached(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """不属于任何请求的后台任务：空 context、留强引用。"""
    task = asyncio.create_task(coro, context=contextvars.Context())
    _DETACHED.add(task)
    task.add_done_callback(_DETACHED.discard)
    return task


def cancel_detached() -> None:
    for task in list(_DETACHED):
        task.cancel()