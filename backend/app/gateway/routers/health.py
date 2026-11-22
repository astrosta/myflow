"""Public liveness and readiness probes for orchestrators."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from agentkit.persistence.engine import get_session_factory
from backend.app.gateway.deps import is_app_ready

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

#: A readiness probe must answer even when the database is black-holing connections.
_READY_TIMEOUT_SECONDS = 3.0


def _not_ready() -> JSONResponse:
    return JSONResponse(status_code=503, content={"status": "not_ready"})


async def _database_is_reachable() -> None:
    factory = get_session_factory()
    if factory is None:
        return
    async with factory() as session:
        await session.execute(text("SELECT 1"))


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process can serve HTTP. Never touches auth or dependencies."""

    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(ready: Annotated[bool, Depends(is_app_ready)]) -> JSONResponse:
    """Readiness: lifespan completed and the application database answers."""

    if not ready:
        return _not_ready()
    try:
        async with asyncio.timeout(_READY_TIMEOUT_SECONDS):
            await _database_is_reachable()
    except Exception as exc:  # noqa: BLE001 - every dependency failure means "not ready"
        logger.warning("readiness probe failed: %s", type(exc).__name__)
        return _not_ready()
    return JSONResponse({"status": "ok"})
