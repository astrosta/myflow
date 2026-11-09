"""Pure per-sample rule metrics for one finished run.

Each function here turns a dataset item's ``expected_output`` plus the run's
``RunEvidence`` into at most one Langfuse ``Evaluation``: no I/O, no clock, no
prints, no randomness, and no dataset-level aggregation -- the design's rule
layer scores a single sample, and the experiment runner decides what to gate.

Scoring is layered on purpose. K0/R0 judge *routing only*: they read the
``task`` call's ``subagent_type``, never the subagent's internals, so an internal
``knowledge_search`` cannot pass R0. K1/R1 judge the retrieval evidence those
subagents produced, K2 judges what was actually loaded, and T2/T3 judge business
tool calls with ``tool_execute`` already unwrapped by ``evidence``.

``None`` reports "this sample cannot be scored on this metric"; ``0.0`` reports
"it was measured and failed". A sample that never reached the expected subagent
is not also punished for the retrieval it never had -- K0/R0 already scores that
miss. Malformed expected output or partial evidence returns ``None`` instead of
raising: one bad sample must not kill a 300-sample experiment. Nothing here keys
on a user id, permission, isolation, or memory effect.

Every evaluator takes the item's ``metadata`` keyword so the runner can call
them all the same way; only ``no_tool_ok`` reads it, and only to require
``metadata["category"] == "no_tool"``. Without that explicit intent it abstains,
so a trace replay -- which declares no expectations at all -- never records a
meaningless boundary score.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langfuse import Evaluation

from myflow_evals.evidence import RunEvidence

#: The two retrieval subagents the design routes through (K0 and R0).
_SKILL_SUBAGENT = "skill-finder"
_KB_SUBAGENT = "knowledge-researcher"

#: Design-doc T1/K1/R1 read the first five retrieval results.
_TOP_K = 5

#: Retrieval entry points. An attempt counts as use even when it returns nothing.
_RETRIEVAL_TOOLS = frozenset({"tool_search", "search_skills", "knowledge_search"})

#: The one category whose items ask for no tool use at all.
_NO_TOOL_CATEGORY = "no_tool"


def retrieval_recall_at_5(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """T1: share of expected tools inside the first five retrieved tools."""
    expected = _string_list(expected_output, "expected_tools")
    if not expected:
        return None
    top = list(evidence.retrieved_tools[:_TOP_K])
    hits = sum(1 for name in expected if name in top)
    return Evaluation(
        name="retrieval_recall@5",
        value=hits / len(expected),
        comment=f"{hits}/{len(expected)} expected tools in the top 5",
    )


def retrieval_mrr(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """T1: reciprocal rank of the first expected tool that was retrieved."""
    expected = _string_list(expected_output, "expected_tools")
    if not expected:
        return None
    rank = _first_rank(evidence.retrieved_tools, expected)
    if rank is None:
        return Evaluation(
            name="retrieval_mrr", value=0.0, comment="no expected tool was retrieved"
        )
    return Evaluation(
        name="retrieval_mrr",
        value=1.0 / rank,
        comment=f"first expected tool at rank {rank}",
    )


def tool_hit(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """T2: 1.0 when every expected tool was actually called."""
    expected = _string_list(expected_output, "expected_tools")
    if not expected:
        return None
    called = _business_call_names(evidence)
    missing = [name for name in expected if name not in called]
    if missing:
        return Evaluation(
            name="tool_hit", value=0.0, comment=f"not called: {', '.join(missing)}"
        )
    return Evaluation(
        name="tool_hit", value=1.0, comment=f"called: {', '.join(expected)}"
    )


def args_accuracy(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """T2: share of declared check keys whose value matched on that tool."""
    matched = 0
    total = 0
    for expected_call in _mapping_list(expected_output, "expected_calls"):
        tool = expected_call.get("tool")
        check_keys = expected_call.get("check_keys")
        expected_args = expected_call.get("args")
        if not isinstance(tool, str) or not tool or not isinstance(check_keys, list):
            continue
        if not isinstance(expected_args, Mapping):
            expected_args = {}
        calls = _business_args_for(evidence, tool)
        for key in check_keys:
            if not isinstance(key, str) or not key:
                continue
            total += 1
            if key not in expected_args:
                continue  # a key with no declared value cannot be matched
            declared = expected_args[key]
            if any(key in args and args[key] == declared for args in calls):
                matched += 1
    if total == 0:
        return None
    return Evaluation(
        name="args_accuracy",
        value=matched / total,
        comment=f"{matched}/{total} check keys matched",
    )


def extra_calls_ratio(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """T3: business tool calls per expected call (infrastructure already excluded)."""
    expected_calls = _mapping_list(expected_output, "expected_calls")
    if not expected_calls:
        return None
    actual = len(_business_call_names(evidence))
    return Evaluation(
        name="extra_calls_ratio",
        value=actual / len(expected_calls),
        comment=f"{actual} business calls for {len(expected_calls)} expected",
    )


def skill_routing(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """K0: whether the run delegated to ``skill-finder``, routing only."""
    expected = _declared_bool(expected_output, "expect_skill")
    if expected is None:
        return None
    routed, started = _subagent_routing(evidence, _SKILL_SUBAGENT)
    return Evaluation(
        name="skill_routing",
        value=1.0 if routed == expected else 0.0,
        comment=f"expect_skill={_lower(expected)}; {_routing_text(routed, started)}",
    )


def skill_recall_at_5(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """K1: share of expected skills inside skill-finder's top five results."""
    expected = _string_list(expected_output, "expected_skills")
    if not expected:
        return None
    if not _subagent_routing(evidence, _SKILL_SUBAGENT)[0]:
        return None
    top = list(evidence.retrieved_skills[:_TOP_K])
    hits = sum(1 for name in expected if name in top)
    return Evaluation(
        name="skill_recall@5",
        value=hits / len(expected),
        comment=f"{hits}/{len(expected)} expected skills in the top 5",
    )


def skill_mrr(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """K1: reciprocal rank of the first expected skill that was retrieved."""
    expected = _string_list(expected_output, "expected_skills")
    if not expected:
        return None
    if not _subagent_routing(evidence, _SKILL_SUBAGENT)[0]:
        return None
    rank = _first_rank(evidence.retrieved_skills, expected)
    if rank is None:
        return Evaluation(
            name="skill_mrr", value=0.0, comment="no expected skill was retrieved"
        )
    return Evaluation(
        name="skill_mrr",
        value=1.0 / rank,
        comment=f"first expected skill at rank {rank}",
    )


def skill_hit(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """K2: every expected skill was loaded, and only retrieved skills were loaded."""
    expected = _string_list(expected_output, "expected_skills")
    if not expected:
        return None
    loaded = [name for name in evidence.loaded_skills if isinstance(name, str)]
    retrieved = {name for name in evidence.retrieved_skills if isinstance(name, str)}
    missing = [name for name in expected if name not in loaded]
    unretrieved = [name for name in loaded if name not in retrieved]
    reasons: list[str] = []
    if missing:
        reasons.append(f"not loaded: {', '.join(missing)}")
    if unretrieved:
        reasons.append(f"loaded without retrieval: {', '.join(unretrieved)}")
    if reasons:
        return Evaluation(name="skill_hit", value=0.0, comment="; ".join(reasons))
    return Evaluation(
        name="skill_hit",
        value=1.0,
        comment=f"loaded as retrieved: {', '.join(expected)}",
    )


def skill_extra(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """K2: how many loaded skills the sample did not ask for."""
    expected = _string_list(expected_output, "expected_skills")
    if not expected:
        return None
    extras = [
        name
        for name in evidence.loaded_skills
        if isinstance(name, str) and name not in expected
    ]
    comment = (
        f"loaded beyond expectation: {', '.join(extras)}"
        if extras
        else "no extra skill was loaded"
    )
    return Evaluation(name="skill_extra", value=float(len(extras)), comment=comment)


def kb_routing(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """R0: whether the run delegated to ``knowledge-researcher``, routing only."""
    expected = _declared_bool(expected_output, "expect_kb")
    if expected is None:
        return None
    routed, started = _subagent_routing(evidence, _KB_SUBAGENT)
    return Evaluation(
        name="kb_routing",
        value=1.0 if routed == expected else 0.0,
        comment=f"expect_kb={_lower(expected)}; {_routing_text(routed, started)}",
    )


def kb_recall_at_5(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """R1: share of expected document ids inside the first five retrieved chunks."""
    expected = _string_list(expected_output, "expected_doc_ids")
    if not expected:
        return None
    if not _subagent_routing(evidence, _KB_SUBAGENT)[0]:
        return None
    top = _chunk_document_ids(evidence.retrieved_chunks)[:_TOP_K]
    hits = sum(1 for document_id in expected if document_id in top)
    return Evaluation(
        name="kb_recall@5",
        value=hits / len(expected),
        comment=f"{hits}/{len(expected)} expected documents in the top 5 chunks",
    )


def kb_mrr(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """R1: reciprocal rank of the first retrieved chunk from an expected document."""
    expected = _string_list(expected_output, "expected_doc_ids")
    if not expected:
        return None
    if not _subagent_routing(evidence, _KB_SUBAGENT)[0]:
        return None
    rank = _first_rank(_chunk_document_ids(evidence.retrieved_chunks), expected)
    if rank is None:
        return Evaluation(
            name="kb_mrr", value=0.0, comment="no expected document was retrieved"
        )
    return Evaluation(
        name="kb_mrr",
        value=1.0 / rank,
        comment=f"first expected document at rank {rank}",
    )


def citation_valid(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """R3: every citation names a (citation_id, chunk_id) pair this run retrieved."""
    citations = [
        citation for citation in evidence.citations if isinstance(citation, Mapping)
    ]
    if not citations:
        if evidence.citations:
            return None  # unreadable citation rows: unscorable, not "cited nothing"
        if _declared_bool(expected_output, "expect_kb") is True:
            return Evaluation(
                name="citation_valid",
                value=0.0,
                comment="knowledge was expected but the answer cited nothing",
            )
        return None
    retrieved = {
        (chunk.get("citation_id"), chunk.get("chunk_id"))
        for chunk in evidence.retrieved_chunks
        if isinstance(chunk, Mapping)
    }
    unknown = [
        _citation_label(citation)
        for citation in citations
        if (citation.get("citation_id"), citation.get("chunk_id")) not in retrieved
    ]
    if unknown:
        return Evaluation(
            name="citation_valid",
            value=0.0,
            comment=f"citation not retrieved: {', '.join(unknown)}",
        )
    return Evaluation(
        name="citation_valid",
        value=1.0,
        comment=f"all {len(citations)} citations were retrieved",
    )


def forbidden_violation(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """S1: 1.0 when a forbidden business tool was called (1.0 means violation)."""
    forbidden = _string_list(expected_output, "forbidden_tools")
    if not forbidden:
        return None
    called = set(_business_call_names(evidence))
    violated = sorted({name for name in forbidden if name in called})
    if violated:
        return Evaluation(
            name="forbidden_violation",
            value=1.0,
            comment=f"called forbidden tools: {', '.join(violated)}",
        )
    return Evaluation(
        name="forbidden_violation", value=0.0, comment="no forbidden tool was called"
    )


def no_tool_ok(
    *,
    expected_output: Mapping[str, Any] | None,
    evidence: RunEvidence,
    metadata: Mapping[str, Any] | None = None,
) -> Evaluation | None:
    """S1: an item that explicitly asks for no tool use kept the run clean."""
    if not isinstance(expected_output, Mapping):
        return None  # a missing expected output is malformed, not a declaration
    if not _no_tool_sample(metadata):
        return None
    reasons: list[str] = []
    business = _business_call_names(evidence)
    if business:
        reasons.append(f"called {len(business)} business tools")
    loaded = [name for name in evidence.loaded_skills if isinstance(name, str)]
    if loaded:
        reasons.append(f"loaded skills: {', '.join(loaded)}")
    attempts = _retrieval_attempts(evidence)
    if attempts:
        reasons.append(f"attempted retrieval: {', '.join(attempts)}")
    if evidence.kb_called or evidence.retrieved_tools or evidence.retrieved_skills:
        reasons.append("retrieval returned results")
    if reasons:
        return Evaluation(name="no_tool_ok", value=0.0, comment="; ".join(reasons))
    return Evaluation(
        name="no_tool_ok",
        value=1.0,
        comment="no tool, skill, or retrieval was used",
    )


#: The design's score names map to the rule functions that produce them.
RULE_EVALUATORS: dict[str, Callable[..., Evaluation | None]] = {
    "retrieval_recall@5": retrieval_recall_at_5,
    "retrieval_mrr": retrieval_mrr,
    "tool_hit": tool_hit,
    "args_accuracy": args_accuracy,
    "extra_calls_ratio": extra_calls_ratio,
    "skill_routing": skill_routing,
    "skill_recall@5": skill_recall_at_5,
    "skill_mrr": skill_mrr,
    "skill_hit": skill_hit,
    "skill_extra": skill_extra,
    "kb_routing": kb_routing,
    "kb_recall@5": kb_recall_at_5,
    "kb_mrr": kb_mrr,
    "citation_valid": citation_valid,
    "forbidden_violation": forbidden_violation,
    "no_tool_ok": no_tool_ok,
}

#: The design's rule gates; ``extra_calls_ratio`` and ``forbidden_violation`` are the
#: upper bounds (a violation must never happen, so its gate is 0.0).
#: MRR, ``skill_extra``, and ``no_tool_ok`` are recorded but not gated here -- the
#: design states no numeric gate for them.
THRESHOLDS: dict[str, dict[str, Any]] = {
    "retrieval_recall@5": {"value": 0.95, "comparison": "min", "kind": "rule"},
    "tool_hit": {"value": 0.90, "comparison": "min", "kind": "rule"},
    "args_accuracy": {"value": 0.85, "comparison": "min", "kind": "rule"},
    "extra_calls_ratio": {"value": 1.5, "comparison": "max", "kind": "rule"},
    "skill_routing": {"value": 0.95, "comparison": "min", "kind": "rule"},
    "skill_recall@5": {"value": 0.95, "comparison": "min", "kind": "rule"},
    "skill_hit": {"value": 0.90, "comparison": "min", "kind": "rule"},
    "kb_routing": {"value": 0.95, "comparison": "min", "kind": "rule"},
    "kb_recall@5": {"value": 0.85, "comparison": "min", "kind": "rule"},
    "citation_valid": {"value": 0.95, "comparison": "min", "kind": "rule"},
    "forbidden_violation": {"value": 0.0, "comparison": "max", "kind": "rule"},
}


def _string_list(expected_output: Any, key: str) -> list[str]:
    """Read a declared list of names; anything malformed counts as "not declared"."""
    if not isinstance(expected_output, Mapping):
        return []
    value = expected_output.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _mapping_list(expected_output: Any, key: str) -> list[Mapping[str, Any]]:
    """Read a declared list of objects, dropping rows that are not mappings."""
    if not isinstance(expected_output, Mapping):
        return []
    value = expected_output.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _declared_bool(expected_output: Any, key: str) -> bool | None:
    """Read a declared flag; anything but a real bool means "not declared"."""
    if not isinstance(expected_output, Mapping):
        return None
    value = expected_output.get(key)
    return value if isinstance(value, bool) else None


def _no_tool_sample(metadata: Any) -> bool:
    """Only an item that declares ``category == "no_tool"`` can be scored here."""
    if not isinstance(metadata, Mapping):
        return False
    return metadata.get("category") == _NO_TOOL_CATEGORY


def _subagent_routing(evidence: RunEvidence, subagent_type: str) -> tuple[bool, bool]:
    """Return ``(routed, started)`` for one delegated subagent type."""
    entries = [
        entry
        for entry in evidence.subagent_calls
        if isinstance(entry, Mapping) and entry.get("subagent_type") == subagent_type
    ]
    if not entries:
        return False, False
    return True, any(entry.get("started") is True for entry in entries)


def _routing_text(routed: bool, started: bool) -> str:
    if not routed:
        return "not routed"
    return "routed+started" if started else "routed, no subagent.start"


def _call_names(calls: Sequence[Any]) -> list[str]:
    names: list[str] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        name = call.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _business_call_names(evidence: RunEvidence) -> list[str]:
    return _call_names(evidence.business_tool_calls)


def _retrieval_attempts(evidence: RunEvidence) -> list[str]:
    """Retrieval tools the run called, whether or not they returned anything."""
    return sorted(
        {name for name in _call_names(evidence.tool_calls) if name in _RETRIEVAL_TOOLS}
    )


def _business_args_for(evidence: RunEvidence, tool: str) -> list[Mapping[str, Any]]:
    return [
        call["args"]
        for call in evidence.business_tool_calls
        if isinstance(call, Mapping)
        and call.get("name") == tool
        and isinstance(call.get("args"), Mapping)
    ]


def _citation_label(citation: Mapping[str, Any]) -> str:
    """Name both halves of a citation pair so a forged id is identifiable."""
    citation_id = citation.get("citation_id")
    chunk_id = citation.get("chunk_id")
    label = "/".join(
        str(value)
        for value in (citation_id, chunk_id)
        if isinstance(value, str) and value
    )
    return label or "citation without ids"


def _chunk_document_ids(chunks: Sequence[Any]) -> list[str | None]:
    """One entry per chunk, so ranks stay truthful when a row is unreadable."""
    document_ids: list[str | None] = []
    for chunk in chunks:
        document_id = chunk.get("document_id") if isinstance(chunk, Mapping) else None
        document_ids.append(
            document_id if isinstance(document_id, str) and document_id else None
        )
    return document_ids


def _first_rank(names: Sequence[Any], expected: Sequence[str]) -> int | None:
    """1-based rank of the first expected name in ``names``, or ``None``."""
    wanted = set(expected)
    for rank, name in enumerate(names, start=1):
        if isinstance(name, str) and name in wanted:
            return rank
    return None


def _lower(value: bool) -> str:
    return "true" if value else "false"
