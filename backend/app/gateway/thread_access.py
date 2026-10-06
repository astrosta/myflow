"""Thread visibility shared by lifecycle routes and uploads."""

from __future__ import annotations

from backend.app.gateway.deps import RunServices
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.services import ThreadNotFound


async def require_visible_thread(services: RunServices, thread_id: str, caller: RunCaller) -> None:
    """Missing and not-owned threads are both 404. The body does not say which."""
    row = await services.run_context.thread_store.get(thread_id, user_id=caller.user_id)
    if row is None:
        raise ThreadNotFound(f"Thread {thread_id} not found")
