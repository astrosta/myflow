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
    """The real `_metadata_payload` shape: no score, message is a list."""

    return {
        "skills": [
            {
                "skill_id": "skill-1",
                "name": "pdf-report",
                "description": "Render a PDF report from a spec.",
            },
            {"skill_id": "skill-2", "name": "xlsx-export", "description": ""},
        ],
        "retrieval": {
            "channels": ["keyword", "embedding"],
            "candidates": 4,
            "reranked": False,
            "warnings": [],
        },
        "message": ["只返回元数据；需要正文时请调用 load_skill(name, id)。"],
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


def _normalize(tool_name: str, payload: Any) -> RetrievalEvent | None:
    return retrieval_event_for_tool(
        tool_name=tool_name, content=json.dumps(payload, ensure_ascii=False)
    )


def _tool_search_reply(*, index: str, warnings: list[str], message: str) -> dict[str, Any]:
    """The exact envelope `tool_search._reply` serializes for an empty answer."""

    return {
        "tools": [],
        "retrieval": {
            "index": index,
            "channels": [],
            "candidates": 0,
            "reranked": False,
            "warnings": warnings,
        },
        "message": message,
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


def test_real_tool_search_unavailable_payload_returns_none() -> None:
    payload = _tool_search_reply(
        index="unavailable",
        warnings=["catalog_unavailable"],
        message=(
            "Tool retrieval is unavailable in this deployment. Do not retry; use the "
            "tools you already have - including any tool_search cards retrieved earlier "
            "in this run, which tool_execute still accepts - or tell the user."
        ),
    )
    assert _normalize("tool_search", payload) is None


def test_real_tool_search_blank_query_payload_returns_none() -> None:
    payload = _tool_search_reply(
        index="unavailable",
        warnings=[],
        message=(
            "task_query must not be empty; rewrite it as one self-contained task "
            "description and search again."
        ),
    )
    assert _normalize("tool_search", payload) is None


def test_tool_search_catalog_unavailable_warning_returns_none() -> None:
    payload = _tool_search_reply(
        index="milvus",
        warnings=["catalog_unavailable"],
        message="No matching tool.",
    )
    assert _normalize("tool_search", payload) is None


@pytest.mark.parametrize("retrieval", [None, [], "milvus", 0, False])
def test_tool_search_without_a_retrieval_marker_returns_none(retrieval: Any) -> None:
    payload = {"tools": [], "retrieval": retrieval, "message": "No matching tool."}
    assert _normalize("tool_search", payload) is None


def test_search_skills_payload_normalizes_to_the_skill_event() -> None:
    event = retrieval_event_for_tool(
        tool_name="search_skills", content=json.dumps(_search_skills_payload(), ensure_ascii=False)
    )
    assert isinstance(event, RetrievalEvent)
    assert event.event_type == RETRIEVAL_SKILL_EVENT.event_type == "retrieval.skill"
    # The producer emits no score; Task 8 ranks by list order.
    assert event.content["skills"] == [
        {"skill_id": "skill-1", "name": "pdf-report"},
        {"skill_id": "skill-2", "name": "xlsx-export"},
    ]
    assert all("score" not in skill for skill in event.content["skills"])
    assert event.content["channels"] == ["keyword", "embedding"]
    assert event.content["truncated"] is False


@pytest.mark.parametrize(
    "payload",
    [
        # config.skills.search.enabled is false
        {"skills": [], "message": ["技能检索在本部署已关闭。"]},
        # task_query is empty after strip()
        {"skills": [], "message": ["task_query 不能为空：请重写成一句自包含的任务描述。"]},
        # get_skill_runtime() is None, or SkillSearchUnavailable was raised
        {
            "skills": [],
            "message": [
                "技能检索暂不可用（技能目录或 Milvus 检索索引未就绪）。",
                "不要用本工具重复尝试相似的说法。",
            ],
        },
    ],
    ids=["disabled", "blank-query", "unavailable"],
)
def test_real_search_skills_failure_payloads_return_none(payload: dict[str, Any]) -> None:
    assert _normalize("search_skills", payload) is None


@pytest.mark.parametrize(
    "warnings",
    [
        ["retrieval_unavailable"],  # SkillSearchService has no projection
        ["vector_unavailable"],  # Milvus search failed; no channel answered
        ["keyword_unavailable"],
    ],
)
def test_real_search_skills_service_failures_return_none(
    warnings: list[str],
) -> None:
    """The service reports an empty hit list with no channels when retrieval failed."""

    payload = {
        "skills": [],
        "retrieval": {
            "channels": [],
            "candidates": 0,
            "reranked": False,
            "warnings": warnings,
        },
        "message": [
            "没有检索到匹配的技能（当前部署可能一个技能都没启用，或技能树为空）。",
            "不要用本工具重复尝试相似的说法；需要正文时让 lead 用 load_skill(name, id)。",
        ],
    }
    assert _normalize("search_skills", payload) is None


def test_real_search_skills_empty_answer_is_an_event() -> None:
    payload = {
        "skills": [],
        "retrieval": {
            "channels": ["keyword", "embedding"],
            "candidates": 0,
            "reranked": False,
            "warnings": [],
        },
        "message": [
            "没有检索到匹配的技能（当前部署可能一个技能都没启用，或技能树为空）。",
            "不要用本工具重复尝试相似的说法；需要正文时让 lead 用 load_skill(name, id)。",
        ],
    }
    event = _normalize("search_skills", payload)
    assert event is not None
    assert event.content["skills"] == []
    assert event.content["channels"] == ["keyword", "embedding"]


@pytest.mark.parametrize("retrieval", [None, [], "metadata", 0, False])
def test_search_skills_without_a_retrieval_marker_returns_none(retrieval: Any) -> None:
    payload = {
        "skills": [{"skill_id": "skill-1", "name": "pdf-report", "description": "x"}],
        "retrieval": retrieval,
        "message": ["只返回元数据；需要正文时请调用 load_skill(name, id)。"],
    }
    assert _normalize("search_skills", payload) is None


def test_degraded_but_real_search_skills_result_is_still_an_event() -> None:
    """One recall channel answering is a real search, warning or not."""

    payload = _search_skills_payload()
    payload["retrieval"]["channels"] = ["keyword"]
    payload["retrieval"]["warnings"] = ["vector_unavailable"]
    event = _normalize("search_skills", payload)
    assert event is not None
    assert event.content["channels"] == ["keyword"]
    assert event.content["warnings"] == ["vector_unavailable"]
    assert [skill["skill_id"] for skill in event.content["skills"]] == [
        "skill-1",
        "skill-2",
    ]


def test_skill_score_is_kept_when_present_and_never_defaulted() -> None:
    payload = _search_skills_payload()
    payload["skills"][0]["score"] = 0.42
    event = _normalize("search_skills", payload)
    assert event is not None
    assert event.content["skills"][0]["score"] == 0.42
    assert "score" not in event.content["skills"][1]


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


def test_kb_payload_without_query_returns_none() -> None:
    payload = _knowledge_search_payload()
    payload.pop("query")
    assert _normalize("knowledge_search", payload) is None


def test_kb_payload_with_blank_query_returns_none() -> None:
    payload = _knowledge_search_payload()
    payload["query"] = "   "
    assert _normalize("knowledge_search", payload) is None


@pytest.mark.parametrize("content", ["", "   ", None], ids=["empty", "blank", "missing"])
def test_kb_item_without_usable_content_is_dropped(content: Any) -> None:
    payload = _knowledge_search_payload()
    second = dict(payload["results"][0])
    second["chunk_id"] = "chunk-2"
    if content is None:
        second.pop("content")
    else:
        second["content"] = content
    payload["results"] = [payload["results"][0], second]
    event = _normalize("knowledge_search", payload)
    assert event is not None
    assert [item["chunk_id"] for item in event.content["items"]] == ["chunk-1"]


def test_kb_results_without_any_usable_row_are_not_an_empty_success() -> None:
    payload = _knowledge_search_payload()
    payload["results"] = [
        {
            "chunk_id": "chunk-1",
            "citation_id": "k3f9a2c1",
            "document_id": "doc-1",
            "filename": "upgrade.md",
            "section": "准备",
            "page": 3,
            "score": 0.9,
            "rerank_score": 0.44,
        }
    ]
    assert _normalize("knowledge_search", payload) is None


def test_real_kb_empty_answer_is_an_event() -> None:
    payload = {
        "query": "升级到 3.2 需要做哪些准备",
        "keyword_query": "3.2 升级 回滚",
        "retrieval_mode": "hybrid",
        "reranked": True,
        "knowledge_base_type": None,
        "sources": [],
        "results": [],
        "truncated": False,
        "message": "用户的知识库中没有找到相关内容。",
    }
    event = _normalize("knowledge_search", payload)
    assert event is not None
    assert event.content["query"] == "升级到 3.2 需要做哪些准备"
    assert event.content["items"] == []
    assert event.content["truncated"] is False


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
                ],
                "retrieval": {
                    "index": "milvus",
                    "channels": [],
                    "candidates": 0,
                    "reranked": False,
                    "warnings": [],
                },
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
            {
                "skills": [None, {"name": "no-id"}, {"skill_id": "s-1", "name": "ok"}],
                "retrieval": {
                    "channels": ["keyword"],
                    "candidates": 1,
                    "reranked": False,
                    "warnings": [],
                },
            }
        ),
    )
    assert skills is not None
    assert skills.content["skills"] == [{"skill_id": "s-1", "name": "ok"}]

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


def test_unavailable_markers_match_the_tool_search_producer() -> None:
    from agentkit.runtime.events.retrieval import (
        CATALOG_UNAVAILABLE_WARNING,
        UNAVAILABLE_RETRIEVAL_INDEX,
    )
    from agentkit.tools.search.service import CATALOG_UNAVAILABLE, UNAVAILABLE_INDEX_KIND

    assert UNAVAILABLE_RETRIEVAL_INDEX == UNAVAILABLE_INDEX_KIND
    assert CATALOG_UNAVAILABLE_WARNING == CATALOG_UNAVAILABLE
