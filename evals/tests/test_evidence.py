"""Run-evidence normalization tests.

The fixtures mirror the persisted run-event ledger shapes verified at b251d0b.
No Gateway and no agentkit import: this module is a pure parser.
"""

from __future__ import annotations

from typing import Any

from myflow_evals.evidence import RunEvidence, collect_evidence

RUN_ID = "run-1"
OTHER_RUN_ID = "run-2"

CITATION = {
    "citation_id": "kabc12345",
    "chunk_id": "doc-1:0",
    "document_id": "doc-1",
    "knowledge_base_id": "kb-1",
    "filename": "policy.md",
    "section": "refunds",
    "page": 2,
    "chunk_index": 0,
    "quote": "Refunds take five business days.",
}

CHUNK_ONE = {
    "document_id": "doc-1",
    "chunk_id": "doc-1:0",
    "citation_id": "kabc12345",
    "filename": "policy.md",
    "section": "refunds",
    "page": 2,
    "score": 0.91,
    "rerank_score": 0.88,
    "content": "Refunds take five business days.",
}

CHUNK_TWO = {
    "document_id": "doc-2",
    "chunk_id": "doc-2:0",
    "citation_id": "kdef67890",
    "filename": "sla.md",
    "section": "support",
    "page": 1,
    "score": 0.72,
    "rerank_score": None,
    "content": "Support answers within one business day.",
}


def _event(
    event_type: str,
    content: Any,
    *,
    category: str = "message",
    metadata: dict[str, Any] | None = None,
    seq: int = 1,
    run_id: str = RUN_ID,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "run_id": run_id,
        "event_type": event_type,
        "category": category,
        "content": content,
        "metadata": metadata or {},
        "created_at": None,
    }


def _ai(
    *,
    content: Any = "",
    tool_calls: list[dict[str, Any]] | None = None,
    citations: list[Any] | None = None,
    message_id: str = "ai-1",
) -> dict[str, Any]:
    additional_kwargs: dict[str, Any] = {}
    if citations is not None:
        additional_kwargs["knowledge_citations"] = citations
    return {
        "type": "ai",
        "content": content,
        "tool_calls": list(tool_calls or []),
        "additional_kwargs": additional_kwargs,
        "response_metadata": {},
        "id": message_id,
    }


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _tool(
    *, content: Any, call_id: str, name: str = "tool", status: str = "success"
) -> dict[str, Any]:
    return {
        "type": "tool",
        "content": content,
        "tool_call_id": call_id,
        "name": name,
        "status": status,
    }


PARENT_CALLS = [
    _call("tool_search", {"query": "weather lookup"}, "call-search"),
    _call(
        "tool_execute",
        {"tool_name": "mcp__weather__forecast", "arguments": {"city": "Paris"}},
        "call-weather",
    ),
    _call(
        "task",
        {"subagent_type": "knowledge-researcher", "description": "find the refund policy"},
        "call-task-kb",
    ),
    _call(
        "task",
        {"subagent_type": "skill-finder", "description": "find a private skill"},
        "call-task-skill",
    ),
    _call("load_skill", {"name": "private-skill", "id": "skill-1"}, "call-load-skill"),
    _call("knowledge_search", {"query": "refund policy"}, "call-kb"),
]
PARENT_AI = _ai(tool_calls=PARENT_CALLS, message_id="ai-parent")
FINAL_AI = _ai(
    content="Refunds take five business days [kabc12345].",
    citations=[CITATION, {"citation_id": "kmissing", "document_id": "doc-9"}],
    message_id="ai-final",
)

FULL_EVENTS = [
    _event("llm.ai.response", PARENT_AI, seq=1),
    _event(
        "llm.tool.result",
        _tool(content='{"tools": []}', call_id="call-search", name="tool_search"),
        seq=2,
    ),
    _event(
        "llm.tool.result",
        _tool(content='{"temperature_c": 21}', call_id="call-weather", name="tool_execute"),
        seq=3,
    ),
    _event(
        "llm.tool.result",
        _tool(content="manifest", call_id="call-task-kb", name="task"),
        seq=4,
    ),
    _event(
        "llm.tool.result",
        _tool(content="manifest", call_id="call-task-skill", name="task"),
        seq=5,
    ),
    _event(
        "llm.tool.result",
        _tool(content="loaded", call_id="call-load-skill", name="load_skill"),
        seq=6,
    ),
    _event(
        "llm.tool.result",
        _tool(content='{"results": []}', call_id="call-kb", name="knowledge_search"),
        seq=7,
    ),
    _event(
        "subagent.start",
        {
            "description": "find the refund policy",
            "subagent_type": "knowledge-researcher",
            "model_name": "model-kb",
        },
        category="subagent",
        metadata={
            "task_id": "call-task-kb",
            "description": "find the refund policy",
            "subagent_type": "knowledge-researcher",
            "model_name": "model-kb",
        },
        seq=8,
    ),
    _event(
        "subagent.start",
        {
            "description": "find a private skill",
            "subagent_type": "skill-finder",
            "model_name": "model-skill",
        },
        category="subagent",
        metadata={
            "task_id": "call-task-skill",
            "description": "find a private skill",
            "subagent_type": "skill-finder",
            "model_name": "model-skill",
        },
        seq=9,
    ),
    _event(
        "retrieval.tool",
        {
            "tools": [
                {"tool_id": "tool-1", "call_name": "mcp__weather__forecast", "score": 0.93},
                {"tool_id": "tool-2", "call_name": "mcp__kb__search", "score": 0.61},
            ],
            "channels": ["vector"],
            "warnings": [],
            "truncated": False,
        },
        category="retrieval",
        seq=10,
    ),
    _event(
        "retrieval.skill",
        {
            "skills": [
                {"skill_id": "skill-1", "name": "private-skill", "score": 0.8},
                {"skill_id": "skill-2", "name": "other-skill"},
            ],
            "channels": ["keyword"],
            "warnings": [],
            "truncated": False,
        },
        category="retrieval",
        seq=11,
    ),
    _event(
        "retrieval.kb",
        {
            "query": "refund policy",
            "keyword_query": None,
            "retrieval_mode": "hybrid",
            "reranked": True,
            "items": [CHUNK_ONE, CHUNK_TWO],
            "truncated": False,
        },
        category="retrieval",
        seq=12,
    ),
    _event("llm.ai.response", FINAL_AI, seq=13),
]


def test_collect_evidence_normalizes_a_full_run_from_events() -> None:
    evidence = collect_evidence([], FULL_EVENTS, run_id=RUN_ID)

    assert isinstance(evidence, RunEvidence)
    assert evidence.answer == "Refunds take five business days [kabc12345]."
    assert [call["name"] for call in evidence.tool_calls] == [
        "tool_search",
        "tool_execute",
        "task",
        "task",
        "load_skill",
        "knowledge_search",
    ]
    assert evidence.tool_calls[1]["tool_call_id"] == "call-weather"
    assert evidence.tool_calls[1]["result"] == '{"temperature_c": 21}'

    assert len(evidence.business_tool_calls) == 1
    assert evidence.business_tool_calls[0] == {
        "tool_call_id": "call-weather",
        "name": "mcp__weather__forecast",
        "args": {"city": "Paris"},
        "result": '{"temperature_c": 21}',
    }

    assert [call["subagent_type"] for call in evidence.subagent_calls] == [
        "knowledge-researcher",
        "skill-finder",
    ]
    assert [call["started"] for call in evidence.subagent_calls] == [True, True]
    assert [call["model_name"] for call in evidence.subagent_calls] == [
        "model-kb",
        "model-skill",
    ]
    assert [call["description"] for call in evidence.subagent_calls] == [
        "find the refund policy",
        "find a private skill",
    ]

    assert evidence.loaded_skills == ["private-skill"]
    assert evidence.retrieved_tools == ["mcp__weather__forecast", "mcp__kb__search"]
    assert evidence.retrieved_skills == ["private-skill", "other-skill"]
    assert evidence.kb_called is True
    assert evidence.retrieved_chunks == [CHUNK_ONE, CHUNK_TWO]
    assert evidence.citations == [CITATION]
    assert evidence.trace_id is None


def test_collect_evidence_prefers_messages_for_the_answer_and_citations() -> None:
    checkpoint_final = _ai(
        content="Checkpoint answer [kabc12345].",
        citations=[CITATION],
        message_id="ai-checkpoint",
    )
    event_final = _ai(content="Event answer", message_id="ai-event")

    evidence = collect_evidence(
        [checkpoint_final],
        [_event("llm.ai.response", event_final)],
        run_id=RUN_ID,
    )

    assert evidence.answer == "Checkpoint answer [kabc12345]."
    assert evidence.citations == [CITATION]


def test_answer_falls_back_to_the_last_ai_response_event() -> None:
    events = [_event("llm.ai.response", _ai(content="Event answer", message_id="ai-event"))]

    assert collect_evidence([], events, run_id=RUN_ID).answer == "Event answer"


def test_answer_does_not_assume_the_last_message_is_ai() -> None:
    first = _ai(content="First answer", message_id="ai-1")
    tool = _tool(content="tool output", call_id="call-1")

    evidence = collect_evidence([first, tool], [], run_id=RUN_ID)

    assert evidence.answer == "First answer"


def test_collect_evidence_deduplicates_calls_present_in_messages_and_events() -> None:
    call = _call(
        "tool_execute",
        {"tool_name": "mcp__weather__forecast", "arguments": {"city": "Paris"}},
        "call-weather",
    )
    parent = _ai(tool_calls=[call], message_id="ai-parent")
    result = _tool(
        content='{"temperature_c": 21}', call_id="call-weather", name="tool_execute"
    )

    evidence = collect_evidence(
        [parent, result],
        [_event("llm.ai.response", parent), _event("llm.tool.result", result)],
        run_id=RUN_ID,
    )

    assert len(evidence.tool_calls) == 1
    assert len(evidence.business_tool_calls) == 1
    assert evidence.business_tool_calls[0]["result"] == '{"temperature_c": 21}'


def test_subagent_calls_keep_routing_without_a_start_and_unmatched_starts() -> None:
    task_call = _call(
        "task",
        {"subagent_type": "knowledge-researcher", "description": "look up"},
        "call-orphan",
    )
    parent = _ai(tool_calls=[task_call], message_id="ai-parent")
    start_content = {
        "description": "find skills",
        "subagent_type": "skill-finder",
        "model_name": "model-skill",
    }
    start = _event(
        "subagent.start",
        start_content,
        category="subagent",
        metadata={"task_id": "call-started-only", **start_content},
    )

    evidence = collect_evidence([parent], [start], run_id=RUN_ID)

    assert evidence.subagent_calls == [
        {
            "tool_call_id": "call-orphan",
            "subagent_type": "knowledge-researcher",
            "args": {"subagent_type": "knowledge-researcher", "description": "look up"},
            "started": False,
            "model_name": None,
            "description": None,
        },
        {
            "tool_call_id": "call-started-only",
            "subagent_type": "skill-finder",
            "args": {},
            "started": True,
            "model_name": "model-skill",
            "description": "find skills",
        },
    ]


def test_citations_drop_entries_without_the_chunk_identity() -> None:
    final = _ai(
        content="Answer [kabc12345].",
        citations=[
            CITATION,
            {"citation_id": "kbad1", "chunk_id": "doc-1:1"},
            {"citation_id": "kbad2", "document_id": "doc-1"},
            {"chunk_id": "doc-1:2", "document_id": "doc-1"},
            "not a mapping",
        ],
        message_id="ai-final",
    )

    evidence = collect_evidence([final], [], run_id=RUN_ID)

    assert evidence.citations == [CITATION]


def test_missing_retrieval_events_yield_empty_lists() -> None:
    answer = _ai(content="Known answer.", message_id="ai-final")

    evidence = collect_evidence([answer], [], run_id=RUN_ID)

    assert evidence.answer == "Known answer."
    assert evidence.retrieved_tools == []
    assert evidence.retrieved_skills == []
    assert evidence.retrieved_chunks == []
    assert evidence.citations == []
    assert evidence.kb_called is False


def test_empty_inputs_yield_empty_evidence() -> None:
    evidence = collect_evidence([], [], run_id=RUN_ID)

    assert evidence.answer == ""
    assert evidence.tool_calls == []
    assert evidence.business_tool_calls == []
    assert evidence.subagent_calls == []
    assert evidence.retrieved_tools == []
    assert evidence.retrieved_skills == []
    assert evidence.loaded_skills == []
    assert evidence.kb_called is False
    assert evidence.retrieved_chunks == []
    assert evidence.citations == []
    assert evidence.trace_id is None


def test_malformed_rows_are_dropped_not_raised() -> None:
    events = [
        _event("llm.ai.response", "not a mapping"),
        _event("llm.tool.result", "not a mapping"),
        _event("retrieval.tool", {"tools": "not a list"}, category="retrieval"),
        _event(
            "retrieval.skill",
            {"skills": [None, {"name": ""}]},
            category="retrieval",
        ),
        _event("retrieval.kb", {"items": "not a list"}, category="retrieval"),
        _event("subagent.start", {}, category="subagent", metadata={}),
    ]
    messages: list[dict[str, Any]] = [
        {"type": "ai", "content": None, "tool_calls": [None, {"name": "", "id": "bad"}]}
    ]

    evidence = collect_evidence(messages, events, run_id=RUN_ID)

    assert evidence.answer == ""
    assert evidence.tool_calls == []
    assert evidence.business_tool_calls == []
    assert evidence.subagent_calls == []
    assert evidence.retrieved_tools == []
    assert evidence.retrieved_skills == []
    assert evidence.retrieved_chunks == []
    assert evidence.kb_called is True


def test_events_from_another_run_are_ignored() -> None:
    events = [
        _event(
            "retrieval.kb",
            {"query": "other", "items": [CHUNK_ONE], "truncated": False},
            category="retrieval",
            run_id=OTHER_RUN_ID,
        ),
        _event("llm.ai.response", _ai(content="Other answer"), run_id=OTHER_RUN_ID),
    ]

    evidence = collect_evidence([], events, run_id=RUN_ID)

    assert evidence.answer == ""
    assert evidence.kb_called is False
    assert evidence.retrieved_chunks == []
