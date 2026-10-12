"""Thread visibility shared by lifecycle routes and uploads."""

from __future__ import annotations

from backend.app.gateway.deps import RunServices
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.services import ThreadNotFound


async def require_visible_thread(services: RunServices, thread_id: str, caller: RunCaller) -> None:
    """Missing and not-owned threads are both 404. The body does not say which.

    **admin 不是数据超级用户**（M9 §2.16）：他拿到的是和普通用户**完全一样**的 404。
    审计那条 `admin_cross_user_access_denied` 统一在错误处理器里写（`errors.py`）——
    写在每个路由里迟早会漏掉一处，而"漏掉"的表现是审计日志缺行，很难发现。
    """
    row = await services.run_context.thread_store.get(thread_id, user_id=caller.user_id)
    if row is None:
        raise ThreadNotFound(f"Thread {thread_id} not found")
