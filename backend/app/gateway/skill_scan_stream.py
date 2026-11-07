"""SSE framing for the skill security scan.

Kept outside routers on purpose: routers declare resources and map errors; they
do not own task scheduling or JSON encoding (see test_m3_routes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from collections.abc import AsyncIterator, Callable
from typing import Any

from agentkit.skills.author import SkillAuthorService
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SecurityScanRecord,
    SkillConflict,
    SkillScanUnavailable,
)

logger = logging.getLogger(__name__)


def _sse(event: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def scan_event_stream(
    service: SkillAuthorService,
    *,
    owner_user_id: str,
    skill_id: str,
    render_result: Callable[[SecurityScanRecord], dict[str, Any]],
) -> AsyncIterator[str]:
    """Push scan phases, then the same JSON body the non-stream route returns."""

    async def events() -> AsyncIterator[str]:
        # queue.Queue, not asyncio.Queue: the scanner thread must enqueue a
        # status before scan() returns, or the sentinel can overtake it.
        statuses: queue.Queue[str | None] = queue.Queue()

        def on_status(message: str) -> None:
            text = str(message).strip()[:200]
            if text:
                statuses.put(text)

        async def work() -> SecurityScanRecord:
            try:
                return await service.scan_security(
                    owner_user_id=owner_user_id,
                    skill_id=skill_id,
                    on_status=on_status,
                )
            finally:
                statuses.put(None)

        logger.info("Security scan started for skill %s", skill_id)
        task = asyncio.create_task(work())
        try:
            while True:
                message = await asyncio.to_thread(statuses.get)
                if message is None:
                    break
                yield _sse("status", {"message": message})
            scan = await task
        except SkillScanUnavailable as exc:
            yield _sse(
                "error",
                {"status": 503, "code": "skill_scanner_unavailable", "detail": str(exc)},
            )
            return
        except SkillConflict as exc:
            yield _sse("error", {"status": 409, "code": "skill_conflict", "detail": str(exc)})
            return
        except InvalidSkillPackage as exc:
            yield _sse(
                "error",
                {"status": 422, "code": "invalid_skill_package", "detail": str(exc)},
            )
            return
        except Exception:
            logger.exception("Security scan failed for skill %s", skill_id)
            yield _sse(
                "error",
                {"status": 500, "code": "internal_error", "detail": "Internal server error"},
            )
            return
        logger.info(
            "Security scan finished for skill %s status=%s duration_ms=%s",
            skill_id,
            scan.status,
            scan.duration_ms,
        )
        yield _sse("result", render_result(scan))

    return events()


__all__ = ["scan_event_stream"]
