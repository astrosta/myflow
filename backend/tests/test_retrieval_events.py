"""Task 2: the retrieval event catalog entries and the payload normalizer.

The normalizer is the only place tool-result JSON becomes a stable evaluation
payload, so it must be tolerant of everything a tool can actually return:
plain text, content blocks, malformed JSON, error payloads, unknown tools.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentkit.runtime.events.catalog import (
    RETRIEVAL_KB_EVENT,
    RETRIEVAL_SKILL_EVENT,
    RETRIEVAL_TOOL_EVENT,
    RUN_EVENT_CATEGORIES,
)
from agentkit.runtime.events.retrieval import RetrievalEvent, retrieval_event_for_tool


def _tool_search_payload() -> dict[str, Any]:
    return {
        "tools": [
            {
                "tool_id": "tool-1",
                "tool_name": "alpha",
                "server_name": "srv",
                "call_name": "mcp__srv__alpha",
                "description": "does alpha",
                "tags": ["alpha"],
                "parameters": ["task"],
                "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
                "schema_truncated": False,
                "score": 0.8125,
            },
            {
                "tool_id": "tool-2",
                "tool_name": "beta",
                "server_name": "srv",
                "call_name": "mcp__srv__beta",
                "description": "does beta",
                "tags": [],
                "parameters": [],
                "input_schema": {},
                "schema_truncated": False,
                "score": 0.25,
            },
        ],
        "retrieval": {
            "index": "milvus",
            "channels": ["keyword", "embedding"],
            "candidates": 11,
            "reranked": True,
            "warnings": ["embedding_degraded"],
        },
        "message": "Call tool_execute(tool_name, arguments) to run one of these tools.",
    }


def _search_skills_payload() -> dict[str, Any]:
    return {
        "skills": [
            {
                "skill_id": "skill-1",
                "name": "pdf-report",
                "description": "Render a PDF report from a spec.",
                "score": 0.75,
            },
            {"skill_id": "skill-2", "name": "xlsx-export", "description": "", "score": 0.5},
        ],
        "retrieval": {
            "channels": ["keyword"],
            "candidates": 4,
            "reranked": False,
            "warnings": [],
        },
        "message": "只返回元数据；需要正文时请调用 load_skill(name, id)。",
    }


def _knowledge_search_payload() -> dict[str, Any]:
    return {
        "query": "升级到 3.2 需要做哪些准备",
        "keyword_query": "3.2 升级 回滚",
        "retrieval_mode": "hybrid",
        "reranked": True,
        "knowledge_base_type": None,
        "sources": [
            {
                "citation_id": "k3f9a2c1",
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "knowledge_base_id": "kb-1",
                "knowledge_base_name": "升级手册",
                "knowledge_base_type": "technical",
                "filename": "upgrade.md",
                "section": "准备",
                "page": 3,
                "chunk_index": 0,
                "quote": "先备份数据库。",
            }
        ],
        "results": [
            {
                "chunk_id": "chunk-1",
                "citation_id": "k3f9a2c1",
                "document_id": "doc-1",
                "filename": "upgrade.md",
                "section": "准备",
                "page": 3,
                "chunk_index": 0,
                "parent_id": None,
                "score": 0.9,
                "rrf_score": 0.03,
                "rerank_score": 0.44,
                "knowledge_base_id": "kb-1",
                "knowledge_base_name": "升级手册",
                "knowledge_base_type": "technical",
                "content": "先备份数据库，再停止写入。",
            }
        ],
        "truncated": False,
    }


def test_tool_search_payload_normalizes_to_the_tool_event() -> None:
    event = retrieval_event_for_tool(
        tool_name="tool_search", content=json.dumps(_tool_search_payload(), ensure_ascii=False)
    )
    assert isinstance(event, RetrievalEvent)
    assert event.event_type == RETRIEVAL_TOOL_EVENT.event_type == "retrieval.tool"
    assert event.content["tools"] == [
        {"tool_id": "tool-1", "call_name": "mcp__srv__alpha", "score": 0.8125},
        {"tool_id": "tool-2", "call_name": "mcp__srv__beta", "score": 0.25},
    ]
    assert "input_schema" not in json.dumps(event.content)
    assert event.content["channels"] == ["keyword", "embedding"]
    assert event.content["warnings"] == ["embedding_degraded"]
    assert event.content["truncated"] is False


def test_search_skills_payload_normalizes_to_the_skill_event() -> None:
    event = retrieval_event_for_tool(
        tool_name="search_skills", content=json.dumps(_search_skills_payload(), ensure_ascii=False)
    )
    assert isinstance(event, RetrievalEvent)
    assert event.event_type == RETRIEVAL_SKILL_EVENT.event_type == "retrieval.skill"
    assert event.content["skills"] == [
        {"skill_id": "skill-1", "name": "pdf-report", "score": 0.75},
        {"skill_id": "skill-2", "name": "xlsx-export", "score": 0.5},
    ]
    assert event.content["truncated"] is False


def test_knowledge_search_payload_normalizes_to_the_kb_event() -> None:
    event = retrieval_event_for_tool(
        tool_name="knowledge_search",
        content=json.dumps(_knowledge_search_payload(), ensure_ascii=False),
    )
    assert isinstance(event, RetrievalEvent)
    assert event.event_type == RETRIEVAL_KB_EVENT.event_type == "retrieval.kb"
    assert event.content["query"] == "升级到 3.2 需要做哪些准备"
    assert event.content["keyword_query"] == "3.2 升级 回滚"
    assert event.content["retrieval_mode"] == "hybrid"
    assert event.content["reranked"] is True
    assert event.content["truncated"] is False
    assert event.content["items"] == [
        {
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "citation_id": "k3f9a2c1",
            "filename": "upgrade.md",
            "section": "准备",
            "page": 3,
            "score": 0.9,
            "rerank_score": 0.44,
            "content": "先备份数据库，再停止写入。",
        }
    ]


def test_truncated_knowledge_search_result_keeps_the_flag() -> None:
    payload = _knowledge_search_payload()
    payload["truncated"] = True
    event = retrieval_event_for_tool(
        tool_name="knowledge_search", content=json.dumps(payload, ensure_ascii=False)
    )
    assert event is not None
    assert event.content["truncated"] is True


def test_truncated_skill_retrieval_keeps_the_flag() -> None:
    payload = _search_skills_payload()
    payload["retrieval"]["truncated"] = True
    event = retrieval_event_for_tool(
        tool_name="search_skills", content=json.dumps(payload, ensure_ascii=False)
    )
    assert event is not None
    assert event.content["truncated"] is True


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        "not json at all",
        '{"tools": [',
        None,
        object(),
        b'{"tools": []}',
        json.dumps([1, 2, 3]),
        json.dumps("tools"),
    ],
)
@pytest.mark.parametrize("tool_name", ["tool_search", "search_skills", "knowledge_search"])
def test_malformed_or_unusable_content_returns_none(tool_name: str, content: Any) -> None:
    assert retrieval_event_for_tool(tool_name=tool_name, content=content) is None


@pytest.mark.parametrize(
    "tool_name", ["tool_execute", "load_skill", "task", "", "Tool_Search", "tool_search "]
)
def test_unknown_tool_names_return_none(tool_name: str) -> None:
    assert (
        retrieval_event_for_tool(
            tool_name=tool_name, content=json.dumps(_tool_search_payload(), ensure_ascii=False)
        )
        is None
    )


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [
        ("tool_search", {"message": "No matching tool."}),
        ("tool_search", {"tools": "nope"}),
        ("search_skills", {"skills": {}}),
        ("search_skills", {"message": "技能检索暂不可用。"}),
        ("knowledge_search", {"error": {"code": "knowledge_disabled", "message": "x"}}),
        ("knowledge_search", {"query": "q", "results": "nope"}),
    ],
)
def test_payloads_without_the_expected_container_return_none(
    tool_name: str, payload: dict[str, Any]
) -> None:
    assert (
        retrieval_event_for_tool(tool_name=tool_name, content=json.dumps(payload, ensure_ascii=False))
        is None
    )


def test_content_blocks_are_read_like_message_text() -> None:
    text = json.dumps(_knowledge_search_payload(), ensure_ascii=False)
    event = retrieval_event_for_tool(
        tool_name="knowledge_search",
        content=[{"type": "text", "text": text}],
    )
    assert event is not None
    assert [item["chunk_id"] for item in event.content["items"]] == ["chunk-1"]


def test_an_empty_result_set_is_still_an_event() -> None:
    event = retrieval_event_for_tool(
        tool_name="tool_search",
        content=json.dumps(
            {
                "tools": [],
                "retrieval": {
                    "index": "milvus",
                    "channels": [],
                    "candidates": 0,
                    "reranked": False,
                    "warnings": [],
                },
                "message": "No matching tool.",
            }
        ),
    )
    assert event is not None
    assert event.content["tools"] == []


def test_invalid_entries_are_dropped_and_never_raise() -> None:
    event = retrieval_event_for_tool(
        tool_name="tool_search",
        content=json.dumps(
            {
                "tools": [
                    None,
                    5,
                    "mcp__srv__alpha",
                    {"call_name": ""},
                    {"call_name": None},
                    {"tool_id": "tool-9", "call_name": "mcp__srv__gamma", "score": "high"},
                ]
            }
        ),
    )
    assert event is not None
    assert event.content["tools"] == [
        {"tool_id": "tool-9", "call_name": "mcp__srv__gamma", "score": 0.0}
    ]

    skills = retrieval_event_for_tool(
        tool_name="search_skills",
        content=json.dumps(
            {"skills": [None, {"name": "no-id"}, {"skill_id": "s-1", "name": "ok"}]}
        ),
    )
    assert skills is not None
    assert skills.content["skills"] == [{"skill_id": "s-1", "name": "ok", "score": 0.0}]

    kb = retrieval_event_for_tool(
        tool_name="knowledge_search",
        content=json.dumps(
            {
                "query": "q",
                "results": [
                    "nope",
                    {"chunk_id": "chunk-1"},
                    {"document_id": "doc-2", "chunk_id": "chunk-2", "content": "text"},
                ],
                "truncated": False,
            }
        ),
    )
    assert kb is not None
    assert [item["chunk_id"] for item in kb.content["items"]] == ["chunk-2"]


def test_normalized_content_is_json_safe() -> None:
    for tool_name, payload in (
        ("tool_search", _tool_search_payload()),
        ("search_skills", _search_skills_payload()),
        ("knowledge_search", _knowledge_search_payload()),
    ):
        event = retrieval_event_for_tool(
            tool_name=tool_name, content=json.dumps(payload, ensure_ascii=False)
        )
        assert event is not None
        assert json.loads(json.dumps(event.content, ensure_ascii=False, allow_nan=False)) == (
            event.content
        )


def test_retrieval_definitions_live_in_the_catalog() -> None:
    assert "retrieval" in RUN_EVENT_CATEGORIES
    assert {RETRIEVAL_TOOL_EVENT.category, RETRIEVAL_SKILL_EVENT.category} == {"retrieval"}
    assert RETRIEVAL_KB_EVENT.category == "retrieval"
