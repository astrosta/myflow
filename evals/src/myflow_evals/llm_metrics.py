"""Prompt-based LLM metrics and the shared judge contract.

The rule evaluators decide everything that can be decided by names and ids.
This module covers what they cannot: whether an answer follows the retrieved
material, matches a reference, satisfies a judge rule, or respects a Skill's
steps.  It is deliberately engine-agnostic: a ChatModel is injected, and the
optional DeepEval engine can replace this one without changing the runner.

Applicability is decided before a judge is ever called.  A trace replay, a
no-answer sample, a truncated retrieval, or a sample with no declared judging
material returns None (unscorable) rather than asking a model to guess.  Every
score is a number in [0, 1] or a JudgeError -- never a silently clamped value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from myflow_evals.evidence import RunEvidence
from myflow_evals.llm import ChatModel, JudgeError

_SYSTEM_PROMPT = (
    "You are a strict evaluation judge for an AI assistant. "
    "Use only the material in the user message. "
    "Return one JSON object and nothing else."
)

#: The judge is shown at most this much of one retrieved chunk.
_CHUNK_LIMIT = 4000
#: The whole retrieved context is capped so a prompt cannot grow without bound.
_CONTEXT_LIMIT = 12000
_ANSWER_LIMIT = 8000
_REFERENCE_LIMIT = 4000
_REASON_LIMIT = 500

#: Clear no-information replies; these avoid a judge call for an obvious refusal.
_REFUSAL_MARKERS = (
    "没有找到",
    "未找到",
    "没有相关",
    "无相关信息",
    "没有提及",
    "未提及",
    "无法确定",
    "资料中没有",
    "文档中没有",
    "not found",
    "no relevant",
    "no information",
    "does not mention",
    "doesn't mention",
    "cannot find",
    "unable to find",
    "not available in",
    "no mention of",
)

#: The design's LLM metrics, in layered order.
LLM_METRICS: tuple[str, ...] = (
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
)

#: CI samples only these; the full-only metrics are skipped in ci mode.
CI_LLM_METRICS = frozenset(
    {"faithfulness", "answer_correctness", "skill_adherence", "refusal_ok"}
)
FULL_ONLY_LLM_METRICS = frozenset(
    {"contextual_precision", "contextual_relevancy", "answer_relevancy"}
)

#: Design thresholds from section 3, all LLM-kind; tool_correctness is record-only.
LLM_THRESHOLDS: dict[str, dict[str, Any]] = {
    "skill_adherence": {"value": 0.85, "comparison": "min", "kind": "llm"},
    "faithfulness": {"value": 0.90, "comparison": "min", "kind": "llm"},
    "contextual_recall": {"value": 0.80, "comparison": "min", "kind": "llm"},
    "contextual_precision": {"value": 0.70, "comparison": "min", "kind": "llm"},
    "contextual_relevancy": {"value": 0.70, "comparison": "min", "kind": "llm"},
    "answer_quality": {"value": 0.85, "comparison": "min", "kind": "llm"},
    "answer_correctness": {"value": 0.80, "comparison": "min", "kind": "llm"},
    "answer_relevancy": {"value": 0.85, "comparison": "min", "kind": "llm"},
    "refusal_ok": {"value": 0.90, "comparison": "min", "kind": "llm"},
}


@dataclass(frozen=True)
class JudgeCase:
    """One scored sample, already normalized into evidence."""

    item_id: str
    query: str
    answer: str
    expected_output: Mapping[str, Any] | None
    evidence: RunEvidence
    metadata: Mapping[str, Any] | None


@dataclass(frozen=True)
class JudgeScore:
    """One metric result: a number in [0, 1] plus a short reason."""

    value: float
    comment: str


class LLMEngine(Protocol):
    """Any engine that can score one metric for one case."""

    def score(self, metric: str, case: JudgeCase) -> JudgeScore | None: ...


class PromptJudgeEngine:
    """Score metrics with one JSON-returning judge call per metric."""

    def __init__(self, judge: ChatModel) -> None:
        self._judge = judge

    def score(self, metric: str, case: JudgeCase) -> JudgeScore | None:
        handler = _PROMPT_METRICS.get(metric)
        if handler is None:
            return None
        return handler(self._judge, case)


def metric_applicable(metric: str, case: JudgeCase) -> bool:
    """Whether this sample can be scored on this metric at all."""
    expected = case.expected_output
    if not isinstance(expected, Mapping) or not expected:
        return False
    evidence = case.evidence
    answer = case.answer.strip()
    refusal = _declared_true(expected, "expect_refusal")
    chunks = _chunk_rows(evidence)
    reference = _text(expected.get("reference_answer"))

    if metric == "faithfulness":
        return (
            _declared_true(expected, "expect_kb")
            and not refusal
            and bool(answer)
            and bool(chunks)
            and not evidence.kb_truncated
        )
    if metric == "answer_correctness":
        return bool(reference) and not refusal and bool(answer)
    if metric == "contextual_recall":
        return bool(reference) and not refusal and bool(chunks)
    if metric == "contextual_precision":
        return bool(reference) and not refusal and bool(chunks)
    if metric == "contextual_relevancy":
        return not refusal and bool(chunks)
    if metric == "answer_relevancy":
        return bool(answer)
    if metric == "answer_quality":
        return bool(answer) and bool(_judging_material(expected))
    if metric == "skill_adherence":
        return (
            bool(answer)
            and bool(_skill_rules(expected))
            and bool(_string_list(expected, "expected_skills"))
            and bool(evidence.loaded_skills)
        )
    if metric == "refusal_ok":
        return refusal
    if metric == "tool_correctness":
        return bool(_string_list(expected, "expected_tools"))
    return False


def _prompt_faithfulness(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Retrieved context:\n{_context_text(case)}",
        f"Answer:\n{_clip(case.answer, _ANSWER_LIMIT)}",
        "Task: extract the answer's factual claims and decide what fraction the "
        "retrieved context supports. Unsupported or invented claims lower the score.",
        'Return {"score": <0..1>, "reason": "<short reason>"}.',
    )
    return _ask_for_score(judge, prompt)


def _prompt_answer_correctness(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    expected = case.expected_output or {}
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Reference answer:\n{_clip(_text(expected.get('reference_answer')) or '', _REFERENCE_LIMIT)}",
        f"Answer:\n{_clip(case.answer, _ANSWER_LIMIT)}",
        "Task: compare the key facts and values in the answer with the reference. "
        "A different wording with the same meaning is correct; a wrong fact or "
        "number is a serious error.",
        'Return {"score": <0..1>, "reason": "<short reason>"}.',
    )
    return _ask_for_score(judge, prompt)


def _prompt_answer_quality(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    expected = case.expected_output or {}
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Answer:\n{_clip(case.answer, _ANSWER_LIMIT)}",
        "Judging material:\n" + _judging_material(expected),
        "Task: check whether the answer satisfies every declared rule or key point. "
        "Penalize invented or off-topic content.",
        'Return {"score": <0..1>, "reason": "<short reason>"}.',
    )
    return _ask_for_score(judge, prompt)


def _prompt_answer_relevancy(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Answer:\n{_clip(case.answer, _ANSWER_LIMIT)}",
        "Task: decide how directly the answer addresses the question. "
        "A partly relevant answer gets a partial score; unrelated text gets zero.",
        'Return {"score": <0..1>, "reason": "<short reason>"}.',
    )
    return _ask_for_score(judge, prompt)


def _prompt_contextual_recall(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    expected = case.expected_output or {}
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Reference answer:\n{_clip(_text(expected.get('reference_answer')) or '', _REFERENCE_LIMIT)}",
        f"Retrieved context:\n{_context_text(case)}",
        "Task: decide what fraction of the reference answer's information is "
        "present in the retrieved context.",
        'Return {"score": <0..1>, "reason": "<short reason>"}.',
    )
    return _ask_for_score(judge, prompt)


def _prompt_contextual_precision(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    expected = case.expected_output or {}
    chunks = _chunk_rows(case.evidence)
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Reference answer:\n{_clip(_text(expected.get('reference_answer')) or '', _REFERENCE_LIMIT)}",
        f"Retrieved chunks (ranked):\n{_context_text(case)}",
        "Task: for each retrieved chunk in order, decide whether it is relevant "
        "to answering the question with the reference answer. Return one boolean "
        f"per chunk ({len(chunks)} values), in the same order.",
        'Return {"relevant": [true|false, ...], "reason": "<short reason>"}.',
    )
    payload = _ask_for_json(judge, prompt)
    verdicts = _bool_list(payload.get("relevant"))
    if len(verdicts) != len(chunks):
        raise JudgeError(
            f"judge returned {len(verdicts)} relevance verdicts for {len(chunks)} chunks"
        )
    precision = _ranked_precision(verdicts)
    return JudgeScore(
        value=precision,
        comment=_reason(payload) or "ranked relevance verdicts",
    )


def _prompt_contextual_relevancy(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    chunks = _chunk_rows(case.evidence)
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Retrieved chunks:\n{_context_text(case)}",
        "Task: for each retrieved chunk, decide whether it contains information "
        f"relevant to the question. Return one boolean per chunk ({len(chunks)} values).",
        'Return {"relevant": [true|false, ...], "reason": "<short reason>"}.',
    )
    payload = _ask_for_json(judge, prompt)
    verdicts = _bool_list(payload.get("relevant"))
    if len(verdicts) != len(chunks):
        raise JudgeError(
            f"judge returned {len(verdicts)} relevance verdicts for {len(chunks)} chunks"
        )
    relevant = sum(1 for verdict in verdicts if verdict)
    return JudgeScore(
        value=relevant / len(verdicts),
        comment=_reason(payload) or f"{relevant}/{len(verdicts)} chunks relevant",
    )


def _prompt_skill_adherence(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    expected = case.expected_output or {}
    rules = _skill_rules(expected)
    skills = ", ".join(_string_list(expected, "expected_skills"))
    prompt = _join_sections(
        f"User request:\n{case.query}",
        f"Skill(s) used: {skills}",
        "Skill requirements:\n" + "\n".join(f"- {rule}" for rule in rules),
        f"Answer:\n{_clip(case.answer, _ANSWER_LIMIT)}",
        f"Tool calls:\n{_tool_call_text(case.evidence)}",
        "Task: for each skill requirement, decide whether the execution satisfied "
        "it. Return one object per requirement, in the same order.",
        'Return {"rules": [{"rule": "<requirement>", "passed": true|false}, ...], '
        '"reason": "<short reason>"}.',
    )
    payload = _ask_for_json(judge, prompt)
    verdicts = _rule_verdicts(payload.get("rules"))
    if len(verdicts) != len(rules):
        raise JudgeError(
            f"judge returned {len(verdicts)} rule verdicts for {len(rules)} requirements"
        )
    passed = sum(1 for verdict in verdicts if verdict)
    return JudgeScore(
        value=passed / len(verdicts),
        comment=_reason(payload) or f"{passed}/{len(verdicts)} skill rules satisfied",
    )


def _prompt_refusal_ok(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    answer = case.answer.strip()
    if _looks_like_refusal(answer):
        return JudgeScore(value=1.0, comment="the answer clearly states no information was found")
    if case.evidence.citations:
        return JudgeScore(value=0.0, comment="the answer cited sources instead of refusing")
    prompt = _join_sections(
        f"Question:\n{case.query}",
        f"Answer:\n{_clip(case.answer, _ANSWER_LIMIT)}",
        "The knowledge base does not contain the answer. Decide whether the answer "
        "clearly says that no relevant information was found, rather than giving a "
        "specific unsupported answer.",
        'Return {"score": <0..1>, "reason": "<short reason>"}.',
    )
    return _ask_for_score(judge, prompt)


def _tool_correctness(judge: ChatModel, case: JudgeCase) -> JudgeScore:
    del judge  # deterministic set comparison, no model needed
    expected = set(_string_list(case.expected_output, "expected_tools"))
    called = {
        call.get("name")
        for call in case.evidence.business_tool_calls
        if isinstance(call, Mapping) and isinstance(call.get("name"), str)
    }
    if not expected:
        return JudgeScore(value=0.0, comment="no expected tool was declared")
    matched = len(expected & called)
    missing = sorted(expected - called)
    score = matched / len(expected)
    comment = f"{matched}/{len(expected)} expected tools called"
    if missing:
        comment += f"; missing: {', '.join(missing)}"
    return JudgeScore(value=score, comment=comment)


_PROMPT_METRICS: dict[str, Callable[[ChatModel, JudgeCase], JudgeScore]] = {
    "skill_adherence": _prompt_skill_adherence,
    "faithfulness": _prompt_faithfulness,
    "contextual_recall": _prompt_contextual_recall,
    "contextual_precision": _prompt_contextual_precision,
    "contextual_relevancy": _prompt_contextual_relevancy,
    "answer_quality": _prompt_answer_quality,
    "answer_correctness": _prompt_answer_correctness,
    "answer_relevancy": _prompt_answer_relevancy,
    "refusal_ok": _prompt_refusal_ok,
    "tool_correctness": _tool_correctness,
}


def _ask_for_score(judge: ChatModel, prompt: str) -> JudgeScore:
    payload = _ask_for_json(judge, prompt)
    raw = payload.get("score")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise JudgeError("judge reply has no numeric score")
    value = float(raw)
    if value != value or value < 0.0 or value > 1.0:
        raise JudgeError("judge score is outside [0, 1]")
    return JudgeScore(value=value, comment=_reason(payload) or "no reason given")


def _ask_for_json(judge: ChatModel, prompt: str) -> Mapping[str, Any]:
    reply = judge.complete(system=_SYSTEM_PROMPT, prompt=prompt)
    payload = _parse_json_object(reply)
    if payload is None:
        raise JudgeError("judge did not return a JSON object")
    return payload


def _parse_json_object(text: str) -> Mapping[str, Any] | None:
    candidate = text.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, Mapping) else None


def _context_text(case: JudgeCase) -> str:
    lines: list[str] = []
    used = 0
    for rank, chunk in enumerate(_chunk_rows(case.evidence), start=1):
        content = _clip(_text(chunk.get("content")) or "", _CHUNK_LIMIT)
        if not content:
            continue
        header = f"[{rank}] chunk={_text(chunk.get('chunk_id')) or '?'}"
        block = f"{header}\n{content}"
        if used + len(block) > _CONTEXT_LIMIT:
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines) if lines else "(no retrieved context)"


def _chunk_rows(evidence: RunEvidence) -> list[Mapping[str, Any]]:
    return [chunk for chunk in evidence.retrieved_chunks if isinstance(chunk, Mapping)]


def _tool_call_text(evidence: RunEvidence) -> str:
    calls = [
        call for call in evidence.business_tool_calls if isinstance(call, Mapping)
    ]
    if not calls:
        return "(no business tool calls)"
    return "\n".join(
        f"- {call.get('name')}: {_clip(json.dumps(call.get('args') or {}, ensure_ascii=False, default=str), 500)}"
        for call in calls
    )


def _judging_material(expected: Mapping[str, Any]) -> str:
    parts: list[str] = []
    rule = _text(expected.get("judge_rule"))
    if rule:
        parts.append(f"Rule: {rule}")
    points = _string_list(expected, "key_points")
    if points:
        parts.append("Key points:\n" + "\n".join(f"- {point}" for point in points))
    return "\n".join(parts)


def _skill_rules(expected: Mapping[str, Any]) -> list[str]:
    value = expected.get("skill_rules")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _string_list(expected: Any, key: str) -> list[str]:
    if not isinstance(expected, Mapping):
        return []
    value = expected.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _declared_true(expected: Mapping[str, Any], key: str) -> bool:
    return expected.get(key) is True


def _bool_list(value: Any) -> list[bool]:
    if not isinstance(value, list) or any(not isinstance(item, bool) for item in value):
        raise JudgeError("judge reply has no boolean list")
    return value


def _rule_verdicts(value: Any) -> list[bool]:
    if not isinstance(value, list):
        raise JudgeError("judge reply has no rule list")
    verdicts: list[bool] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("passed"), bool):
            raise JudgeError("judge rule verdict is malformed")
        verdicts.append(item["passed"])
    return verdicts


def _ranked_precision(verdicts: Sequence[bool]) -> float:
    """Average precision at each relevant rank (the standard contextual metric)."""
    total = 0.0
    relevant = 0
    for rank, verdict in enumerate(verdicts, start=1):
        if not verdict:
            continue
        relevant += 1
        total += relevant / rank
    return total / relevant if relevant else 0.0


def _reason(payload: Mapping[str, Any]) -> str:
    raw = payload.get("reason")
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:_REASON_LIMIT]


def _looks_like_refusal(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(truncated)"


def _join_sections(*sections: str) -> str:
    return "\n\n".join(section for section in sections if section)
