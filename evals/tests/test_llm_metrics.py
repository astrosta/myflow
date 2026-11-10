"""Prompt-judge metric tests: applicability, prompts, parsing, and short-circuits."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from myflow_evals.evidence import RunEvidence
from myflow_evals.llm import JudgeError
from myflow_evals.llm_metrics import (
    CI_LLM_METRICS,
    FULL_ONLY_LLM_METRICS,
    LLM_METRICS,
    LLM_THRESHOLDS,
    JudgeCase,
    PromptJudgeEngine,
    metric_applicable,
)

HASHED_USER_ID = "9f2c4b6d8e0a1c3e5f7a9b1d3c5e7f90"


class FakeJudge:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _evidence(**overrides: Any) -> RunEvidence:
    fields: dict[str, Any] = {
        "answer": "The answer is forty-two.",
        "tool_calls": [],
        "business_tool_calls": [],
        "subagent_calls": [],
        "retrieved_tools": [],
        "retrieved_skills": [],
        "loaded_skills": [],
        "kb_called": True,
        "kb_truncated": False,
        "retrieved_chunks": [
            {"chunk_id": "c1", "document_id": "d1", "content": "chunk alpha text"}
        ],
        "citations": [],
        "trace_id": None,
    }
    fields.update(overrides)
    return RunEvidence(**fields)


def _case(
    expected: Mapping[str, Any] | None,
    *,
    evidence: RunEvidence | None = None,
    query: str = "what is the answer?",
    answer: str | None = None,
) -> JudgeCase:
    return JudgeCase(
        item_id="item-1",
        query=query,
        answer=answer if answer is not None else "The answer is forty-two.",
        expected_output=expected,
        evidence=evidence if evidence is not None else _evidence(),
        metadata={"category": "kb_single_hop", "user_id": HASHED_USER_ID},
    )


def _score_response(score: Any = 0.75, reason: str = "because") -> str:
    return json.dumps({"score": score, "reason": reason})


def test_the_metric_catalog_matches_the_design() -> None:
    assert set(LLM_METRICS) == {
        "skill_adherence",
        "faithfulness",
        "contextual_recall",
        "contextual_precision",
        "contextual_relevancy",
        "answer_quality",
        "answer_correctness",
        "answer_relevancy",
        "refusal_ok",
        "tool_correctness",
    }
    assert CI_LLM_METRICS == {
        "faithfulness",
        "answer_correctness",
        "skill_adherence",
        "refusal_ok",
    }
    assert FULL_ONLY_LLM_METRICS == {
        "contextual_precision",
        "contextual_relevancy",
        "answer_relevancy",
    }
    for metric, spec in LLM_THRESHOLDS.items():
        assert metric in LLM_METRICS
        assert spec["kind"] == "llm"
        assert spec["comparison"] in {"min", "max"}


def test_applicability_honors_the_sample_shape() -> None:
    assert metric_applicable("faithfulness", _case({"expect_kb": True})) is True
    assert metric_applicable("faithfulness", _case({"expect_kb": True, "expect_refusal": True})) is False
    assert metric_applicable("faithfulness", _case({"expect_kb": True}, evidence=_evidence(retrieved_chunks=[]))) is False
    assert metric_applicable("faithfulness", _case({"expect_kb": True}, evidence=_evidence(kb_truncated=True))) is False
    assert metric_applicable("answer_correctness", _case({"reference_answer": "42"})) is True
    assert metric_applicable("answer_correctness", _case({"reference_answer": "42", "expect_refusal": True})) is False
    assert metric_applicable("contextual_recall", _case({"reference_answer": "42"})) is True
    assert metric_applicable("contextual_recall", _case({})) is False
    assert metric_applicable("contextual_relevancy", _case({"expect_kb": True})) is True
    assert metric_applicable("answer_relevancy", _case({"judge_rule": "be brief"})) is True
    assert metric_applicable("answer_relevancy", _case({}, answer="")) is False
    assert metric_applicable("answer_quality", _case({"judge_rule": "be brief"})) is True
    assert metric_applicable("answer_quality", _case({"key_points": ["a"]})) is True
    assert metric_applicable("answer_quality", _case({"expect_kb": True})) is False
    assert metric_applicable("skill_adherence", _case({"expected_skills": ["s"], "skill_rules": ["r"]}, evidence=_evidence(loaded_skills=["s"], retrieved_skills=["s"]))) is True
    assert metric_applicable("skill_adherence", _case({"expected_skills": ["s"]}, evidence=_evidence(loaded_skills=["s"]))) is False
    assert metric_applicable("refusal_ok", _case({"expect_refusal": True})) is True
    assert metric_applicable("refusal_ok", _case({"expect_kb": True})) is False
    assert metric_applicable("tool_correctness", _case({"expected_tools": ["mcp__a__b"]})) is True
    assert metric_applicable("unknown", _case({"judge_rule": "x"})) is False


def test_judge_metrics_send_the_declared_inputs_and_parse_a_score() -> None:
    judge = FakeJudge(_score_response(0.75))
    engine = PromptJudgeEngine(judge)
    score = engine.score("faithfulness", _case({"expect_kb": True}))
    assert score is not None
    assert score.value == 0.75
    assert score.comment == "because"
    prompt = judge.calls[0][1]
    assert "what is the answer?" in prompt
    assert "chunk alpha text" in prompt
    assert "The answer is forty-two." in prompt
    assert HASHED_USER_ID not in prompt


def test_answer_metrics_include_their_declared_judging_material() -> None:
    expected = {
        "reference_answer": "reference answer text",
        "judge_rule": "judge rule text",
        "key_points": ["point one", "point two"],
    }
    judge = FakeJudge(_score_response(0.5))
    PromptJudgeEngine(judge).score("answer_correctness", _case(expected))
    assert "reference answer text" in judge.calls[0][1]

    quality_judge = FakeJudge(_score_response(0.5))
    PromptJudgeEngine(quality_judge).score("answer_quality", _case(expected))
    quality_prompt = quality_judge.calls[0][1]
    assert "judge rule text" in quality_prompt
    assert "point one" in quality_prompt
    assert "point two" in quality_prompt


def test_skill_adherence_scores_the_fraction_of_passing_rules() -> None:
    judge = FakeJudge(
        json.dumps(
            {
                "rules": [
                    {"rule": "first rule", "passed": True},
                    {"rule": "second rule", "passed": False},
                    {"rule": "third rule", "passed": True},
                ],
                "reason": "two of three",
            }
        )
    )
    evidence = _evidence(loaded_skills=["pdf-report"], retrieved_skills=["pdf-report"])
    score = PromptJudgeEngine(judge).score(
        "skill_adherence",
        _case(
            {"expected_skills": ["pdf-report"], "skill_rules": ["first rule", "second rule", "third rule"]},
            evidence=evidence,
        ),
    )
    assert score is not None
    assert score.value == pytest.approx(2 / 3)
    assert "first rule" in judge.calls[0][1]
    assert "pdf-report" in judge.calls[0][1]


def test_contextual_precision_uses_the_ranked_relevance_verdicts() -> None:
    judge = FakeJudge(json.dumps({"relevant": [True, False, True, False], "reason": "mixed"}))
    score = PromptJudgeEngine(judge).score(
        "contextual_precision",
        _case({"reference_answer": "42"}, evidence=_evidence(retrieved_chunks=[
            {"chunk_id": "a", "content": "relevant one"},
            {"chunk_id": "b", "content": "noise"},
            {"chunk_id": "c", "content": "relevant two"},
            {"chunk_id": "d", "content": "more noise"},
        ])),
    )
    assert score is not None
    assert score.value == pytest.approx((1.0 + 2 / 3) / 2)
    prompt = judge.calls[0][1]
    assert "relevant one" in prompt
    assert "more noise" in prompt


def test_tool_correctness_is_computed_without_calling_the_judge() -> None:
    judge = FakeJudge(_score_response(0.1))
    evidence = _evidence(
        business_tool_calls=[
            {"name": "mcp__a__find", "args": {}},
            {"name": "mcp__a__create", "args": {}},
        ]
    )
    score = PromptJudgeEngine(judge).score(
        "tool_correctness",
        _case({"expected_tools": ["mcp__a__find"]}, evidence=evidence),
    )
    assert score is not None
    assert score.value == 1.0
    assert judge.calls == []


def test_refusal_ok_short_circuits_clear_refusals_and_citations() -> None:
    judge = FakeJudge(_score_response(0.2))
    engine = PromptJudgeEngine(judge)
    refusal = engine.score("refusal_ok", _case({"expect_refusal": True}, answer="知识库中没有找到相关信息。"))
    assert refusal is not None and refusal.value == 1.0
    assert judge.calls == []

    cited = engine.score(
        "refusal_ok",
        _case(
            {"expect_refusal": True},
            answer="The local office opens at 9am.",
            evidence=_evidence(citations=[{"citation_id": "k1", "chunk_id": "c1", "document_id": "d1"}]),
        ),
    )
    assert cited is not None and cited.value == 0.0
    assert judge.calls == []

    ambiguous = engine.score("refusal_ok", _case({"expect_refusal": True}, answer="Let me think about it."))
    assert ambiguous is not None and ambiguous.value == 0.2
    assert len(judge.calls) == 1


def test_a_malformed_or_out_of_range_score_raises_a_judge_error() -> None:
    for response in ("not json", json.dumps({"score": 1.5, "reason": "too high"}), json.dumps({"reason": "missing"})):
        with pytest.raises(JudgeError):
            PromptJudgeEngine(FakeJudge(response)).score("faithfulness", _case({"expect_kb": True}))


def test_a_prose_wrapped_json_reply_is_still_parsed() -> None:
    judge = FakeJudge('Here is the result: {"score": 0.25, "reason": "ok"}')
    score = PromptJudgeEngine(judge).score("faithfulness", _case({"expect_kb": True}))
    assert score is not None and score.value == 0.25
