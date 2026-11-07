import json
from typing import Annotated

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId

from agentkit.tools.builtin.search_tools import search_tools
from agentkit.tools.search.service import (
    ToolSearchHit,
    ToolSearchOutcome,
    reset_tool_search_runtime,
    set_tool_search_runtime,
)


def _entry():
    from agentkit.contextforge.catalog import ToolCatalogEntry

    return ToolCatalogEntry(
        name="github_search",
        display_name="GitHub",
        description="Search GitHub code",
        tags=("github",),
        parameters=("query",),
        server="contextforge",
        content="github_search\nGitHub\nSearch GitHub code",
        content_hash="h",
        updated_at=1,
    )


class FakeService:
    def __init__(self) -> None:
        self.last_user_id = None

    async def search(self, *, task_query, domain_keywords=(), user_id=None):
        self.last_user_id = user_id
        return ToolSearchOutcome(
            hits=(ToolSearchHit(entry=_entry(), score=0.9),),
            channels=("embedding", "keyword"),
            candidates=1,
            reranked=True,
            index_kind="milvus",
        )


@pytest.mark.asyncio
async def test_search_tools_returns_metadata_and_promotes() -> None:
    set_tool_search_runtime(FakeService())
    try:
        result = await search_tools.coroutine(
            task_query="github code",
            domain_keywords=[],
            tool_call_id="call-1",
        )
    finally:
        reset_tool_search_runtime()
    assert hasattr(result, "update")
    assert result.update["promoted"] == ["github_search"]
    message = result.update["messages"][0]
    assert isinstance(message, ToolMessage)
    payload = json.loads(message.content)
    assert payload["tools"][0]["tool_name"] == "github_search"
    assert "input_schema" not in payload["tools"][0]


@pytest.mark.asyncio
async def test_search_tools_without_runtime_returns_unavailable() -> None:
    reset_tool_search_runtime()
    result = await search_tools.coroutine(
        task_query="github",
        domain_keywords=[],
        tool_call_id="call-1",
    )
    payload = json.loads(result.update["messages"][0].content)
    assert payload["tools"] == []
    assert payload["retrieval"]["warnings"] == ["catalog_unavailable"]

@pytest.mark.asyncio
async def test_search_tools_passes_the_runtime_user_to_the_catalog() -> None:
    service = FakeService()
    set_tool_search_runtime(service)
    try:
        await search_tools.coroutine(
            task_query="github code",
            domain_keywords=[],
            runtime=type("Runtime", (), {"context": {"user_id": "u1"}})(),
            tool_call_id="call-1",
        )
    finally:
        reset_tool_search_runtime()
    assert service.last_user_id == "u1"
