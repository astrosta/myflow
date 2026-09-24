from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain_core.callbacks import adispatch_custom_event, dispatch_custom_event
from langgraph.errors import GraphBubbleUp

logger = logging.getLogger(__name__)

StreamWriter = Callable[[Any], None]


def emit_custom_event(payload: dict[str, Any], *, writer: StreamWriter) -> None:
    """生产代码发 custom 事件的唯一出口，不要直接调 writer。"""
    name = _require_event_type(payload)
    writer(payload)
    try:
        dispatch_custom_event(name, payload)
    except GraphBubbleUp:
        raise
    except Exception:
        logger.debug("Callback dispatch failed for custom event %s", name, exc_info=True)


async def aemit_custom_event(payload: dict[str, Any], *, writer: StreamWriter) -> None:
    """与同步版逐行对应，第 3 步换成 await adispatch_custom_event。"""
    name = _require_event_type(payload)
    writer(payload)
    try:
        await adispatch_custom_event(name, payload)
    except GraphBubbleUp:
        raise
    except Exception:
        logger.debug("Callback dispatch failed for custom event %s", name, exc_info=True)


def _require_event_type(payload: Any) -> str:
    event_type = payload.get("type") if isinstance(payload, dict) else None
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("custom event payload must carry a non-empty string 'type'")
    return event_type
