"""DeepEval engine tests using an injected fake deepeval package."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from myflow_evals.evidence import RunEvidence
from myflow_evals.llm import JudgeError
from myflow_evals.llm_metrics import JudgeCase, PromptJudgeEngine
from myflow_evals.deepeval_engine import (
    DeepEvalEngine,
    _chat_model,
    build_engine,
    deepeval_available,
)

HASHED_USER_ID = "9f2c4b6d8e0a1c3e5f7a9b1d3c5e7f90"


class FakeJudge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return "0.5"


class FakeBaseLLM:
    def __init__(self, model: str | None = None, *args: Any, **kwargs: Any) -> None:
        self.name = model
        self.model = self.load_model()

    def load_model(self, *args: Any, **kwargs: Any) -> "FakeBaseLLM":
        return self

    def generate(self, *args: Any, **kwargs: Any) -> str:
        raise NotImplementedError

    async def a_generate(self, *args: Any, **kwargs: Any) -> str:
        raise NotImplementedError

    def get_model_name(self, *args: Any, **kwargs: Any) -> str | None:
        return self.name


class FakeToolCall:
    def __init__(self, /, **data: Any) -> None:
        self.__dict__.update(data)


class FakeTestCase:
    def __init__(self, /, **data: Any) -> None:
        self.__dict__.update(data)


class FakeParams:
    INPUT = "input"
    ACTUAL_OUTPUT = "actual_output"
    EXPECTED_OUTPUT = "expected_output"
    RETRIEVAL_CONTEXT = "retrieval_context"
    TOOLS_CALLED = "tools_called"
    EXPECTED_TOOLS = "expected_tools"
    CONTEXT = "context"


class FakeMetric:
    last: "FakeMetric | None" = None
    score_value = 0.66
    reason_text = "a fake reason"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.measured: Any = None
        self.score: Any = self.score_value
        self.reason: Any = self.reason_text
        FakeMetric.last = self

    def measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
        self.measured = test_case
        return self.score


def _metric_class(name: str) -> type:
    return type(name, (FakeMetric,), {})


@pytest.fixture
def fake_deepeval(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    deepeval = types.ModuleType("deepeval")
    models = types.ModuleType("deepeval.models")
    metrics = types.ModuleType("deepeval.metrics")
    test_case = types.ModuleType("deepeval.test_case")
    models.DeepEvalBaseLLM = FakeBaseLLM
    metrics.FaithfulnessMetric = _metric_class("FaithfulnessMetric")
    metrics.AnswerRelevancyMetric = _metric_class("AnswerRelevancyMetric")
    metrics.ContextualRecallMetric = _metric_class("ContextualRecallMetric")
    metrics.ContextualPrecisionMetric = _metric_class("ContextualPrecisionMetric")
    metrics.ContextualRelevancyMetric = _metric_class("ContextualRelevancyMetric")
    metrics.ToolCorrectnessMetric = _metric_class("ToolCorrectnessMetric")
    metrics.GEval = _metric_class("GEval")
    test_case.LLMTestCase = FakeTestCase
    test_case.ToolCall = FakeToolCall
    test_case.LLMTestCaseParams = FakeParams
    deepeval.models = models
    deepeval.metrics = metrics
    deepeval.test_case = test_case
    monkeypatch.setitem(sys.modules, "deepeval", deepeval)
    monkeypatch.setitem(sys.modules, "deepeval.models", models)
    monkeypatch.setitem(sys.modules, "deepeval.metrics", metrics)
    monkeypatch.setitem(sys.modules, "deepeval.test_case", test_case)
    return deepeval


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


def _case(expected: dict[str, Any], **overrides: Any) -> JudgeCase:
    return JudgeCase(
        item_id="item-1",
        query="what is the answer?",
        answer="The answer is forty-two.",
        expected_output=expected,
        evidence=overrides.pop("evidence", _evidence()),
        metadata={"category": "kb_single_hop", "user_id": HASHED_USER_ID},
    )


def test_deepeval_availability_detects_the_import(fake_deepeval: types.ModuleType) -> None:
    assert deepeval_available() is True


def test_build_engine_prefers_deepeval_by_default(fake_deepeval: types.ModuleType) -> None:
    engine = build_engine(FakeJudge(), preference="auto")
    assert isinstance(engine, DeepEvalEngine)


def test_build_engine_falls_back_to_prompt_when_deepeval_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("deepeval", "deepeval.models", "deepeval.metrics", "deepeval.test_case"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    engine = build_engine(FakeJudge(), preference="auto")
    assert isinstance(engine, PromptJudgeEngine)
    with pytest.raises(JudgeError, match="DeepEval"):
        build_engine(FakeJudge(), preference="deepeval")


def test_build_engine_returns_none_without_a_judge(fake_deepeval: types.ModuleType) -> None:
    assert build_engine(None) is None


def test_build_engine_rejects_an_unknown_preference(fake_deepeval: types.ModuleType) -> None:
    with pytest.raises(JudgeError, match="preference"):
        build_engine(FakeJudge(), preference="magic")


def test_the_chat_adapter_forwards_to_the_judge(fake_deepeval: types.ModuleType) -> None:
    judge = FakeJudge()
    chat = _chat_model(judge, model_name="judge-model")
    assert chat.get_model_name() == "judge-model"
    assert chat.generate("hello") == "0.5"
    assert judge.calls[0][1] == "hello"


def test_the_chat_adapter_forwards_async_calls(fake_deepeval: types.ModuleType) -> None:
    import asyncio

    judge = FakeJudge()
    chat = _chat_model(judge, model_name="judge-model")
    assert asyncio.run(chat.a_generate("hello")) == "0.5"


def test_deepeval_rag_metrics_receive_the_retrieved_context(
    fake_deepeval: types.ModuleType,
) -> None:
    score = DeepEvalEngine(FakeJudge(), model_name="judge-model").score(
        "faithfulness", _case({"expect_kb": True})
    )
    assert score is not None and score.value == 0.66
    metric = FakeMetric.last
    assert metric is not None
    assert type(metric).__name__ == "FaithfulnessMetric"
    assert metric.kwargs["async_mode"] is False
    assert isinstance(metric.kwargs["model"], FakeBaseLLM)
    assert metric.measured.retrieval_context == ["chunk alpha text"]


def test_contextual_precision_uses_the_reference_answer(
    fake_deepeval: types.ModuleType,
) -> None:
    DeepEvalEngine(FakeJudge()).score(
        "contextual_precision", _case({"reference_answer": "reference text"})
    )
    metric = FakeMetric.last
    assert type(metric).__name__ == "ContextualPrecisionMetric"
    assert metric.measured.expected_output == "reference text"


def test_answer_metrics_build_geval_with_the_declared_rules(
    fake_deepeval: types.ModuleType,
) -> None:
    DeepEvalEngine(FakeJudge()).score(
        "answer_quality",
        _case({"judge_rule": "must mention the budget", "key_points": ["budget"]}),
    )
    metric = FakeMetric.last
    assert type(metric).__name__ == "GEval"
    assert metric.kwargs["name"] == "answer_quality"
    assert any("must mention the budget" in step for step in metric.kwargs["evaluation_steps"])
    assert metric.kwargs["async_mode"] is False


def test_answer_correctness_uses_the_reference_answer(fake_deepeval: types.ModuleType) -> None:
    DeepEvalEngine(FakeJudge()).score(
        "answer_correctness", _case({"reference_answer": "reference answer"})
    )
    metric = FakeMetric.last
    assert type(metric).__name__ == "GEval"
    assert metric.measured.expected_output == "reference answer"
    assert FakeParams.EXPECTED_OUTPUT in metric.kwargs["evaluation_params"]


def test_skill_adherence_reads_the_rules_and_tool_log(fake_deepeval: types.ModuleType) -> None:
    evidence = _evidence(
        loaded_skills=["pdf-report"],
        retrieved_skills=["pdf-report"],
        business_tool_calls=[{"name": "mcp__a__export", "args": {"format": "pdf"}}],
    )
    DeepEvalEngine(FakeJudge()).score(
        "skill_adherence",
        _case(
            {"expected_skills": ["pdf-report"], "skill_rules": ["export a PDF"]},
            evidence=evidence,
        ),
    )
    metric = FakeMetric.last
    assert type(metric).__name__ == "GEval"
    assert any("export a PDF" in step for step in metric.kwargs["evaluation_steps"])
    assert "mcp__a__export" in metric.measured.actual_output


def test_tool_correctness_maps_called_and_expected_tools(
    fake_deepeval: types.ModuleType,
) -> None:
    evidence = _evidence(
        business_tool_calls=[{"name": "mcp__a__find", "args": {}}]
    )
    DeepEvalEngine(FakeJudge()).score(
        "tool_correctness",
        _case({"expected_tools": ["mcp__a__find"]}, evidence=evidence),
    )
    metric = FakeMetric.last
    assert type(metric).__name__ == "ToolCorrectnessMetric"
    assert [call.name for call in metric.measured.tools_called] == ["mcp__a__find"]
    assert [call.name for call in metric.measured.expected_tools] == ["mcp__a__find"]


def test_refusal_ok_builds_a_geval_without_a_reference(
    fake_deepeval: types.ModuleType,
) -> None:
    DeepEvalEngine(FakeJudge()).score(
        "refusal_ok", _case({"expect_refusal": True})
    )
    metric = FakeMetric.last
    assert type(metric).__name__ == "GEval"
    assert "no relevant information" in " ".join(metric.kwargs["evaluation_steps"])


def test_an_unknown_metric_is_unscorable(fake_deepeval: types.ModuleType) -> None:
    assert DeepEvalEngine(FakeJudge()).score("not_a_metric", _case({"expect_kb": True})) is None


def test_a_metric_without_a_numeric_score_raises_a_judge_error(
    fake_deepeval: types.ModuleType,
) -> None:
    class BrokenMetric(FakeMetric):
        score_value = None

        def measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
            self.measured = test_case
            return 0.0

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            sys.modules["deepeval.metrics"], "FaithfulnessMetric", BrokenMetric
        )
        with pytest.raises(JudgeError, match="score"):
            DeepEvalEngine(FakeJudge()).score("faithfulness", _case({"expect_kb": True}))
    finally:
        monkeypatch.undo()
