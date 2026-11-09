"""Subagent citation hand-off: only cited, retrieved sources reach the lead."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.knowledge.citations import citations_for_hits
from agentkit.knowledge.models import SearchHit
from agentkit.subagents.executor import extract_citations


def _citation(chunk_id: str) -> dict:
    document_id = chunk_id.split(":")[0]
    return citations_for_hits([
        SearchHit(
            chunk_id=chunk_id,
            document_id=document_id,
            filename=document_id + ".md",
            section="安装",
            content="安装步骤",
            score=0.9,
            knowledge_base_id="b1",
            knowledge_base_name="产品文档",
            knowledge_base_type="technical",
        )
    ])[0]


def test_extract_citations_resolves_markers_against_knowledge_search_results() -> None:
    first, second = _citation("d1:0"), _citation("d2:0")
    state = {
        "messages": [
            HumanMessage(content="问题"),
            ToolMessage(
                content=json.dumps({"sources": [first, second]}, ensure_ascii=False),
                tool_call_id="call-1",
                name="knowledge_search",
            ),
            AIMessage(content=f"结论 [{first['citation_id']}]，伪造 [kdeadbeef]。"),
        ]
    }

    assert extract_citations(state, f"结论 [{first['citation_id']}]，伪造 [kdeadbeef]。") == [first]


def test_extract_citations_returns_nothing_when_the_answer_has_no_markers() -> None:
    first = _citation("d1:0")
    state = {
        "messages": [
            ToolMessage(
                content=json.dumps({"sources": [first]}, ensure_ascii=False),
                tool_call_id="call-1",
                name="knowledge_search",
            ),
            AIMessage(content="没有标记的答案"),
        ]
    }

    assert extract_citations(state, "没有标记的答案") == []


def test_extract_citations_ignores_malformed_tool_results() -> None:
    state = {
        "messages": [
            ToolMessage(
                content='{"sources": [', tool_call_id="call-1", name="knowledge_search"
            ),
            AIMessage(content="答案 [kdeadbeef]"),
        ]
    }

    assert extract_citations(state, "答案 [kdeadbeef]") == []
