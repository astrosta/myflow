"""Read files from a thread's outputs directory."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from backend.app.gateway.deps import RunServices, get_run_services, require_scope
from backend.app.gateway.models.request import ThreadId
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.thread_access import require_visible_thread
from backend.app.gateway.thread_files import open_artifact

router = APIRouter(prefix="/threads", tags=["artifacts"])


@router.get("/{thread_id}/artifacts/{path:path}")
async def get_artifact(
    thread_id: ThreadId,
    path: str,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    download: Annotated[bool, Query()] = False,
) -> FileResponse:
    await require_visible_thread(services, thread_id, caller)
    opened = await open_artifact(thread_id, caller.user_id, path, download=download)
    return opened.response
