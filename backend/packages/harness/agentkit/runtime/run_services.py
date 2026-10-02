"""Process-wide run services assembled once during lifespan."""

from __future__ import annotations

from dataclasses import dataclass

from agentkit.runtime.context import RunContext
from agentkit.runtime.events.store.base import RunEventStore
from agentkit.runtime.runs.manager import RunManager
from agentkit.runtime.runs.store.base import RunStore
from agentkit.runtime.stream_bridge.base import StreamBridge
from agentkit.runtime.stream_bridge.memory import MemoryStreamBridge


@dataclass(frozen=True, slots=True)
class RunServices:
    """Lifespan builds this once and stores it on ``app.state.run_services``."""

    run_manager: RunManager
    bridge: StreamBridge
    run_context: RunContext
    run_store: RunStore
    event_store: RunEventStore


def build_run_services(
    app_config,
    checkpointer,
    *,
    thread_meta,
    run_store: RunStore,
    event_store: RunEventStore,
) -> RunServices:
    """Wire the stores created after ``init_engine_from_config``."""
    del app_config
    return RunServices(
        run_manager=RunManager(run_store),
        bridge=MemoryStreamBridge(),
        run_context=RunContext(
            checkpointer=checkpointer,
            thread_store=thread_meta,
            run_store=run_store,
            event_store=event_store,
        ),
        run_store=run_store,
        event_store=event_store,
    )
