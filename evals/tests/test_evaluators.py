"""Rule-evaluator tests: routing, retrieval, tool calls, and skill loading.

Each test scores a hand-built ``RunEvidence`` -- the shape ``collect_evidence``
returns for a finished run -- and asserts the Langfuse ``Evaluation`` that comes back.
No Gateway, no Langfuse client, no mocks of the functions under test: the
evaluators are pure and their only observable output is the score.
"""

from __future__ import annotations

from typing import Any

import pytest
from langfuse import Evaluation

from myflow_evals.evaluators import (
    RULE_EVALUATORS,
    THRESHOLDS,
    args_accuracy,
    extra_calls_ratio,
    forbidden_violation,
    kb_mrr,
    kb_recall_at_5,
    kb_routing,
    no_tool_ok,
    retrieval_mrr,
    retrieval_recall_at_5,
    skill_extra,
    skill_hit,
    skill_mrr,
    skill_recall_at_5,
    skill_routing,
    tool_hit,
)
from myflow_evals.evidence import RunEvidence, collect_evidence

RUN_ID = "run-1"


def _evidence(**overrides: Any) -> RunEvidence:
    fields: dict[str, Any] = {
        "answer": "",
        "tool_calls": [],
        "business_tool_calls": [],
        "subagent_calls": [],
        "retrieved_tools": [],
        "retrieved_skills": [],
        "loaded_skills": [],
        "kb_called": False,
        "kb_truncated": False,
        "retrieved_chunks": [],
        "citations": [],
        "trace_id": None,
    }
    fields.update(overrides)
    return RunEvidence(**fields)


def _subagent(subagent_type: str, *, started: bool = True) -> dict[str, Any]:
    return {
        "tool_call_id": f"call-{subagent_type}",
        "subagent_type": subagent_type,
        "args": {"subagent_type": subagent_type},
        "started": started,
        "model_name": None,
        "description": None,
    }


def _business_call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "tool_call_id": f"call-{name}",
        "name": name,
        "args": dict(args or {}),
        "result": "",
    }


def _raw_call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """A raw `tool_calls` row, infrastructure names included."""
    return _business_call(name, args)


def _chunk(document_id: Any, index: int = 0) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chunk_id": f"{document_id}:{index}",
        "citation_id": f"k{index:08d}",
        "content": "text",
    }


def _event(event_type: str, content: Any, *, seq: int = 1) -> dict[str, Any]:
    return {
        "seq": seq,
        "run_id": RUN_ID,
        "event_type": event_type,
        "category": "message",
        "content": content,
        "metadata": {},
        "created_at": None,
    }


def _ai_row(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "ai-1",
        "type": "ai",
        "content": "",
        "tool_calls": tool_calls,
        "additional_kwargs": {},
    }


# --- T1 tool retrieval -----------------------------------------------------


def test_retrieval_recall_and_mrr_read_the_top_five() -> None:
    evidence = _evidence(
        retrieved_tools=["mcp__a__one", "mcp__b__two", "mcp__c__three"]
    )
    expected = {"expected_tools": ["mcp__b__two", "mcp__z__late"]}

    recall = retrieval_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.name == "retrieval_recall@5"
    assert recall.value == 0.5
    assert "1/2" in recall.comment

    mrr = retrieval_mrr(expected_output=expected, evidence=evidence)
    assert mrr is not None
    assert mrr.name == "retrieval_mrr"
    assert mrr.value == 0.5  # mcp__b__two ranked second
    assert "rank 2" in mrr.comment


def test_retrieval_recall_stops_at_five_while_mrr_ranks_the_whole_list() -> None:
    retrieved = [f"mcp__x__t{index}" for index in range(5)] + ["mcp__x__expected"]
    evidence = _evidence(retrieved_tools=retrieved)
    expected = {"expected_tools": ["mcp__x__expected"]}

    recall = retrieval_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.value == 0.0

    mrr = retrieval_mrr(expected_output=expected, evidence=evidence)
    assert mrr is not None
    assert mrr.value == pytest.approx(1 / 6)


def test_retrieval_metrics_return_none_without_expected_tools() -> None:
    evidence = _evidence(retrieved_tools=["mcp__a__one"])

    assert retrieval_recall_at_5(expected_output={}, evidence=evidence) is None
    assert retrieval_mrr(expected_output={}, evidence=evidence) is None
    for empty in ({"expected_tools": []}, {"expected_tools": None}):
        assert retrieval_recall_at_5(expected_output=empty, evidence=evidence) is None
        assert retrieval_mrr(expected_output=empty, evidence=evidence) is None


# --- T2 tool selection and arguments --------------------------------------


def test_tool_hit_scores_unwrapped_business_calls() -> None:
    evidence = _evidence(
        business_tool_calls=[_business_call("mcp__db__query", {"sql": "select 1"})]
    )

    hit = tool_hit(
        expected_output={"expected_tools": ["mcp__db__query"]}, evidence=evidence
    )
    assert hit is not None
    assert hit.name == "tool_hit"
    assert hit.value == 1.0

    miss = tool_hit(
        expected_output={"expected_tools": ["mcp__db__query", "mcp__db__write"]},
        evidence=evidence,
    )
    assert miss is not None
    assert miss.value == 0.0
    assert "mcp__db__write" in miss.comment


def test_tool_hit_returns_none_for_an_expectation_free_sample() -> None:
    assert tool_hit(expected_output={}, evidence=_evidence()) is None


def _tool_execute_evidence() -> RunEvidence:
    """One tool_execute round trip exactly as the run ledger records it."""
    return collect_evidence(
        [],
        [
            _event(
                "llm.ai.response",
                _ai_row(
                    [
                        {
                            "id": "c1",
                            "name": "tool_execute",
                            "args": {
                                "tool_name": "mcp__db__query",
                                "arguments": {"sql": "select 1"},
                            },
                        }
                    ]
                ),
            ),
            _event(
                "llm.tool.result",
                {"tool_call_id": "c1", "content": "1 row"},
                seq=2,
            ),
        ],
        run_id=RUN_ID,
    )


def test_tool_hit_reads_the_tool_execute_unwrap() -> None:
    evidence = _tool_execute_evidence()
    assert [call["name"] for call in evidence.business_tool_calls] == [
        "mcp__db__query"
    ]

    score = tool_hit(
        expected_output={"expected_tools": ["mcp__db__query"]}, evidence=evidence
    )
    assert score is not None
    assert score.value == 1.0


def test_args_accuracy_reads_the_tool_execute_arguments() -> None:
    evidence = _tool_execute_evidence()

    score = args_accuracy(
        expected_output={
            "expected_calls": [
                {
                    "tool": "mcp__db__query",
                    "args": {"sql": "select 1"},
                    "check_keys": ["sql"],
                }
            ]
        },
        evidence=evidence,
    )
    assert score is not None
    assert score.value == 1.0


def test_args_accuracy_matches_check_keys_by_tool_and_value() -> None:
    evidence = _evidence(
        business_tool_calls=[
            _business_call("mcp__db__query", {"sql": "select 1", "limit": 5})
        ]
    )
    expected = {
        "expected_calls": [
            {
                "tool": "mcp__db__query",
                "args": {"sql": "select 1", "limit": 10},
                "check_keys": ["sql", "limit"],
            }
        ]
    }

    score = args_accuracy(expected_output=expected, evidence=evidence)
    assert score is not None
    assert score.name == "args_accuracy"
    assert score.value == 0.5
    assert "1/2" in score.comment

    matched = args_accuracy(
        expected_output={
            "expected_calls": [
                {
                    "tool": "mcp__db__query",
                    "args": {"sql": "select 1"},
                    "check_keys": ["sql"],
                }
            ]
        },
        evidence=evidence,
    )
    assert matched is not None
    assert matched.value == 1.0


def test_args_accuracy_does_not_match_a_key_without_a_declared_value() -> None:
    evidence = _evidence(
        business_tool_calls=[_business_call("mcp__db__query", {"sql": None})]
    )
    expected = {
        "expected_calls": [
            {"tool": "mcp__db__query", "args": {}, "check_keys": ["sql"]}
        ]
    }

    score = args_accuracy(expected_output=expected, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
    assert "0/1" in score.comment


def test_args_accuracy_ignores_calls_to_another_tool() -> None:
    evidence = _evidence(
        business_tool_calls=[_business_call("mcp__db__other", {"sql": "select 1"})]
    )
    expected = {
        "expected_calls": [
            {
                "tool": "mcp__db__query",
                "args": {"sql": "select 1"},
                "check_keys": ["sql"],
            }
        ]
    }

    score = args_accuracy(expected_output=expected, evidence=evidence)
    assert score is not None
    assert score.value == 0.0


def test_args_accuracy_is_none_without_check_keys() -> None:
    evidence = _evidence(business_tool_calls=[_business_call("mcp__db__query")])

    assert args_accuracy(expected_output={"expected_calls": []}, evidence=evidence) is None
    assert (
        args_accuracy(
            expected_output={
                "expected_calls": [
                    {"tool": "mcp__db__query", "args": {}, "check_keys": []}
                ]
            },
            evidence=evidence,
        )
        is None
    )


def test_extra_calls_ratio_divides_business_calls_by_expected_calls() -> None:
    evidence = _evidence(
        business_tool_calls=[
            _business_call("mcp__a__one"),
            _business_call("mcp__a__two"),
            _business_call("mcp__a__three"),
        ]
    )
    expected = {
        "expected_calls": [{"tool": "mcp__a__one"}, {"tool": "mcp__a__two"}]
    }

    score = extra_calls_ratio(expected_output=expected, evidence=evidence)
    assert score is not None
    assert score.name == "extra_calls_ratio"
    assert score.value == 1.5
    assert "3 business calls" in score.comment

    assert extra_calls_ratio(expected_output={}, evidence=evidence) is None


def test_extra_calls_ratio_skips_infrastructure_calls() -> None:
    evidence = collect_evidence(
        [],
        [
            _event(
                "llm.ai.response",
                _ai_row(
                    [
                        {"id": "c1", "name": "tool_search", "args": {"query": "db"}},
                        {"id": "c2", "name": "load_skill", "args": {"name": "deploy"}},
                        {
                            "id": "c3",
                            "name": "task",
                            "args": {"subagent_type": "skill-finder"},
                        },
                        {
                            "id": "c4",
                            "name": "tool_execute",
                            "args": {
                                "tool_name": "mcp__db__query",
                                "arguments": {"sql": "select 1"},
                            },
                        },
                        {"id": "c5", "name": "knowledge_search", "args": {"query": "x"}},
                    ]
                ),
            )
        ],
        run_id=RUN_ID,
    )
    assert [call["name"] for call in evidence.business_tool_calls] == ["mcp__db__query"]

    score = extra_calls_ratio(
        expected_output={"expected_calls": [{"tool": "mcp__db__query"}]},
        evidence=evidence,
    )
    assert score is not None
    assert score.value == 1.0


# --- K0 routing ------------------------------------------------------------


def test_skill_routing_passes_only_when_skill_finder_is_delegated() -> None:
    routed = _evidence(subagent_calls=[_subagent("skill-finder")])
    expected = {"expect_skill": True}

    score = skill_routing(expected_output=expected, evidence=routed)
    assert score is not None
    assert score.name == "skill_routing"
    assert score.value == 1.0
    assert "routed+started" in score.comment

    not_routed = _evidence()
    assert skill_routing(expected_output=expected, evidence=not_routed).value == 0.0


def test_skill_routing_reports_a_routed_but_never_started_subagent() -> None:
    evidence = _evidence(subagent_calls=[_subagent("skill-finder", started=False)])

    score = skill_routing(expected_output={"expect_skill": True}, evidence=evidence)
    assert score is not None
    assert score.value == 1.0  # delegation happened; the missing start is diagnostic
    assert "routed, no subagent.start" in score.comment


def test_skill_routing_passes_when_skill_is_not_expected_and_not_routed() -> None:
    score = skill_routing(expected_output={"expect_skill": False}, evidence=_evidence())
    assert score is not None
    assert score.value == 1.0
    assert "not routed" in score.comment


def test_skill_routing_ignores_a_static_load_skill_call() -> None:
    evidence = _evidence(
        tool_calls=[
            {
                "tool_call_id": "c1",
                "name": "load_skill",
                "args": {"name": "deploy"},
                "result": "",
            }
        ],
        loaded_skills=["deploy"],
    )

    score = skill_routing(expected_output={"expect_skill": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0


def test_skill_routing_returns_none_when_expectation_is_absent() -> None:
    assert skill_routing(expected_output={}, evidence=_evidence()) is None


def test_routing_metrics_require_a_boolean_declaration() -> None:
    evidence = _evidence()

    assert skill_routing(expected_output={"expect_skill": "yes"}, evidence=evidence) is None
    assert kb_routing(expected_output={"expect_kb": 1}, evidence=evidence) is None


# --- K1 skill retrieval ----------------------------------------------------


def test_skill_recall_and_mrr_read_skill_finder_retrieval() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("skill-finder")],
        retrieved_skills=["alpha", "deploy", "gamma", "delta", "epsilon"],
    )
    expected = {"expected_skills": ["deploy"]}

    recall = skill_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.name == "skill_recall@5"
    assert recall.value == 1.0

    mrr = skill_mrr(expected_output=expected, evidence=evidence)
    assert mrr is not None
    assert mrr.name == "skill_mrr"
    assert mrr.value == 0.5


def test_skill_recall_is_none_when_skill_finder_was_never_routed() -> None:
    evidence = _evidence(retrieved_skills=["deploy"])
    expected = {"expected_skills": ["deploy"]}

    assert skill_recall_at_5(expected_output=expected, evidence=evidence) is None
    assert skill_mrr(expected_output=expected, evidence=evidence) is None


def test_skill_recall_is_zero_when_routed_without_retrieval() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("skill-finder")], loaded_skills=["deploy"]
    )
    expected = {"expected_skills": ["deploy"]}

    recall = skill_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.value == 0.0

    mrr = skill_mrr(expected_output=expected, evidence=evidence)
    assert mrr is not None
    assert mrr.value == 0.0


def test_skill_recall_counts_only_the_top_five() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("skill-finder")],
        retrieved_skills=["a", "b", "c", "d", "e", "deploy"],
    )
    expected = {"expected_skills": ["deploy"]}

    recall = skill_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.value == 0.0
    assert skill_mrr(expected_output=expected, evidence=evidence).value == pytest.approx(
        1 / 6
    )


def test_skill_recall_is_none_without_expected_skills() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("skill-finder")], retrieved_skills=["deploy"]
    )

    assert skill_recall_at_5(expected_output={}, evidence=evidence) is None
    assert skill_mrr(expected_output={}, evidence=evidence) is None


# --- K2 skill selection ----------------------------------------------------


def test_skill_hit_requires_loaded_and_retrieved() -> None:
    expected = {"expected_skills": ["deploy"]}

    loaded = _evidence(retrieved_skills=["deploy"], loaded_skills=["deploy"])
    hit = skill_hit(expected_output=expected, evidence=loaded)
    assert hit is not None
    assert hit.name == "skill_hit"
    assert hit.value == 1.0

    unretrieved = _evidence(retrieved_skills=[], loaded_skills=["deploy"])
    stale = skill_hit(expected_output=expected, evidence=unretrieved)
    assert stale is not None
    assert stale.value == 0.0
    assert "retrieval" in stale.comment

    nothing_loaded = _evidence(retrieved_skills=["deploy"], loaded_skills=[])
    missing = skill_hit(expected_output=expected, evidence=nothing_loaded)
    assert missing is not None
    assert missing.value == 0.0
    assert "not loaded" in missing.comment


def test_skill_hit_is_none_without_expected_skills() -> None:
    evidence = _evidence(retrieved_skills=["deploy"], loaded_skills=["deploy"])

    assert skill_hit(expected_output={}, evidence=evidence) is None


def test_skill_extra_counts_unexpected_loads() -> None:
    evidence = _evidence(
        retrieved_skills=["deploy", "ops"], loaded_skills=["deploy", "ops"]
    )

    score = skill_extra(expected_output={"expected_skills": ["deploy"]}, evidence=evidence)
    assert score is not None
    assert score.name == "skill_extra"
    assert score.value == 1.0
    assert "ops" in score.comment

    assert skill_extra(expected_output={}, evidence=evidence) is None


# --- R0 routing ------------------------------------------------------------


def test_kb_routing_passes_only_when_knowledge_researcher_is_delegated() -> None:
    routed = _evidence(subagent_calls=[_subagent("knowledge-researcher")])
    expected = {"expect_kb": True}

    score = kb_routing(expected_output=expected, evidence=routed)
    assert score is not None
    assert score.name == "kb_routing"
    assert score.value == 1.0
    assert "routed+started" in score.comment


def test_kb_routing_rejects_a_direct_knowledge_search_call() -> None:
    evidence = _evidence(
        tool_calls=[_business_call("knowledge_search", {"query": "refunds"})],
        kb_called=True,
    )

    score = kb_routing(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
    assert "not routed" in score.comment


def test_kb_routing_ignores_knowledge_search_when_kb_is_not_expected() -> None:
    evidence = _evidence(
        tool_calls=[_business_call("knowledge_search", {"query": "refunds"})],
        kb_called=True,
    )

    score = kb_routing(expected_output={"expect_kb": False}, evidence=evidence)
    assert score is not None
    assert score.value == 1.0


def test_kb_routing_is_not_satisfied_by_a_skill_finder_route() -> None:
    evidence = _evidence(subagent_calls=[_subagent("skill-finder")])

    score = kb_routing(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0


@pytest.mark.parametrize(
    ("evaluator", "subagent_type", "flag"),
    [
        (skill_routing, "skill-finder", "expect_skill"),
        (kb_routing, "knowledge-researcher", "expect_kb"),
    ],
)
def test_routing_fails_when_a_subagent_is_routed_but_not_expected(
    evaluator: Any, subagent_type: str, flag: str
) -> None:
    evidence = _evidence(subagent_calls=[_subagent(subagent_type)])

    score = evaluator(expected_output={flag: False}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
    assert "routed+started" in score.comment


def test_kb_routing_returns_none_when_expectation_is_absent() -> None:
    assert kb_routing(expected_output={}, evidence=_evidence()) is None


# --- R1 knowledge retrieval ------------------------------------------------


def test_kb_recall_and_mrr_score_document_ids_in_the_top_five() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("knowledge-researcher")],
        retrieved_chunks=[_chunk("doc-3"), _chunk("doc-1"), _chunk("doc-2")],
    )
    expected = {"expected_doc_ids": ["doc-1", "doc-9"]}

    recall = kb_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.name == "kb_recall@5"
    assert recall.value == 0.5

    mrr = kb_mrr(expected_output=expected, evidence=evidence)
    assert mrr is not None
    assert mrr.name == "kb_mrr"
    assert mrr.value == 0.5


def test_kb_recall_ignores_chunks_beyond_rank_five() -> None:
    chunks = [_chunk(f"doc-{index}") for index in range(5)] + [_chunk("doc-expected")]
    evidence = _evidence(
        subagent_calls=[_subagent("knowledge-researcher")],
        retrieved_chunks=chunks,
    )
    expected = {"expected_doc_ids": ["doc-expected"]}

    recall = kb_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.value == 0.0
    assert kb_mrr(expected_output=expected, evidence=evidence).value == pytest.approx(
        1 / 6
    )


def test_kb_recall_is_none_when_knowledge_researcher_was_never_routed() -> None:
    evidence = _evidence(retrieved_chunks=[_chunk("doc-1")], kb_called=True)
    expected = {"expected_doc_ids": ["doc-1"]}

    assert kb_recall_at_5(expected_output=expected, evidence=evidence) is None
    assert kb_mrr(expected_output=expected, evidence=evidence) is None


def test_kb_recall_is_zero_when_routed_without_retrieval() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("knowledge-researcher")], kb_called=True
    )
    expected = {"expected_doc_ids": ["doc-1"]}

    recall = kb_recall_at_5(expected_output=expected, evidence=evidence)
    assert recall is not None
    assert recall.value == 0.0

    mrr = kb_mrr(expected_output=expected, evidence=evidence)
    assert mrr is not None
    assert mrr.value == 0.0


def test_kb_recall_is_none_without_expected_doc_ids() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("knowledge-researcher")],
        retrieved_chunks=[_chunk("doc-1")],
    )

    assert kb_recall_at_5(expected_output={}, evidence=evidence) is None
    assert kb_mrr(expected_output={}, evidence=evidence) is None


# --- S1 boundary -----------------------------------------------------------


def test_forbidden_violation_flags_business_calls() -> None:
    called = _evidence(
        business_tool_calls=[_business_call("mcp__db__drop", {"table": "users"})]
    )
    expected = {"forbidden_tools": ["mcp__db__drop"]}

    violation = forbidden_violation(expected_output=expected, evidence=called)
    assert violation is not None
    assert violation.name == "forbidden_violation"
    assert violation.value == 1.0
    assert "mcp__db__drop" in violation.comment

    quiet = _evidence(business_tool_calls=[_business_call("mcp__db__query")])
    clean = forbidden_violation(expected_output=expected, evidence=quiet)
    assert clean is not None
    assert clean.value == 0.0

    no_list = forbidden_violation(
        expected_output={"forbidden_tools": []}, evidence=called
    )
    assert no_list is None


NO_TOOL_METADATA: dict[str, Any] = {"category": "no_tool", "source": "hand_built"}


def test_no_tool_ok_scores_an_explicit_no_tool_sample() -> None:
    ok = no_tool_ok(expected_output={}, evidence=_evidence(), metadata=NO_TOOL_METADATA)
    assert ok is not None
    assert ok.name == "no_tool_ok"
    assert ok.value == 1.0

    for evidence in (
        _evidence(business_tool_calls=[_business_call("mcp__db__query")]),
        _evidence(loaded_skills=["deploy"]),
        _evidence(kb_called=True),
        _evidence(retrieved_tools=["mcp__db__query"]),
        _evidence(retrieved_skills=["deploy"]),
    ):
        score = no_tool_ok(
            expected_output={}, evidence=evidence, metadata=NO_TOOL_METADATA
        )
        assert score is not None
        assert score.value == 0.0


def test_no_tool_ok_fails_a_retrieval_attempt_that_returned_nothing() -> None:
    evidence = _evidence(
        tool_calls=[
            _raw_call("tool_search", {"query": "database"}),
            _raw_call("knowledge_search", {"query": "refunds"}),
        ]
    )

    score = no_tool_ok(expected_output={}, evidence=evidence, metadata=NO_TOOL_METADATA)
    assert score is not None
    assert score.value == 0.0
    assert "tool_search" in score.comment


def test_no_tool_ok_is_none_without_the_no_tool_category() -> None:
    evidence = _evidence()

    assert no_tool_ok(expected_output={}, evidence=evidence) is None
    assert no_tool_ok(expected_output={}, evidence=evidence, metadata=None) is None

    trace = {"user_id": "hashed", "source": "trace", "category": "trace_replay"}
    assert no_tool_ok(expected_output={}, evidence=evidence, metadata=trace) is None

    tool_item = {"category": "single_tool"}
    assert no_tool_ok(expected_output={}, evidence=evidence, metadata=tool_item) is None


def test_no_tool_ok_is_none_when_the_expected_output_is_missing() -> None:
    score = no_tool_ok(
        expected_output=None, evidence=_evidence(), metadata=NO_TOOL_METADATA
    )
    assert score is None


# --- catalog ---------------------------------------------------------------

RICH_EXPECTED = {
    "expected_tools": ["mcp__db__query"],
    "expected_calls": [
        {"tool": "mcp__db__query", "args": {"sql": "select 1"}, "check_keys": ["sql"]}
    ],
    "expected_skills": ["deploy"],
    "expected_doc_ids": ["doc-1"],
    "forbidden_tools": ["mcp__db__drop"],
    "expect_kb": True,
    "expect_skill": True,
}


def _rich_evidence() -> RunEvidence:
    return _evidence(
        business_tool_calls=[_business_call("mcp__db__query", {"sql": "select 1"})],
        tool_calls=[_business_call("mcp__db__query", {"sql": "select 1"})],
        subagent_calls=[_subagent("skill-finder"), _subagent("knowledge-researcher")],
        retrieved_tools=["mcp__db__query"],
        retrieved_skills=["deploy"],
        loaded_skills=["deploy"],
        kb_called=True,
        retrieved_chunks=[
            {"document_id": "doc-1", "chunk_id": "doc-1:0", "citation_id": "k00000001"}
        ],
        citations=[
            {"citation_id": "k00000001", "chunk_id": "doc-1:0", "document_id": "doc-1"}
        ],
    )


def test_every_evaluator_accepts_item_metadata() -> None:
    evidence = _rich_evidence()
    metadata = {"category": "kb_single_hop", "source": "synthetic_from_docs"}

    for name, evaluator in RULE_EVALUATORS.items():
        if name == "no_tool_ok":
            continue
        without = evaluator(expected_output=RICH_EXPECTED, evidence=evidence)
        with_metadata = evaluator(
            expected_output=RICH_EXPECTED, evidence=evidence, metadata=metadata
        )
        assert with_metadata is not None, name
        assert (with_metadata.name, with_metadata.value, with_metadata.comment) == (
            without.name,
            without.value,
            without.comment,
        ), name


def test_rule_evaluators_map_the_design_metric_names() -> None:
    assert set(RULE_EVALUATORS) == {
        "retrieval_recall@5",
        "retrieval_mrr",
        "tool_hit",
        "args_accuracy",
        "extra_calls_ratio",
        "skill_routing",
        "skill_recall@5",
        "skill_mrr",
        "skill_hit",
        "skill_extra",
        "kb_routing",
        "kb_recall@5",
        "kb_mrr",
        "citation_valid",
        "forbidden_violation",
        "no_tool_ok",
    }


def test_every_evaluator_names_itself_after_its_rule() -> None:
    evidence = _rich_evidence()

    for name, evaluator in RULE_EVALUATORS.items():
        if name == "no_tool_ok":
            result = evaluator(
                expected_output={}, evidence=_evidence(), metadata=NO_TOOL_METADATA
            )
        else:
            result = evaluator(expected_output=RICH_EXPECTED, evidence=evidence)
        assert result is not None, name
        assert result.name == name
        assert result.value is not None


@pytest.mark.parametrize(
    ("name", "threshold", "comparison"),
    [
        ("retrieval_recall@5", 0.95, "min"),
        ("tool_hit", 0.90, "min"),
        ("args_accuracy", 0.85, "min"),
        ("extra_calls_ratio", 1.5, "max"),
        ("skill_routing", 0.95, "min"),
        ("skill_recall@5", 0.95, "min"),
        ("skill_hit", 0.90, "min"),
        ("kb_routing", 0.95, "min"),
        ("kb_recall@5", 0.85, "min"),
        ("citation_valid", 0.95, "min"),
    ],
)
def test_thresholds_carry_the_design_gates(
    name: str, threshold: float, comparison: str
) -> None:
    assert name in RULE_EVALUATORS
    assert THRESHOLDS[name] == {
        "value": threshold,
        "comparison": comparison,
        "kind": "rule",
    }


def test_thresholds_gate_only_rule_metrics() -> None:
    assert len(THRESHOLDS) == 11
    assert set(THRESHOLDS) <= set(RULE_EVALUATORS)
    for name in ("retrieval_mrr", "skill_mrr", "kb_mrr", "skill_extra", "no_tool_ok"):
        assert name not in THRESHOLDS
    # A forbidden call must never happen, so its gate is the zero upper bound.
    assert THRESHOLDS["forbidden_violation"] == {
        "value": 0.0,
        "comparison": "max",
        "kind": "rule",
    }


# --- robustness ------------------------------------------------------------


@pytest.mark.parametrize(
    "expected_output",
    [
        {},
        {"expected_calls": "not-a-list"},
        {"expected_calls": [{"tool": None, "check_keys": "x"}]},
        {"expected_tools": "mcp__db__query"},
        {"expected_skills": [None, 3]},
        {"expected_doc_ids": {"doc-1": True}},
        {"forbidden_tools": 7},
        {"expect_kb": "yes"},
        {"expect_skill": None},
        None,
    ],
)
def test_malformed_expected_output_never_raises(expected_output: Any) -> None:
    evidence = _rich_evidence()

    for evaluator in RULE_EVALUATORS.values():
        result = evaluator(expected_output=expected_output, evidence=evidence)
        assert result is None or isinstance(result, Evaluation)


def test_malformed_retrieval_rows_never_raise() -> None:
    evidence = _evidence(
        subagent_calls=[_subagent("knowledge-researcher")],
        retrieved_chunks=[{"chunk_id": "doc-1:0"}, {"document_id": None}, {"content": "x"}],
        citations=[{"citation_id": "kforged", "chunk_id": "doc-1:0"}],
    )
    expected = {"expected_doc_ids": ["doc-1"], "expect_kb": True}

    assert kb_recall_at_5(expected_output=expected, evidence=evidence).value == 0.0
    assert kb_mrr(expected_output=expected, evidence=evidence).value == 0.0
    citation = RULE_EVALUATORS["citation_valid"](
        expected_output=expected, evidence=evidence
    )
    assert citation.value == 0.0
