"""Model-facing read-only memory tools for the OpenViking adapter."""

from __future__ import annotations

import json

import pytest
from langchain.tools import ToolRuntime
from pydantic import ValidationError

from agentkit.agents.memory.openviking.contracts import MemoryHit, TurnDecision
from agentkit.agents.memory.openviking.tools import (
    make_memory_overview_tool,
    make_memory_read_tool,
    make_memory_remember_tool,
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


def test_memory_tools_document_their_arguments() -> None:
    search = make_memory_search_tool(FakeFacade())
    schema = search.args_schema.model_json_schema()
    properties = schema["properties"]
    assert "要回忆" in properties["query"]["description"]
    assert "experiences" in properties["memory_types"]["description"]
    assert "ISO-8601" in properties["since"]["description"]
    assert "画像由系统单独注入" not in (search.description or "")

    read = make_memory_read_tool(FakeFacade())
    read_schema = read.args_schema.model_json_schema()
    assert "uri" in read_schema["properties"]
    assert read_schema["properties"]["uri"]["description"]

    overview = make_memory_overview_tool(FakeFacade())
    assert overview.args_schema.model_json_schema()["properties"]["uri"]["description"]


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
async def test_search_tool_rejects_invalid_type_and_mixed_time_filter() -> None:
    facade = FakeFacade()
    tool = make_memory_search_tool(facade)

    for invalid_type in ("profile", "identity", "semantic", "skills"):
        with pytest.raises(ValidationError, match="memory_types"):
            await tool.ainvoke(
                {
                    "query": "style",
                    "memory_types": [invalid_type],
                    "runtime": runtime({"user_id": "u1"}),
                }
            )

    mixed = json.loads(
        await tool.ainvoke(
            {
                "query": "style",
                "memory_types": ["preferences", "events"],
                "since": "2026-01-01",
                "runtime": runtime({"user_id": "u1"}),
            }
        )
    )
    assert mixed["error"] == "time_filter_requires_events"


@pytest.mark.asyncio
async def test_search_tool_accepts_tools_type() -> None:
    facade = FakeFacade()
    tool = make_memory_search_tool(facade)

    await tool.ainvoke(
        {
            "query": "usage",
            "memory_types": ["tools"],
            "runtime": runtime({"user_id": "u1"}),
        }
    )

    assert [call[3] for call in facade.calls] == [["tools"]]


@pytest.mark.asyncio
async def test_explicit_remember_tool_marks_the_current_turn_decision() -> None:
    tool = make_memory_remember_tool()
    context = {
        "user_id": "u1",
        "capture_decision": TurnDecision("capture", reason="default_capture"),
    }

    result = json.loads(
        await tool.ainvoke(
            {
                "text": "我喜欢吃苹果",
                "runtime": runtime(context),
            }
        )
    )

    assert result == {"ok": True, "status": "queued", "signal": "EXPLICIT_REMEMBER"}
    assert context["capture_decision"].signals == ("EXPLICIT_REMEMBER",)


@pytest.mark.asyncio
async def test_explicit_remember_tool_does_not_duplicate_the_signal() -> None:
    tool = make_memory_remember_tool()
    context = {
        "user_id": "u1",
        "capture_decision": TurnDecision("capture", signals=("EXPLICIT_REMEMBER",)),
    }

    await tool.ainvoke(
        {"text": "我喜欢吃苹果", "runtime": runtime(context)}
    )

    assert context["capture_decision"].signals == ("EXPLICIT_REMEMBER",)


@pytest.mark.asyncio
async def test_explicit_remember_tool_still_marks_a_missing_decision() -> None:
    tool = make_memory_remember_tool()
    context = {"user_id": "u1"}

    result = json.loads(
        await tool.ainvoke(
            {"text": "我喜欢吃苹果", "runtime": runtime(context)}
        )
    )

    assert result["status"] == "queued"
    assert context["memory_explicit_remember"] is True


@pytest.mark.asyncio
async def test_read_and_overview_tools_require_user() -> None:
    facade = FakeFacade()
    read = make_memory_read_tool(facade)
    overview = make_memory_overview_tool(facade)

    missing = json.loads(await read.ainvoke({"uri": "viking://user/u/memories/x.md", "runtime": runtime({})}))
    assert "error" in missing
    assert await read.ainvoke({"uri": "viking://user/u/memories/x.md", "runtime": runtime({"user_id": "u1"})})
    assert await overview.ainvoke({"uri": "viking://user/u/memories/x.md", "runtime": runtime({"user_id": "u1"})})
