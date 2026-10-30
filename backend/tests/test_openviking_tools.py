"""Model-facing read-only memory tools for the OpenViking adapter."""

from __future__ import annotations

import json

import pytest
from langchain.tools import ToolRuntime

from agentkit.agents.memory.openviking.contracts import MemoryHit
from agentkit.agents.memory.openviking.tools import (
    make_memory_overview_tool,
    make_memory_read_tool,
    make_memory_search_tool,
)


class FakeFacade:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def search(self, user_id, query, *, memory_types, limit=8, since=None, until=None, time_field=None):
        self.calls.append(("search", user_id, query, memory_types, limit))
        return [
            MemoryHit(
                uri="viking://user/u/memories/preferences/style.md",
                memory_type="preferences",
                abstract="likes concise",
                score=0.7,
                level="L0",
                tags=("kind=style",),
            )
        ]

    async def read(self, user_id, uri, *, max_chars=8000):
        self.calls.append(("read", user_id, uri, max_chars))
        return "<memory-context>body</memory-context>"

    async def overview(self, user_id, uri):
        self.calls.append(("overview", user_id, uri))
        return "<memory-context>overview</memory-context>"


def runtime(context):
    return ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id=None,
        store=None,
    )


@pytest.mark.asyncio
async def test_search_tool_maps_hits_and_user() -> None:
    facade = FakeFacade()
    tool = make_memory_search_tool(facade)
    result = json.loads(
        await tool.ainvoke(
            {
                "query": "style",
                "memory_types": ["preferences"],
                "runtime": runtime({"user_id": "u1"}),
            }
        )
    )
    assert result["items"][0]["memory_type"] == "preferences"
    assert result["items"][0]["tags"] == ["kind=style"]
    assert facade.calls[0][1] == "u1"


@pytest.mark.asyncio
async def test_read_and_overview_tools_require_user() -> None:
    facade = FakeFacade()
    read = make_memory_read_tool(facade)
    overview = make_memory_overview_tool(facade)

    missing = json.loads(await read.ainvoke({"uri": "viking://user/u/memories/x.md", "runtime": runtime({})}))
    assert "error" in missing
    assert await read.ainvoke({"uri": "viking://user/u/memories/x.md", "runtime": runtime({"user_id": "u1"})})
    assert await overview.ainvoke({"uri": "viking://user/u/memories/x.md", "runtime": runtime({"user_id": "u1"})})
