"""Process-wide run services assembled once during lifespan."""

from __future__ import annotations

from dataclasses import dataclass

from agentkit.runtime.context import RunContext
from agentkit.persistence.events.base import RunEventStore
from agentkit.runtime.runs.manager import RunManager
from agentkit.persistence.run.base import RunStore
from agentkit.runtime.stream_bridge.base import StreamBridge
from agentkit.runtime.stream_bridge.provider import create_stream_bridge


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
    return RunServices(
        run_manager=RunManager(run_store),
        bridge=create_stream_bridge(app_config.stream_bridge),  # 进程级单例（§7.5）
        run_context=RunContext(
            checkpointer=checkpointer,
            thread_store=thread_meta,
            run_store=run_store,
            event_store=event_store,
        ),
        run_store=run_store,
        event_store=event_store,
    )
