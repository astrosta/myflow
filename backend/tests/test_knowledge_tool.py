"""knowledge_search tool: user identity from runtime and type routing."""

from __future__ import annotations

import asyncio
import json

from agentkit.knowledge.models import SearchHit, SearchOutcome
from agentkit.knowledge.tool import make_knowledge_search_tool
from langchain.tools import ToolRuntime


class FakeService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return SearchOutcome(
            query=kwargs["query"],
            hits=[
                SearchHit(
                    chunk_id="d1:0",
                    document_id="d1",
                    filename="manual.md",
                    section="安装",
                    content="安装步骤",
                    score=0.9,
                    knowledge_base_id="b1",
                    knowledge_base_name="产品文档",
                    knowledge_base_type="technical",
                    page=2,
                )
            ],
            truncated=False,
            keyword_query=kwargs.get("keyword_query") or kwargs["query"],
            retrieval_mode="hybrid",
            reranked=True,
        )


def _runtime(context: dict) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id=None,
        store=None,
    )


def test_tool_passes_runtime_user_and_type_to_service() -> None:
    service = FakeService()
    tool = make_knowledge_search_tool(service)

    async def call():
        return await tool.ainvoke(
            {
                "query": "怎么安装",
                "keyword_query": "E1024 安装",
                "knowledge_base_type": "technical",
                "knowledge_base_name": "产品文档",
                "top_k": 3,
                "runtime": _runtime({"user_id": "u1"}),
            }
        )

    payload = json.loads(asyncio.run(call()))
    assert payload["results"][0]["knowledge_base_type"] == "technical"
    assert payload["results"][0]["page"] == 2
    assert payload["truncated"] is False
    assert service.calls[0]["user_id"] == "u1"
    assert service.calls[0]["knowledge_base_type"] == "technical"
    assert service.calls[0]["top_k"] == 3
    assert service.calls[0]["keyword_query"] == "E1024 安装"
    assert payload["keyword_query"] == "E1024 安装"
    assert payload["retrieval_mode"] == "hybrid"
    assert payload["reranked"] is True


def test_tool_without_user_returns_structured_error() -> None:
    tool = make_knowledge_search_tool(FakeService())

    async def call():
        return await tool.ainvoke({"query": "hi", "runtime": _runtime({})})

    payload = json.loads(asyncio.run(call()))
    assert payload["error"]["code"] == "knowledge_disabled"


def test_tool_schema_names_type_and_usage_docstring() -> None:
    tool = make_knowledge_search_tool(FakeService())
    assert "知识库" in (tool.description or "")
    assert "technical" in (tool.description or "")
    assert "faq" in (tool.description or "")
    assert "knowledge_base_type" in tool.args_schema.model_fields
    assert "keyword_query" in tool.args_schema.model_fields
    assert "keyword_query" in (tool.description or "")
