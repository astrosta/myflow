"""citation_valid tests: an answer may only cite what this run retrieved.

The metric validates the final AI message's ``knowledge_citations`` against the
run's own ``retrieval.kb`` chunks. A forged id -- or a citation that names a
real chunk by a pair the retriever never produced -- fails the sample, and an
answer that cites nothing only fails when the item expected knowledge.
"""

from __future__ import annotations

from typing import Any

from myflow_evals.evaluators import citation_valid
from myflow_evals.evidence import RunEvidence


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
        "retrieved_chunks": [],
        "citations": [],
        "trace_id": None,
    }
    fields.update(overrides)
    return RunEvidence(**fields)


def _chunk(
    document_id: str,
    index: int = 0,
    citation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chunk_id": f"{document_id}:{index}",
        "citation_id": citation_id or f"k{document_id.replace('-', '')}{index}",
        "content": "Refunds take five business days.",
    }


def _citation(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "citation_id": chunk["citation_id"],
        "chunk_id": chunk["chunk_id"],
        "document_id": chunk["document_id"],
    }


def test_citation_valid_passes_when_every_citation_was_retrieved() -> None:
    chunk = _chunk("doc-1")
    evidence = _evidence(retrieved_chunks=[chunk], citations=[_citation(chunk)])

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.name == "citation_valid"
    assert score.value == 1.0
    assert "1 citation" in score.comment


def test_citation_valid_fails_a_forged_citation_id() -> None:
    chunk = _chunk("doc-1")
    forged = {
        "citation_id": "kforged01",
        "chunk_id": chunk["chunk_id"],
        "document_id": "doc-1",
    }
    evidence = _evidence(retrieved_chunks=[chunk], citations=[forged])

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
    assert "kforged01" in score.comment


def test_citation_valid_fails_an_unknown_chunk_id() -> None:
    chunk = _chunk("doc-1")
    forged = {
        "citation_id": chunk["citation_id"],
        "chunk_id": "doc-9:3",
        "document_id": "doc-1",
    }
    evidence = _evidence(retrieved_chunks=[chunk], citations=[forged])

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
    assert "doc-9:3" in score.comment


def test_citation_valid_fails_a_pair_the_retriever_never_produced() -> None:
    first = _chunk("doc-1", 0)
    second = _chunk("doc-1", 1)
    mixed = {
        "citation_id": first["citation_id"],
        "chunk_id": second["chunk_id"],
        "document_id": "doc-1",
    }
    evidence = _evidence(retrieved_chunks=[first, second], citations=[mixed])

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0


def test_citation_valid_fails_one_bad_citation_among_good_ones() -> None:
    good = _chunk("doc-1")
    bad = {"citation_id": "kforged02", "chunk_id": "doc-2:0", "document_id": "doc-2"}
    evidence = _evidence(retrieved_chunks=[good], citations=[_citation(good), bad])

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
    assert "kforged02" in score.comment


def test_citation_valid_is_zero_when_knowledge_was_expected_but_nothing_cited() -> None:
    evidence = _evidence(retrieved_chunks=[_chunk("doc-1")], citations=[])

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
    assert "cited nothing" in score.comment


def test_citation_valid_is_none_when_knowledge_was_not_expected_and_nothing_cited() -> None:
    evidence = _evidence(retrieved_chunks=[_chunk("doc-1")], citations=[])

    assert citation_valid(expected_output={"expect_kb": False}, evidence=evidence) is None
    assert citation_valid(expected_output={}, evidence=evidence) is None


def test_citation_valid_checks_every_retrieved_chunk_not_only_the_top_five() -> None:
    chunks = [_chunk(f"doc-{index}") for index in range(6)]
    evidence = _evidence(
        retrieved_chunks=chunks, citations=[_citation(chunks[0]), _citation(chunks[5])]
    )

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 1.0
    assert "2 citations" in score.comment


def test_citation_valid_fails_a_citation_without_a_pair_partner() -> None:
    evidence = _evidence(
        retrieved_chunks=[_chunk("doc-1")],
        citations=[{"citation_id": "k00000000", "chunk_id": None, "document_id": "doc-1"}],
    )

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0


def test_citation_valid_is_none_when_only_malformed_citation_rows_exist() -> None:
    evidence = _evidence(
        retrieved_chunks=[_chunk("doc-1")],
        citations=["not-a-mapping", None],
    )

    assert (
        citation_valid(expected_output={"expect_kb": True}, evidence=evidence) is None
    )


def test_citation_valid_ignores_malformed_chunk_rows() -> None:
    evidence = _evidence(
        retrieved_chunks=[{"chunk_id": "doc-1:0"}, {"document_id": None}, "junk"],
        citations=[
            {"citation_id": "kforged03", "chunk_id": "doc-1:0", "document_id": "doc-1"}
        ],
    )

    score = citation_valid(expected_output={"expect_kb": True}, evidence=evidence)
    assert score is not None
    assert score.value == 0.0
