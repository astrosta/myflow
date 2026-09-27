"""
RunContext
"""
from __future__ import annotations
from dataclasses import dataclass
import dataclasses
from agentkit.runtime.checkpointer.base import BaseCheckpointerSaver
from agentkit.runtime.store.thread import ThreadMetaStore
from agentkit.runtime.store.event import RunEventStore
from agentkit.runtime.config.app import AppConfig
from agentkit.runtime.config.agent import AgentFactory
from typing import Any

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
    checkpointer: BaseCheckpointerSaver
    thread_store: ThreadMetaStore
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