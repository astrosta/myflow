"""LangGraph runtime context goes to astream(context=), not configurable."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agentkit.runtime.context import RunContext
from agentkit.runtime.runs.manager import RunRecord
from agentkit.runtime.runs.schemas import RunStatus
from agentkit.runtime.runs.usage import UsageCollector
from agentkit.runtime.runs.worker import _stream_graph


def test_stream_graph_passes_context_to_astream() -> None:
    captured: dict = {}

    async def astream(
        input, config=None, *, context=None, stream_mode=None, subgraphs=False, version=None
    ):
        captured["input"] = input
        captured["config"] = config
        captured["context"] = context
        captured["stream_mode"] = stream_mode
        captured["subgraphs"] = subgraphs
        captured["version"] = version
        if False:
            yield None

    record = RunRecord(
        run_id="run-1",
        thread_id="t1",
        assistant_id=None,
        status=RunStatus.running,
        on_disconnect="continue",
        user_id="alice",
    )
    run_config = {
        "context": {"user_id": "alice", "model_name": "demo"},
        "configurable": {},
    }
    ctx = RunContext(
        checkpointer=SimpleNamespace(),
        thread_store=SimpleNamespace(),
        run_store=SimpleNamespace(),
        run_config=run_config,
        graph_input={"messages": []},
        stream_modes=("values",),
    )
    usage = UsageCollector(SimpleNamespace(), "run-1")
    bridge = SimpleNamespace(publish=AsyncMock())

    asyncio.run(_stream_graph(bridge, record, ctx, SimpleNamespace(astream=astream), usage, None))

    assert captured["context"]["thread_id"] == "t1"
    assert captured["context"]["run_id"] == "run-1"
    assert captured["context"]["user_id"] == "alice"
    assert captured["context"]["model_name"] == "demo"
    assert "__pregel_runtime" not in (captured["config"] or {}).get("configurable", {})
    assert "context" not in (captured["config"] or {})
    assert captured["version"] == "v2", "帧形状走 LangGraph v2（unpack_stream_part 认的是它）"
