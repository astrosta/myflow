"""Optional DeepEval metric engine.

DeepEval is an optional extra.  Importing this module must not import it: the
package has to keep working with rule metrics only.  The engine therefore loads
DeepEval lazily, adapts the judge ChatModel to DeepEvalBaseLLM, and translates
each design metric into the matching DeepEval metric class.

The engine is deliberately a thin adapter, not a second metric implementation:
DeepEval owns the prompt decomposition and scoring, and this module only maps
evidence into LLMTestCase and reads back score/reason.  Any DeepEval failure is
reported as a JudgeError with the exception class, never a provider body.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from typing import Any

from myflow_evals.llm import ChatModel, JudgeError
from myflow_evals.llm_metrics import (
    LLMEngine,
    JudgeCase,
    JudgeScore,
    PromptJudgeEngine,
    refusal_short_circuit,
)

_ENGINE_PREFERENCES = ("auto", "prompt", "deepeval")
_DEFAULT_MODEL_NAME = "myflow-judge"
_JUDGE_SYSTEM = (
    "You are a strict evaluation judge. Follow the metric instructions exactly "
    "and return only the requested output."
)
_ANSWER_LIMIT = 8000


def deepeval_available() -> bool:
    """Whether the optional DeepEval package can be imported right now."""
    try:
        importlib.import_module("deepeval")
    except Exception:  # noqa: BLE001 - a missing/broken extra is just unavailable
        return False
    return True


def build_engine(
    judge: ChatModel | None,
    *,
    preference: str = "auto",
    model_name: str = _DEFAULT_MODEL_NAME,
) -> LLMEngine | None:
    """Choose the metric engine, or None when no judge is configured."""
    if preference not in _ENGINE_PREFERENCES:
        raise JudgeError(
            f"unknown metric engine preference {preference!r}; "
            f"expected one of {', '.join(_ENGINE_PREFERENCES)}"
        )
    if judge is None:
        return None
    if preference == "prompt":
        return PromptJudgeEngine(judge)
    if preference == "deepeval":
        if not deepeval_available():
            raise JudgeError(
                "the deepeval metric engine was requested but DeepEval is not "
                "installed; install the eval 'llm' extra or use --metric-engine prompt"
            )
        return DeepEvalEngine(judge, model_name=model_name)
    if deepeval_available():
        return DeepEvalEngine(judge, model_name=model_name)
    return PromptJudgeEngine(judge)


class DeepEvalEngine:
    """DeepEval metric adapter over one judge ChatModel."""

    def __init__(self, judge: ChatModel, *, model_name: str = _DEFAULT_MODEL_NAME) -> None:
        self._judge = judge
        self._model_name = model_name

    def score(self, metric: str, case: JudgeCase) -> JudgeScore | None:
        if metric == "refusal_ok":
            decided = refusal_short_circuit(case)
            if decided is not None:
                return decided
        handler = _METRIC_HANDLERS.get(metric)
        if handler is None:
            return None
        try:
            return handler(self._judge, self._model_name, case)
        except JudgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - a metric failure is evidence, not a crash
            raise JudgeError(
                f"DeepEval metric {metric} failed ({type(exc).__name__})"
            ) from None


_CHAT_CLASS: type | None = None
_CHAT_BASE: type | None = None


def _deep_eval_chat_class() -> type:
    """Build (once) the DeepEvalBaseLLM subclass after a successful import."""
    global _CHAT_CLASS, _CHAT_BASE
    try:
        models = importlib.import_module("deepeval.models")
    except Exception as exc:  # noqa: BLE001
        raise JudgeError(
            f"DeepEval is not importable ({type(exc).__name__})"
        ) from None
    base = models.DeepEvalBaseLLM
    if _CHAT_CLASS is not None and _CHAT_BASE is base:
        return _CHAT_CLASS

    class _DeepEvalChat(base):  # type: ignore[misc, valid-type]
        """A DeepEval model that forwards every prompt to our judge client."""

        def __init__(self, judge: ChatModel, model_name: str) -> None:
            self._judge = judge
            self._model_name = model_name
            super().__init__(model=model_name)

        def load_model(self, *args: Any, **kwargs: Any) -> "_DeepEvalChat":
            return self

        def generate(self, prompt: Any = "", *args: Any, **kwargs: Any) -> str:
            return self._judge.complete(system=_JUDGE_SYSTEM, prompt=str(prompt))

        async def a_generate(self, prompt: Any = "", *args: Any, **kwargs: Any) -> str:
            return self.generate(prompt, *args, **kwargs)

        def get_model_name(self, *args: Any, **kwargs: Any) -> str:
            return self._model_name

    _CHAT_CLASS = _DeepEvalChat
    _CHAT_BASE = base
    return _DeepEvalChat


def _chat_model(judge: ChatModel, model_name: str = _DEFAULT_MODEL_NAME) -> Any:
    """Instantiate the lazy DeepEval chat adapter for one judge."""
    return _deep_eval_chat_class()(judge, model_name)


def _score_with(
    metric_name: str,
    case: JudgeCase,
    *,
    metric_class: str,
    test_case: Any,
    model: Any,
    **kwargs: Any,
) -> JudgeScore:
    metrics = importlib.import_module("deepeval.metrics")
    factory = getattr(metrics, metric_class)
    metric = factory(model=model, async_mode=False, threshold=0.0, **kwargs)
    metric.measure(test_case)
    raw = getattr(metric, "score", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise JudgeError(f"DeepEval metric {metric_name} returned no numeric score")
    value = float(raw)
    if value != value or value < 0.0 or value > 1.0:
        raise JudgeError(f"DeepEval metric {metric_name} scored outside [0, 1]")
    reason = getattr(metric, "reason", None)
    comment = " ".join(str(reason).split())[:500] if reason else "no reason given"
    return JudgeScore(value=value, comment=comment)


def _test_case(
    case: JudgeCase,
    *,
    expected_output: str = "",
    actual_output: str | None = None,
    retrieval_context: list[str] | None = None,
    tools_called: list[Any] | None = None,
    expected_tools: list[Any] | None = None,
) -> Any:
    test_case_module = importlib.import_module("deepeval.test_case")
    payload: dict[str, Any] = {
        "input": case.query,
        "actual_output": actual_output or case.answer or "(no answer)",
        "expected_output": expected_output,
    }
    if retrieval_context is not None:
        payload["retrieval_context"] = retrieval_context
    if tools_called is not None:
        payload["tools_called"] = tools_called
    if expected_tools is not None:
        payload["expected_tools"] = expected_tools
    return test_case_module.LLMTestCase(**payload)


def _params() -> Any:
    test_case_module = importlib.import_module("deepeval.test_case")
    return getattr(test_case_module, "SingleTurnParams", None) or getattr(
        test_case_module, "LLMTestCaseParams"
    )


def _retrieval_context(case: JudgeCase) -> list[str]:
    return [
        text
        for chunk in case.evidence.retrieved_chunks
        if isinstance(chunk, Mapping)
        for text in [chunk.get("content")]
        if isinstance(text, str) and text.strip()
    ]


def _expected(case: JudgeCase) -> Mapping[str, Any]:
    return case.expected_output if isinstance(case.expected_output, Mapping) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _faithfulness(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    return _score_with(
        "faithfulness",
        case,
        metric_class="FaithfulnessMetric",
        test_case=_test_case(case, retrieval_context=_retrieval_context(case)),
        model=_chat_model(judge, model_name),
    )


def _answer_relevancy(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    return _score_with(
        "answer_relevancy",
        case,
        metric_class="AnswerRelevancyMetric",
        test_case=_test_case(case),
        model=_chat_model(judge, model_name),
    )


def _contextual_recall(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    expected = _expected(case)
    return _score_with(
        "contextual_recall",
        case,
        metric_class="ContextualRecallMetric",
        test_case=_test_case(
            case,
            expected_output=_text(expected.get("reference_answer")),
            retrieval_context=_retrieval_context(case),
        ),
        model=_chat_model(judge, model_name),
    )


def _contextual_precision(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    expected = _expected(case)
    return _score_with(
        "contextual_precision",
        case,
        metric_class="ContextualPrecisionMetric",
        test_case=_test_case(
            case,
            expected_output=_text(expected.get("reference_answer")),
            retrieval_context=_retrieval_context(case),
        ),
        model=_chat_model(judge, model_name),
    )


def _contextual_relevancy(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    return _score_with(
        "contextual_relevancy",
        case,
        metric_class="ContextualRelevancyMetric",
        test_case=_test_case(case, retrieval_context=_retrieval_context(case)),
        model=_chat_model(judge, model_name),
    )


def _answer_correctness(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    expected = _expected(case)
    params = _params()
    return _score_with(
        "answer_correctness",
        case,
        metric_class="GEval",
        test_case=_test_case(case, expected_output=_text(expected.get("reference_answer"))),
        model=_chat_model(judge, model_name),
        name="answer_correctness",
        evaluation_params=[
            params.INPUT,
            params.ACTUAL_OUTPUT,
            params.EXPECTED_OUTPUT,
        ],
        evaluation_steps=[
            "Compare the key facts and values in the actual output with the expected output.",
            "A different wording with the same meaning is correct.",
            "A wrong fact or a wrong number is a serious error.",
        ],
    )


def _answer_quality(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    expected = _expected(case)
    params = _params()
    steps = ["Judge whether the actual output answers the user request."]
    rule = _text(expected.get("judge_rule"))
    if rule:
        steps.append(f"Rule: {rule}")
    for point in _string_list(expected.get("key_points")):
        steps.append(f"The answer must include: {point}")
    steps.append("Penalize invented or off-topic content.")
    return _score_with(
        "answer_quality",
        case,
        metric_class="GEval",
        test_case=_test_case(case),
        model=_chat_model(judge, model_name),
        name="answer_quality",
        evaluation_params=[params.INPUT, params.ACTUAL_OUTPUT],
        evaluation_steps=steps,
    )


def _skill_adherence(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    expected = _expected(case)
    params = _params()
    rules = expected.get("skill_rules")
    if isinstance(rules, str):
        rule_list = [rules] if rules.strip() else []
    else:
        rule_list = _string_list(rules)
    skills = ", ".join(_string_list(expected.get("expected_skills")))
    calls = [
        call
        for call in case.evidence.business_tool_calls
        if isinstance(call, Mapping)
    ]
    tool_log = json.dumps(calls, ensure_ascii=False, default=str)
    actual_output = f"{case.answer}\n\n[tool calls]\n{tool_log}"
    steps = [f"Skill(s): {skills}"] if skills else []
    steps.extend(f"Skill requirement: {rule}" for rule in rule_list)
    steps.extend(
        [
            "Check the actual output and tool calls against every skill requirement.",
            "A requirement that is not met lowers the score.",
        ]
    )
    test_case = _test_case(case, actual_output=actual_output)
    return _score_with(
        "skill_adherence",
        case,
        metric_class="GEval",
        test_case=test_case,
        model=_chat_model(judge, model_name),
        name="skill_adherence",
        evaluation_params=[params.INPUT, params.ACTUAL_OUTPUT],
        evaluation_steps=steps,
    )


def _refusal_ok(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    params = _params()
    return _score_with(
        "refusal_ok",
        case,
        metric_class="GEval",
        test_case=_test_case(case),
        model=_chat_model(judge, model_name),
        name="refusal_ok",
        evaluation_params=[params.INPUT, params.ACTUAL_OUTPUT],
        evaluation_steps=[
            "The knowledge base contains no relevant information for this question.",
            "Decide whether the actual output clearly states that no relevant information was found.",
            "A specific unsupported answer is a failure.",
        ],
    )


def _tool_correctness(judge: ChatModel, model_name: str, case: JudgeCase) -> JudgeScore:
    test_case_module = importlib.import_module("deepeval.test_case")
    expected = _string_list(_expected(case).get("expected_tools"))
    called = [
        call.get("name")
        for call in case.evidence.business_tool_calls
        if isinstance(call, Mapping) and isinstance(call.get("name"), str)
    ]
    test_case = _test_case(
        case,
        tools_called=[test_case_module.ToolCall(name=name) for name in called],
        expected_tools=[test_case_module.ToolCall(name=name) for name in expected],
    )
    return _score_with(
        "tool_correctness",
        case,
        metric_class="ToolCorrectnessMetric",
        test_case=test_case,
        model=_chat_model(judge, model_name),
        should_exact_match=False,
        should_consider_ordering=False,
    )


_METRIC_HANDLERS = {
    "skill_adherence": _skill_adherence,
    "faithfulness": _faithfulness,
    "contextual_recall": _contextual_recall,
    "contextual_precision": _contextual_precision,
    "contextual_relevancy": _contextual_relevancy,
    "answer_quality": _answer_quality,
    "answer_correctness": _answer_correctness,
    "answer_relevancy": _answer_relevancy,
    "refusal_ok": _refusal_ok,
    "tool_correctness": _tool_correctness,
}
