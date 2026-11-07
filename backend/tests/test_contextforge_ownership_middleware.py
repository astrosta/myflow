from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from agentkit.agents.middlewares.contextforge_ownership_middleware import (
    ContextForgeOwnershipMiddleware,
)
from agentkit.contextforge.cache import invalidate_contextforge_cache, set_contextforge_snapshot
from agentkit.contextforge.catalog import ToolCatalogEntry
from agentkit.tools.contextforge_metadata import tag_contextforge_tool


def _tool() -> StructuredTool:
    def _run(query: str = "") -> str:
        return query

    return tag_contextforge_tool(
        StructuredTool.from_function(_run, name="mine", description="mine"),
        original_name="mine",
        owner="u1",
    )


def _entry() -> ToolCatalogEntry:
    return ToolCatalogEntry(
        name="mine",
        display_name="mine",
        description="mine",
        tags=(),
        parameters=("query",),
        server="contextforge",
        content="mine",
        content_hash="h",
        updated_at=1,
        owner="u1",
    )


def _request(user_id: str):
    return SimpleNamespace(
        tool=_tool(),
        tool_call={"id": "call-1", "name": "mine", "args": {}},
        runtime=SimpleNamespace(context={"user_id": user_id}),
    )


@pytest.mark.asyncio
async def test_own_tool_passes_the_guard() -> None:
    invalidate_contextforge_cache("guard")
    set_contextforge_snapshot(tools=(_tool(),), entries=(_entry(),))
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = await ContextForgeOwnershipMiddleware().awrap_tool_call(
        _request("u1"), handler
    )
    invalidate_contextforge_cache("guard")
    assert isinstance(result, ToolMessage)
    assert result.content == "ok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_another_users_tool_is_denied_without_calling() -> None:
    invalidate_contextforge_cache("guard")
    set_contextforge_snapshot(tools=(_tool(),), entries=(_entry(),))
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = await ContextForgeOwnershipMiddleware().awrap_tool_call(
        _request("u2"), handler
    )
    invalidate_contextforge_cache("guard")
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert calls["n"] == 0
