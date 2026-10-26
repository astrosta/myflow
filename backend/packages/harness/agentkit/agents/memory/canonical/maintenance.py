"""Background maintenance for canonical memory.

The worker closes idle episode sessions, expires pending semantic facts, and
drains episodic ingest jobs. It never calls an operator inline in the chat
request path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from agentkit.agents.memory.canonical.episodic import GateSignals
from agentkit.agents.memory.types import utc_now
from agentkit.config.memory_config import MemoryConfig

logger = logging.getLogger(__name__)


class MemoryMaintenanceWorker:
    def __init__(self, service: object, config: MemoryConfig, *, interval_seconds: float = 60.0) -> None:
        self._service = service
        self._config = config
        self._interval = max(1.0, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None

    async def run_once(self, *, now: datetime | None = None, max_jobs: int = 10) -> dict[str, int]:
        service = self._service
        current = now or utc_now()
        closed = await service.sessions.close_idle_sessions(current)  # type: ignore[attr-defined]
        expired = await service.semantic.expire_pending(now=current)  # type: ignore[attr-defined]
        processed = 0
        for _ in range(max_jobs):
            job = await service.repo.claim_ingest_job(  # type: ignore[attr-defined]
                worker_id="memory-maintenance", lease_seconds=120
            )
            if job is None:
                break
            # v1 uses trace-level signals from the captured turn. A later gate
            # calibration can replace this with a run/tool signal extractor.
            result = await service.episodic.process_ingest_job(  # type: ignore[attr-defined]
                job.id, signals=GateSignals(tool_error=True, retry=True)
            )
            if result is not None:
                processed += 1
        return {"closed": closed, "expired": expired, "episodes": processed}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="memory-maintenance")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a background loop must survive
                logger.warning("memory maintenance failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._interval)
