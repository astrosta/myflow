"""
RunContext
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from agentkit.config import AppConfig

if TYPE_CHECKING:
    from agentkit.persistence.thread_meta.base import ThreadMetaStore
    from agentkit.runtime.events.store.base import RunEventStore
    from agentkit.runtime.runs.store.base import RunStore
    from agentkit.runtime.runs.worker import AgentFactory

_PER_RUN_FIELDS = frozenset({
    "app_config",
    "agent_factory",
    "run_config",
    "graph_input",
    "stream_modes",
})


@dataclass(frozen=True, slots=True)
class RunContext:
    # --- 进程级：lifespan 建一次 ---
    checkpointer: BaseCheckpointSaver
    thread_store: ThreadMetaStore
    run_store: RunStore
    event_store: RunEventStore | None = None

    # --- 每个run 每次使用for_run更新---
    app_config: AppConfig | None = None
    agent_factory: AgentFactory | None = None
    run_config: dict | None = None
    graph_input: Any = None
    stream_modes: tuple[str, ...] = ()

    def for_run(self, **per_run: Any) -> RunContext:
        if missing := _PER_RUN_FIELDS - per_run.keys():
            raise ValueError(f"for_run() missing per-run fields: {sorted(missing)}")
        if extra := per_run.keys() - _PER_RUN_FIELDS:
            raise TypeError(f"for_run() cannot override infrastructure: {sorted(extra)}")

        return dataclasses.replace(self, **per_run)