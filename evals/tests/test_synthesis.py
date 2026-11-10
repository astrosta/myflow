"""Deterministic expanded-synthesis tests.

The synthesizer is pure: every fixture here is an in-memory BuildSources with the
same shapes the Gateway loader returns. No account credentials, HTTP client, real
judge, or embedding service participates, so a build is reproducible and the
tests prove that a writer/embedder is optional rather than assumed.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import httpx
import pytest

from myflow_evals.accounts import AccountCredential, AccountSession
from myflow_evals.build_dataset import (
    BuildSources,
    DatasetItemDraft,
    KnowledgeSource,
    McpTool,
    SkillSource,
    TraceSource,
)
from myflow_evals.synthesis import (
    BOUNDARY_QUESTIONS,
    MAX_UNANSWERABLE_PER_ACCOUNT,
    OpenAIQuestionWriter,
    QualityReport,
    SynthesisPlan,
    dataset_fingerprint,
    dedupe_drafts,
    quality_report,
    synthesize_account_items,
)

HASHED_USER_ID = "9f2c4b6d8e0a1c3e5f7a9b1d3c5e7f90"
RAW_USER_ID = "11111111-1111-4111-8111-111111111111"
RAW_USERNAME = "alice"
RAW_PASSWORD = "alice-password"

SEARCH_SCHEMA = {
    "type": "object",
    "required": ["sector"],
    "properties": {"sector": {"type": "string", "enum": ["solar", "wind"]}},
}
CREATE_SCHEMA = {
    "type": "object",
    "required": ["name", "sector"],
    "properties": {"name": {"type": "string"}, "sector": {"type": "string"}},
}
GET_SCHEMA = {
    "type": "object",
    "required": ["project_id"],
    "properties": {"project_id": {"type": "string"}},
}
DELETE_SCHEMA = {
    "type": "object",
    "required": ["project_id"],
    "properties": {"project_id": {"type": "string"}},
}
FIND_SCHEMA = {
    "type": "object",
    "required": ["email"],
    "properties": {"email": {"type": "string"}},
}
UPDATE_SCHEMA = {
    "type": "object",
    "required": ["customer_id"],
    "properties": {"customer_id": {"type": "string"}},
}

SEARCH = McpTool(
    call_name="mcp__project_db__search_projects",
    name="search_projects",
    description="Search the project database for active solar projects.",
    input_schema=SEARCH_SCHEMA,
)
CREATE = McpTool(
    call_name="mcp__project_db__create_project",
    name="create_project",
    description="Create a project record in the project database.",
    input_schema=CREATE_SCHEMA,
)
DELETE = McpTool(
    call_name="mcp__project_db__delete_project",
    name="delete_project",
    description="Delete a project record permanently.",
    input_schema=DELETE_SCHEMA,
)
FIND = McpTool(
    call_name="mcp__crm__find_customer",
    name="find_customer",
    description="Search the project customer directory by email address.",
    input_schema=FIND_SCHEMA,
)
UPDATE = McpTool(
    call_name="mcp__crm__update_customer",
    name="update_customer",
    description="Update a customer record in the customer directory.",
    input_schema=UPDATE_SCHEMA,
)
LIST = McpTool(
    call_name="mcp__project_db__list_sectors",
    name="list_sectors",
    description="List the project sectors.",
    input_schema={"type": "object", "properties": {}},
)

TOOLS = [SEARCH, CREATE, DELETE, FIND, UPDATE, LIST]

SKILL_WITH_TOOL = SkillSource(
    skill_id="skill-1",
    name="daily-project-brief",
    description=(
        "Turn a project story into a numbered daily brief; "
        "use mcp__project_db__search_projects for the facts."
    ),
    git_commit="abc1234",
)
SKILL_SIMILAR = SkillSource(
    skill_id="skill-2",
    name="daily-news-brief",
    description="Turn a story into a numbered daily brief.",
    git_commit=None,
)
SKILL_OTHER = SkillSource(
    skill_id="skill-3",
    name="risk-summary",
    description="Summarise project risk in a short memo.",
    git_commit="def5678",
)

POLICY = KnowledgeSource(
    knowledge_base_id="kb-1",
    document_id="doc-1",
    filename="policy.md",
    chunk_id="doc-1:0",
    chunk_content="Refunds take five business days.",
    chunk_count=2,
    related_chunks=(("doc-1:1", "Returns require the original receipt."),),
    section="refunds",
    page=1,
)
SLA = KnowledgeSource(
    knowledge_base_id="kb-1",
    document_id="doc-2",
    filename="sla.md",
    chunk_id="doc-2:0",
    chunk_content="Support answers within one business day.",
    chunk_count=1,
    section="support",
    page=2,
)
TRACE = TraceSource(
    trace_id="trace-1",
    query="Why did the refund fail?",
    category="kb_single_hop",
)


def _session() -> AccountSession:
    return AccountSession(
        credential=AccountCredential(
            label="analyst_a", username=RAW_USERNAME, password=RAW_PASSWORD
        ),
        client=httpx.Client(base_url="http://gateway.test/api"),
        user_id=RAW_USER_ID,
        hashed_user_id=HASHED_USER_ID,
    )


def _sources(
    *,
    tools: Any = (),
    skills: Any = (),
    knowledge: Any = (),
    traces: Any = (),
    warnings: Any = (),
) -> BuildSources:
    return BuildSources(
        tools=tools,
        skills=skills,
        knowledge=knowledge,
        traces=traces,
        warnings=warnings,
    )


def _plan(**changes: Any) -> SynthesisPlan:
    return replace(SynthesisPlan(), **changes)


def _by_category(drafts: Any, category: str) -> list[DatasetItemDraft]:
    return [draft for draft in drafts if draft.metadata.get("category") == category]


def _tool_only_plan(**changes: Any) -> SynthesisPlan:
    settings: dict[str, Any] = {
        "tool_samples_per_source": 0,
        "skill_samples_per_source": 0,
        "knowledge_samples_per_source": 0,
        "similar_pairs": 0,
        "include_missing_params": False,
        "include_multi_tool": False,
        "include_boundary": False,
        "include_injection": False,
    }
    settings.update(changes)
    return _plan(**settings)


# --- tools -----------------------------------------------------------------


def test_each_tool_gets_two_distinct_single_tool_phrasings() -> None:
    result = synthesize_account_items(_session(), _sources(tools=[SEARCH, CREATE, FIND]))

    singles = _by_category(result.drafts, "single_tool")
    assert len(singles) == 6
    per_source = {
        tool.call_name: [d for d in singles if d.metadata["source_id"] == tool.call_name]
        for tool in (SEARCH, CREATE, FIND)
    }
    assert all(len(group) == 2 for group in per_source.values())
    for tool in (SEARCH, CREATE, FIND):
        first, second = per_source[tool.call_name]
        assert first.input["query"] != second.input["query"]
        assert first.id != second.id
        assert first.expected_output["expected_tools"] == [tool.call_name]
        args = first.expected_output["expected_calls"][0]["args"]
        assert args
        assert first.expected_output["expected_calls"][0]["check_keys"] == sorted(args)
        assert first.metadata["source_kind"] == "tool"
        assert first.metadata["tool_schema_hash"]


def test_missing_params_only_when_the_schema_requires_properties() -> None:
    result = synthesize_account_items(_session(), _sources(tools=[SEARCH, LIST]))

    missing = _by_category(result.drafts, "missing_params")
    assert [draft.metadata["source_id"] for draft in missing] == [SEARCH.call_name]
    only = missing[0]
    assert only.expected_output["expect_clarification"] is True
    assert only.expected_output["expected_calls"] == []
    assert only.expected_output["forbidden_tools"] == []
    assert only.expected_output["expected_tools"] == []
    assert only.input["query"]


def test_similar_tools_use_the_highest_description_token_overlap() -> None:
    plan = _tool_only_plan(similar_pairs=1)
    result = synthesize_account_items(
        _session(),
        _sources(tools=[SEARCH, FIND, LIST]),
        plan=plan,
    )

    similar = _by_category(result.drafts, "similar_tools")
    assert len(similar) == 1
    draft = similar[0]
    pair = draft.expected_output["similar_tools"]
    assert pair[0] == draft.expected_output["expected_tools"][0]
    assert set(pair) == {SEARCH.call_name, FIND.call_name}
    assert draft.expected_output["forbidden_tools"] == []
    assert pair[1] in {SEARCH.call_name, FIND.call_name}
    assert draft.input["query"]
    assert draft.metadata["category"] == "similar_tools"


def test_similar_pairs_are_capped_by_the_plan() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(tools=TOOLS),
        plan=_tool_only_plan(similar_pairs=1),
    )

    assert len(_by_category(result.drafts, "similar_tools")) == 1


def test_multi_tool_pairs_a_read_like_and_write_like_tool_on_one_server() -> None:
    plan = _tool_only_plan(include_multi_tool=True)
    result = synthesize_account_items(
        _session(),
        _sources(tools=[SEARCH, CREATE, FIND, UPDATE]),
        plan=plan,
    )

    multi = _by_category(result.drafts, "multi_tool")
    assert len(multi) == 2
    for draft in multi:
        expected = draft.expected_output["expected_tools"]
        assert len(expected) == 2
        assert len(draft.expected_output["expected_calls"]) == 2
        server_names = {name.split("__")[1] for name in expected}
        assert len(server_names) == 1
        assert {call["tool"] for call in draft.expected_output["expected_calls"]} == set(expected)
        assert expected[0].split("__")[2] in {"search_projects", "find_customer"}
        assert draft.input["query"]


def test_injection_is_only_built_for_a_detected_destructive_tool() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(tools=[SEARCH, DELETE]),
        plan=_tool_only_plan(include_injection=True),
    )

    injection = _by_category(result.drafts, "injection")
    assert len(injection) == 1
    draft = injection[0]
    assert draft.expected_output["forbidden_tools"] == [DELETE.call_name]
    assert draft.expected_output["expected_tools"] == []
    assert draft.expected_output["expected_calls"] == []
    assert draft.expected_output["expect_refusal"] is True
    assert draft.input["query"]


def test_unsafe_call_names_are_skipped_with_a_warning() -> None:
    unsafe = McpTool(
        call_name="mcp__project_db__delete.project",
        name="delete.project",
        description="Delete a project.",
        input_schema=DELETE_SCHEMA,
    )
    result = synthesize_account_items(
        _session(),
        _sources(tools=[SEARCH, unsafe]),
        plan=_tool_only_plan(include_injection=True),
    )

    assert all(draft.metadata["source_id"] != unsafe.call_name for draft in result.drafts)
    assert any("unsafe" in warning for warning in result.warnings)


# --- skills ----------------------------------------------------------------


def test_each_private_skill_gets_two_distinct_phrasings() -> None:
    result = synthesize_account_items(
        _session(), _sources(skills=[SKILL_OTHER, SKILL_SIMILAR])
    )

    singles = _by_category(result.drafts, "skill_single")
    assert len(singles) == 4
    for skill in (SKILL_OTHER, SKILL_SIMILAR):
        group = [d for d in singles if d.metadata["source_id"] == skill.skill_id]
        assert len(group) == 2
        assert group[0].input["query"] != group[1].input["query"]
        assert group[0].id != group[1].id
        assert group[0].expected_output["expected_skills"] == [skill.name]
        assert group[0].expected_output["expect_skill"] is True
        assert group[0].metadata["skill_commit"] == skill.git_commit


def test_similar_skills_use_description_overlap_and_target_first() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(skills=[SKILL_WITH_TOOL, SKILL_SIMILAR, SKILL_OTHER]),
        plan=_plan(skill_samples_per_source=0, similar_pairs=1),
    )

    similar = _by_category(result.drafts, "similar_skills")
    assert len(similar) == 1
    draft = similar[0]
    pair = draft.expected_output["similar_skills"]
    assert set(pair) == {SKILL_WITH_TOOL.skill_id, SKILL_SIMILAR.skill_id}
    selected = next(
        skill for skill in (SKILL_WITH_TOOL, SKILL_SIMILAR) if skill.skill_id == pair[0]
    )
    assert draft.expected_output["expected_skills"] == [selected.name]
    assert draft.metadata["category"] == "similar_skills"


def test_skill_with_tools_needs_a_real_call_name_in_the_description() -> None:
    plan = _plan(
        tool_samples_per_source=0,
        skill_samples_per_source=0,
        knowledge_samples_per_source=0,
        similar_pairs=0,
        include_missing_params=False,
        include_multi_tool=False,
        include_boundary=False,
        include_injection=False,
    )
    result = synthesize_account_items(
        _session(),
        _sources(tools=[SEARCH], skills=[SKILL_WITH_TOOL, SKILL_SIMILAR]),
        plan=plan,
    )

    with_tools = _by_category(result.drafts, "skill_with_tools")
    assert len(with_tools) == 1
    draft = with_tools[0]
    assert draft.metadata["source_id"] == SKILL_WITH_TOOL.skill_id
    assert draft.expected_output["expected_tools"] == [SEARCH.call_name]
    assert draft.expected_output["expected_skills"] == [SKILL_WITH_TOOL.name]


# --- knowledge -------------------------------------------------------------


def test_knowledge_single_and_multi_hop_use_real_chunks() -> None:
    result = synthesize_account_items(
        _session(), _sources(knowledge=[POLICY, SLA])
    )

    single = _by_category(result.drafts, "kb_single_hop")
    assert len(single) == 2
    for draft in single:
        assert draft.expected_output["expect_kb"] is True
        assert len(draft.expected_output["expected_doc_ids"]) == 1
        assert draft.expected_output["reference_answer"]
        assert draft.expected_output["expect_refusal"] is False

    multi = _by_category(result.drafts, "kb_multi_hop")
    assert [draft.metadata["source_id"] for draft in multi] == [POLICY.document_id]
    hop = multi[0]
    assert hop.expected_output["expected_doc_ids"] == [POLICY.document_id]
    assert "five business days" in hop.expected_output["reference_answer"]
    assert "original receipt" in hop.expected_output["reference_answer"]
    assert hop.metadata["chunk_id"] == POLICY.chunk_id
    assert hop.metadata["related_chunk_ids"] == ["doc-1:1"]


def test_unanswerable_questions_are_capped_and_expect_refusal() -> None:
    result = synthesize_account_items(
        _session(), _sources(knowledge=[POLICY, SLA])
    )

    unanswerable = _by_category(result.drafts, "kb_unanswerable")
    assert len(unanswerable) == MAX_UNANSWERABLE_PER_ACCOUNT
    for draft in unanswerable:
        assert draft.expected_output["expect_refusal"] is True
        assert draft.expected_output["expected_doc_ids"] == []
        assert draft.expected_output["expect_kb"] is True
        assert draft.input["query"]


def test_unanswerable_questions_are_not_built_without_knowledge_sources() -> None:
    result = synthesize_account_items(_session(), _sources(tools=[SEARCH]))

    assert _by_category(result.drafts, "kb_unanswerable") == []


# --- boundary, trace, determinism -----------------------------------------


def test_boundary_questions_are_fixed_no_tool_samples() -> None:
    result = synthesize_account_items(
        _session(), _sources(tools=[SEARCH]), plan=_tool_only_plan(include_boundary=True)
    )

    boundary = _by_category(result.drafts, "no_tool")
    assert [draft.input["query"] for draft in boundary] == list(BOUNDARY_QUESTIONS)
    for draft in boundary:
        assert draft.expected_output["no_tool_ok"] is True
        assert draft.expected_output["expected_tools"] == []
        assert draft.expected_output["expect_kb"] is False
        assert draft.expected_output["expect_skill"] is False
        assert draft.metadata["source_kind"] == "tool"


def test_trace_sources_become_unscored_trace_replay_drafts() -> None:
    result = synthesize_account_items(
        _session(), _sources(traces=[TRACE]), plan=_tool_only_plan()
    )

    trace = _by_category(result.drafts, "trace_replay")
    assert len(trace) == 1
    assert trace[0].input == {"query": TRACE.query}
    assert trace[0].expected_output == {}
    assert trace[0].metadata["source_kind"] == "trace"
    assert trace[0].metadata["source_id"] == TRACE.trace_id
    assert trace[0].metadata["review_status"] == "pending"


def test_all_synthetic_queries_are_chinese() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(
            tools=[SEARCH, CREATE, DELETE, FIND, UPDATE, LIST],
            skills=[SKILL_WITH_TOOL, SKILL_SIMILAR, SKILL_OTHER],
            knowledge=[POLICY, SLA],
        ),
    )

    queries = [draft.input["query"] for draft in result.drafts]
    assert queries
    assert all(re.search(r"[\u4e00-\u9fff]", query) for query in queries)


def test_generation_is_deterministic_and_ids_are_unique() -> None:
    sources = _sources(
        tools=[SEARCH, CREATE, DELETE, FIND, UPDATE, LIST],
        skills=[SKILL_WITH_TOOL, SKILL_SIMILAR, SKILL_OTHER],
        knowledge=[POLICY, SLA],
        traces=[TRACE],
    )

    first = synthesize_account_items(_session(), sources)
    second = synthesize_account_items(_session(), sources)

    assert [draft.id for draft in first.drafts] == [draft.id for draft in second.drafts]
    assert [draft.input for draft in first.drafts] == [draft.input for draft in second.drafts]
    assert len({draft.id for draft in first.drafts}) == len(first.drafts)
    assert first.dropped_duplicates == second.dropped_duplicates


def test_max_total_caps_the_result_without_losing_duplicate_accounting() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(tools=TOOLS, skills=[SKILL_SIMILAR], knowledge=[POLICY]),
        plan=_plan(max_total=5),
    )

    assert len(result.drafts) == 5


def test_every_query_is_non_empty_and_no_raw_account_value_leaks() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(
            tools=[SEARCH, CREATE, DELETE, FIND, UPDATE, LIST],
            skills=[SKILL_WITH_TOOL, SKILL_SIMILAR, SKILL_OTHER],
            knowledge=[POLICY, SLA],
            traces=[TRACE],
        ),
    )

    payload = json.dumps(
        [
            {"input": draft.input, "expected": draft.expected_output, "metadata": draft.metadata}
            for draft in result.drafts
        ],
        ensure_ascii=False,
    )
    for draft in result.drafts:
        assert isinstance(draft.input.get("query"), str)
        assert draft.input["query"].strip()
        assert draft.metadata["user_id"] == HASHED_USER_ID
    assert RAW_USER_ID not in payload
    assert RAW_USERNAME not in payload
    assert RAW_PASSWORD not in payload


def test_plan_booleans_disable_their_categories() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(tools=[SEARCH, DELETE]),
        plan=_tool_only_plan(
            include_missing_params=False,
            include_multi_tool=False,
            include_boundary=False,
            include_injection=False,
        ),
    )

    categories = {draft.metadata["category"] for draft in result.drafts}
    assert "missing_params" not in categories
    assert "multi_tool" not in categories
    assert "no_tool" not in categories
    assert "injection" not in categories


# --- dedup -----------------------------------------------------------------


class _Embedder:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vectors[text] for text in texts]


def _draft(
    item_id: str,
    query: str,
    *,
    expected: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    source_id: str = "mcp__project_db__search_projects",
    category: str = "single_tool",
    user_id: str = HASHED_USER_ID,
) -> DatasetItemDraft:
    return DatasetItemDraft(
        id=item_id,
        input={"query": query},
        expected_output=(
            {"expected_tools": [source_id], "expected_calls": []}
            if expected is None
            else expected
        ),
        metadata={
            "user_id": user_id,
            "source_id": source_id,
            "source_kind": "tool",
            "category": category,
            "source": "synthetic_from_tools",
            **(metadata or {}),
        },
    )


def test_dedupe_drops_exact_normalized_queries_first() -> None:
    kept, dropped = dedupe_drafts(
        [
            _draft("a", "Please search the project database"),
            _draft("b", "  please   SEARCH the project database  "),
            _draft("c", "List every project sector"),
        ]
    )

    assert [draft.id for draft in kept] == ["a", "c"]
    assert dropped == 1


def test_dedupe_drops_near_duplicate_character_shingles() -> None:
    kept, dropped = dedupe_drafts(
        [
            _draft("a", "Please search the project database for solar projects"),
            _draft("b", "Please search the project database for solar projects now"),
            _draft("c", "Completely different customer question"),
        ]
    )

    assert [draft.id for draft in kept] == ["a", "c"]
    assert dropped == 1


def test_optional_embedder_drops_cosine_near_duplicates() -> None:
    embedder = _Embedder(
        {
            "Alpha question": [1.0, 0.0],
            "Beta question": [0.95, 0.31],
            "Gamma question": [0.0, 1.0],
        }
    )

    kept, dropped = dedupe_drafts(
        [
            _draft("a", "Alpha question"),
            _draft("b", "Beta question"),
            _draft("c", "Gamma question"),
        ],
        embedder=embedder,
    )

    assert [draft.id for draft in kept] == ["a", "c"]
    assert dropped == 1
    assert embedder.calls == [["Alpha question", "Beta question", "Gamma question"]]


# --- quality ---------------------------------------------------------------


def _quality_sources() -> BuildSources:
    return _sources(
        tools=[SEARCH, CREATE],
        skills=[SKILL_SIMILAR],
        knowledge=[POLICY],
    )


def test_quality_report_names_every_task_six_problem() -> None:
    valid = _draft("valid", "A valid unique question")
    cases = [
        (
            replace(valid, metadata={**valid.metadata, "user_id": "not-a-hash"}),
            "metadata.user_id",
        ),
        (replace(valid, input={}), "missing query"),
        (replace(valid, expected_output={}), "no declared expectation"),
        (replace(valid, id="valid"), "duplicate item id"),
        (replace(valid, input={"query": "a VALID unique question!"}), "duplicate normalized query"),
        (
            replace(
                valid,
                expected_output={"expected_tools": ["mcp__db__bad.tool"]},
            ),
            "unsafe expected tool name",
        ),
        (
            _draft(
                "kb",
                "A knowledge question",
                expected={"expected_doc_ids": ["not-in-account"]},
                metadata={"source_kind": "knowledge"},
                source_id="not-in-account",
                category="kb_single_hop",
            ),
            "account sources",
        ),
    ]

    for bad, needle in cases:
        report = quality_report(
            [valid, bad] if "duplicate" in needle else [bad], sources=_quality_sources()
        )
        assert report.ok is False
        assert any(needle in problem for problem in report.problems), (needle, report.problems)




def test_quality_report_allows_one_query_across_two_accounts() -> None:
    first = _draft("first", "the same boundary question")
    second = _draft(
        "second",
        "the same boundary question",
        user_id="0123456789abcdef0123456789abcdef",
    )

    report = quality_report([first, second])

    assert report.ok is True
    assert report.problems == ()




def test_quality_report_rejects_unsolvable_tool_and_skill_expectations() -> None:
    sources = _quality_sources()
    unknown_tool = _draft(
        "unknown-tool",
        "a unique question",
        expected={"expected_tools": ["mcp__db__ghost"]},
    )
    unknown_skill = _draft(
        "unknown-skill",
        "another unique question",
        expected={"expected_skills": ["ghost-skill"]},
        source_id="skill-2",
        category="skill_single",
    )

    report = quality_report([unknown_tool, unknown_skill], sources=sources)

    assert report.ok is False
    assert any("expected tool is not in account sources" in problem for problem in report.problems)
    assert any("expected Skill is not in account sources" in problem for problem in report.problems)

def test_quality_report_accepts_a_clean_synthesized_set_and_counts_dimensions() -> None:
    result = synthesize_account_items(
        _session(),
        _sources(
            tools=[SEARCH, CREATE],
            skills=[SKILL_SIMILAR],
            knowledge=[POLICY],
            traces=[TRACE],
        ),
    )
    report = quality_report(result.drafts, sources=_quality_sources(), dropped_duplicates=3)

    assert isinstance(report, QualityReport)
    assert report.ok is True
    assert report.problems == ()
    assert report.total == len(result.drafts)
    assert report.dropped_duplicates == 3
    assert sum(report.category_counts.values()) == report.total
    assert sum(report.difficulty_counts.values()) == report.total
    assert report.source_counts["tool"] >= 1
    assert "total:" in report.render()


def test_quality_warnings_cover_distribution_empty_category_and_one_source() -> None:
    drafts = [
        _draft(f"id-{index}", f"A unique single-tool question number {index}")
        for index in range(10)
    ]

    report = quality_report(drafts, sources=_quality_sources())

    assert report.ok is True
    assert any("share" in warning for warning in report.warnings)
    assert any("empty category" in warning for warning in report.warnings)
    assert any("one source" in warning for warning in report.warnings)


def test_quality_report_exempts_trace_replay_from_declared_expectations() -> None:
    trace = DatasetItemDraft(
        id="trace-1",
        input={"query": "Why did it fail?"},
        expected_output={},
        metadata={
            "user_id": HASHED_USER_ID,
            "source_id": "trace-1",
            "source_kind": "trace",
            "category": "trace_replay",
            "source": "online_trace",
        },
    )

    report = quality_report([trace], sources=_quality_sources())

    assert report.ok is True


# --- fingerprint -----------------------------------------------------------


def test_dataset_fingerprint_is_stable_order_independent_and_16_hex() -> None:
    one = _draft("a", "First question")
    two = _draft("b", "Second question")

    fingerprint = dataset_fingerprint([one, two])

    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert dataset_fingerprint([two, one]) == fingerprint
    assert dataset_fingerprint([replace(one, input={"query": "Changed"}), two]) != fingerprint
    assert dataset_fingerprint(
        [one, replace(two, expected_output={"expected_tools": ["mcp__other__tool"]})]
    ) != fingerprint
    assert dataset_fingerprint(
        [replace(one, metadata={**one.metadata, "category": "multi_tool"}), two]
    ) != fingerprint


# --- question writer -------------------------------------------------------


class _RecordingWriter:
    def __init__(self) -> None:
        self.generations: list[
            tuple[str, dict[str, Any], dict[str, Any], int, str]
        ] = []
        self.rules: list[tuple[str, str, str]] = []

    def generate(
        self,
        *,
        kind: str,
        source: Any,
        evaluation: Any,
        variant: int,
        fallback: str,
    ) -> str:
        index = len(self.generations)
        self.generations.append(
            (kind, dict(source), dict(evaluation), variant, fallback)
        )
        return f"生成的问题 {index}"

    def skill_rules(
        self, *, name: str, description: str, body: str
    ) -> list[str]:
        self.rules.append((name, description, body))
        return [f"Follow {name} exactly", "Return the numbered format"]


class _BrokenWriter:
    def generate(
        self,
        *,
        kind: str,
        source: Any,
        evaluation: Any,
        variant: int,
        fallback: str,
    ) -> str:
        raise RuntimeError("judge unavailable")

    def skill_rules(
        self, *, name: str, description: str, body: str
    ) -> list[str]:
        raise RuntimeError("judge unavailable")


class _ShortWriter:
    def generate(
        self,
        *,
        kind: str,
        source: Any,
        evaluation: Any,
        variant: int,
        fallback: str,
    ) -> str:
        return ""

    def skill_rules(
        self, *, name: str, description: str, body: str
    ) -> list[str]:
        return ["only one rule"]


class _FakeJudge:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_writer_generates_queries_from_source_and_supplies_skill_rules() -> None:
    writer = _RecordingWriter()
    sources = _sources(
        tools=[SEARCH], skills=[SKILL_WITH_TOOL], knowledge=[POLICY]
    )

    result = synthesize_account_items(_session(), sources, writer=writer)

    assert result.drafts
    assert all(
        draft.input["query"].startswith("生成的问题") for draft in result.drafts
    )
    assert all(len(generation) == 5 for generation in writer.generations)
    assert all(
        generation[1] and generation[2]
        for generation in writer.generations
    )
    skill = _by_category(result.drafts, "skill_single")[0]
    assert skill.expected_output["skill_rules"] == [
        f"Follow {SKILL_WITH_TOOL.name} exactly",
        "Return the numbered format",
    ]
    assert all(draft.metadata["user_id"] == HASHED_USER_ID for draft in result.drafts)


def test_writer_receives_source_and_evaluation_not_template() -> None:
    writer = _RecordingWriter()
    sources = _sources(tools=[SEARCH])

    synthesize_account_items(_session(), sources, writer=writer)

    kind, source, evaluation, variant, fallback = writer.generations[0]
    assert kind == "single_tool"
    assert source["kind"] == "tool"
    assert source["tools"][0]["name"] == "search_projects"
    assert source["tools"][0]["role"] == "target"
    assert evaluation["category"] == "single_tool"
    assert evaluation["expected_arguments"] == [{"sector": "solar"}]
    assert variant == 0
    assert fallback.startswith("请使用 search_projects 工具")
    assert fallback not in json.dumps(source, ensure_ascii=False)
    assert fallback not in json.dumps(evaluation, ensure_ascii=False)


def test_writer_material_includes_knowledge_content() -> None:
    writer = _RecordingWriter()
    sources = _sources(knowledge=[POLICY])

    synthesize_account_items(_session(), sources, writer=writer)

    kind, source, evaluation, _variant, fallback = next(
        generation
        for generation in writer.generations
        if generation[0] == "kb_single_hop"
    )
    assert kind == "kb_single_hop"
    assert source["kind"] == "knowledge"
    assert source["content"] == POLICY.chunk_content
    assert evaluation["category"] == "kb_single_hop"
    assert fallback.startswith("根据《policy.md》")

    unanswerable = next(
        generation
        for generation in writer.generations
        if generation[0] == "kb_unanswerable"
    )
    assert unanswerable[1]["covered_documents"] == ["policy.md"]
    assert unanswerable[2]["expect_refusal"] is True


def test_writer_exception_falls_back_to_deterministic_queries() -> None:
    sources = _sources(tools=[SEARCH], skills=[SKILL_SIMILAR], knowledge=[POLICY])
    baseline = synthesize_account_items(_session(), sources)
    result = synthesize_account_items(_session(), sources, writer=_BrokenWriter())

    assert [draft.input["query"] for draft in result.drafts] == [
        draft.input["query"] for draft in baseline.drafts
    ]
    assert any("question writer failed" in warning for warning in result.warnings)


def test_writer_returning_an_empty_query_falls_back() -> None:
    sources = _sources(tools=[SEARCH])
    baseline = synthesize_account_items(_session(), sources)
    result = synthesize_account_items(_session(), sources, writer=_ShortWriter())

    assert [draft.input["query"] for draft in result.drafts] == [
        draft.input["query"] for draft in baseline.drafts
    ]
    assert any("question writer failed" in warning for warning in result.warnings)


def test_writer_returning_malformed_skill_rules_falls_back() -> None:
    class _EmptyRulesWriter(_RecordingWriter):
        def skill_rules(
            self, *, name: str, description: str, body: str
        ) -> list[str]:
            return []

    result = synthesize_account_items(
        _session(), _sources(skills=[SKILL_SIMILAR]), writer=_EmptyRulesWriter()
    )

    skill = _by_category(result.drafts, "skill_single")[0]
    assert skill.expected_output["skill_rules"] == [
        f"Follow the {SKILL_SIMILAR.name} skill: {SKILL_SIMILAR.description}"
    ]
    assert any("malformed skill rules" in warning for warning in result.warnings)


def test_no_writer_never_constructs_a_judge_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import myflow_evals.synthesis as synthesis

    class _ExplodingWriter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("no writer was configured")

    monkeypatch.setattr(synthesis, "OpenAIQuestionWriter", _ExplodingWriter)

    result = synthesize_account_items(_session(), _sources(tools=[SEARCH]))

    assert result.drafts


def test_long_descriptions_are_bounded_for_queries_and_writer_material() -> None:
    long_description = (
        "First sentence states the purpose. Second sentence explains the action. "
        + "Operational rule with scoring details. " * 100
    )
    tool = replace(SEARCH, description=long_description)
    sources = _sources(tools=[tool])

    baseline = synthesize_account_items(_session(), sources)
    query = _by_category(baseline.drafts, "single_tool")[0].input["query"]
    assert len(query) < 600
    assert "First sentence states the purpose." in query
    assert "Operational rule with scoring details." not in query
    assert ".." not in query

    writer = _RecordingWriter()
    synthesize_account_items(_session(), sources, writer=writer)
    kind, source, _evaluation, _variant, _fallback = writer.generations[0]
    assert kind == "single_tool"
    source_text = json.dumps(source, ensure_ascii=False)
    assert len(source_text) < 4000
    assert "First sentence states the purpose." in source_text
    assert "Operational rule with scoring details." not in source_text

def test_synthesis_generation_prompt_does_not_include_template_query() -> None:
    judge = _FakeJudge('{"query": "生成的问题"}')
    writer = OpenAIQuestionWriter(judge)
    sources = _sources(tools=[SEARCH])

    baseline = synthesize_account_items(_session(), sources)
    deterministic = _by_category(baseline.drafts, "single_tool")[0].input[
        "query"
    ]
    synthesize_account_items(_session(), sources, writer=writer)

    system, prompt = judge.calls[0]
    assert "Generate" in system
    assert deterministic not in prompt
    assert "queries" not in json.loads(prompt)


def test_openai_writer_generates_from_source_and_evaluation() -> None:
    judge = _FakeJudge('{"query": "生成的问题"}')
    writer = OpenAIQuestionWriter(judge)
    source = {
        "kind": "tool",
        "tools": [
            {
                "role": "target",
                "name": "search_projects",
                "description": "Search projects.",
                "required_parameters": ["sector"],
            }
        ],
    }
    evaluation = {
        "category": "single_tool",
        "expected_arguments": [{"sector": "solar"}],
    }

    query = writer.generate(
        kind="single_tool",
        source=source,
        evaluation=evaluation,
        variant=0,
        fallback="规则问题",
    )

    assert query == "生成的问题"
    system, prompt = judge.calls[0]
    payload = json.loads(prompt)
    assert "Simplified Chinese" in system
    assert "template" in system.lower()
    assert payload["output_language"] == "zh-CN"
    assert payload["source"] == source
    assert payload["evaluation_item"] == evaluation
    assert payload["variant"] == 0
    assert payload["style_hint"] == "direct request"
    assert payload["category_guidance"]
    assert "fallback" not in payload
    assert "规则问题" not in prompt
    assert "queries" not in payload


def test_openai_writer_uses_conversational_style_for_odd_variant() -> None:
    judge = _FakeJudge('{"query": "生成的问题"}')
    writer = OpenAIQuestionWriter(judge)

    writer.generate(
        kind="single_tool",
        source={"kind": "tool", "tools": []},
        evaluation={"category": "single_tool"},
        variant=1,
        fallback="规则问题",
    )

    _system, prompt = judge.calls[0]
    payload = json.loads(prompt)
    assert payload["style_hint"] == "conversational question"


def test_openai_writer_generation_rejects_non_chinese_output() -> None:
    writer = OpenAIQuestionWriter(
        _FakeJudge('{"query": "Use the search tool."}')
    )

    query = writer.generate(
        kind="single_tool",
        source={"kind": "tool", "tools": []},
        evaluation={"category": "single_tool"},
        variant=0,
        fallback="规则问题",
    )

    assert query == "规则问题"
    assert any("generation failed" in warning for warning in writer.warnings)


def test_openai_writer_generation_returns_fallback_on_malformed_reply() -> None:
    writer = OpenAIQuestionWriter(_FakeJudge("not json"))

    query = writer.generate(
        kind="kb_single_hop",
        source={"kind": "knowledge", "content": "Refunds take five days."},
        evaluation={"category": "kb_single_hop"},
        variant=0,
        fallback="根据《policy.md》，要点是什么？",
    )

    assert query == "根据《policy.md》，要点是什么？"
    assert any("generation failed" in warning for warning in writer.warnings)


def test_openai_writer_generation_returns_fallback_on_judge_exception() -> None:
    writer = OpenAIQuestionWriter(_FakeJudge(RuntimeError("boom")))

    query = writer.generate(
        kind="single_tool",
        source={"kind": "tool", "tools": []},
        evaluation={"category": "single_tool"},
        variant=0,
        fallback="规则问题",
    )

    assert query == "规则问题"
    assert any("generation failed" in warning for warning in writer.warnings)


def test_openai_writer_skill_rules_prompt_is_grounded_and_never_empty() -> None:
    judge = _FakeJudge('{"rules": ["Follow the brief skill."]}')
    writer = OpenAIQuestionWriter(judge)

    writer.skill_rules(name="brief", description="Write a brief.", body="")

    system, prompt = judge.calls[0]
    payload = json.loads(prompt)
    assert "do not invent" in system.lower()
    assert "never return an empty" in system.lower()
    assert '"rules"' in system.lower()
    assert payload["body_available"] is False
    assert payload["output_format"]["rules"]


def test_writer_translates_trace_replay_to_chinese() -> None:
    judge = _FakeJudge('{"query": "退款为什么失败？"}')
    writer = OpenAIQuestionWriter(judge)

    result = synthesize_account_items(
        _session(), _sources(traces=[TRACE]), plan=_tool_only_plan(), writer=writer
    )

    trace = _by_category(result.drafts, "trace_replay")
    assert trace[0].input == {"query": "退款为什么失败？"}
    _system, prompt = judge.calls[0]
    payload = json.loads(prompt)
    assert payload["kind"] == "trace_replay"
    assert payload["source"]["original_query"] == TRACE.query
    assert payload["evaluation_item"]["category"] == "trace_replay"


def test_synthesis_drains_openai_writer_warnings_into_the_result() -> None:
    writer = OpenAIQuestionWriter(_FakeJudge("not json"))
    result = synthesize_account_items(
        _session(), _sources(tools=[SEARCH]), writer=writer
    )

    assert any(
        "question writer" in warning or "generation failed" in warning
        for warning in result.warnings
    )
