"""Pick the ledger backend once. Callers keep the RunEventStore they were given.

具体后端**懒加载**（PEP 562）：memory / jsonl 两个后端不该被 DB 栈拖住，
`runtime.events.journal` 也不该在 import 它的时候顺着 db → persistence →
runtime.runs 绕回 worker（那是循环导入）。
"""

from __future__ import annotations

import logging
from importlib import import_module

from agentkit.config.run_events_config import RunEventsConfig
from agentkit.persistence.engine import get_session_factory
from agentkit.persistence.events.base import RunEventSeqConflict, RunEventStore

logger = logging.getLogger(__name__)

_LAZY_BACKENDS = {
    "DbRunEventStore": "agentkit.persistence.events.db",
    "JsonlRunEventStore": "agentkit.persistence.events.jsonl",
    "MemoryRunEventStore": "agentkit.persistence.events.memory",
    # ORM 行也走懒加载：memory / jsonl 的使用者不该为了 import 这个包就拉进 ORM。
    "RunEventRow": "agentkit.persistence.events.model",
}

_UNSET = object()

__all__ = [
    "DbRunEventStore",
    "JsonlRunEventStore",
    "MemoryRunEventStore",
    "RunEventRow",
    "RunEventSeqConflict",
    "RunEventStore",
    "make_run_event_store",
]


def __getattr__(name: str):
    """PEP 562：`from agentkit.persistence.events import MemoryRunEventStore` 照常可用。"""
    module_name = _LAZY_BACKENDS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)


def make_run_event_store(
    config: RunEventsConfig,
    *,
    session_factory=_UNSET,
    gateway_workers: int = 1,
    jsonl_base_dir: str | None = None,
) -> RunEventStore:
    """memory / jsonl cannot be shared across gateway workers. db without a factory degrades to memory."""
    from agentkit.persistence.events.db import DbRunEventStore
    from agentkit.persistence.events.jsonl import JsonlRunEventStore
    from agentkit.persistence.events.memory import MemoryRunEventStore

    if session_factory is _UNSET:
        session_factory = get_session_factory()
    if gateway_workers > 1 and config.backend != "db":
        raise RuntimeError(
            "GATEWAY_WORKERS > 1 requires run_events.backend=db; "
            "memory and jsonl ledgers diverge across processes"
        )
    if config.backend == "memory":
        return MemoryRunEventStore()
    if config.backend == "jsonl":
        return JsonlRunEventStore(jsonl_base_dir or ".", max_trace_content=None)
    if session_factory is None:
        if gateway_workers > 1:
            raise RuntimeError(
                "GATEWAY_WORKERS > 1 requires run_events.backend=db and a database session factory"
            )
        logger.warning(
            "run_events.backend is db but no database session factory is available; using memory"
        )
        return MemoryRunEventStore()
    return DbRunEventStore(session_factory, max_trace_content=config.max_trace_content)
