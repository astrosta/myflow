"""Knowledge citation contract: stable IDs, manifests, and marker validation."""

from __future__ import annotations

import json

from langchain_core.messages import ToolMessage

from agentkit.knowledge.citations import (
    CITATION_KEY,
    citation_id_for,
    citations_for_hits,
    citations_from_search_payload,
    collect_known_citations,
    resolve_citations,
)
from agentkit.knowledge.models import SearchHit


def _hit(**overrides) -> SearchHit:
    data = {
        "chunk_id": "doc-1:3",
        "document_id": "doc-1",
        "filename": "manual.md",
        "section": "安装",
        "content": "升级前先备份配置。",
        "score": 0.9,
        "knowledge_base_id": "base-1",
        "knowledge_base_name": "产品文档",
        "knowledge_base_type": "technical",
        "chunk_index": 3,
        "page": 7,
    }
    data.update(overrides)
    return SearchHit(**data)


def test_citation_id_is_stable_and_depends_on_the_source_identity() -> None:
    first = citation_id_for(
        chunk_id="doc-1:3", document_id="doc-1", knowledge_base_id="base-1"
    )
    same = citation_id_for(
        chunk_id="doc-1:3", document_id="doc-1", knowledge_base_id="base-1"
    )
    other_doc = citation_id_for(
        chunk_id="doc-2:3", document_id="doc-2", knowledge_base_id="base-1"
    )

    assert first == same
    assert first != other_doc
    assert first.startswith("k")
    assert len(first) == 9


def test_citations_for_hits_carries_the_source_metadata_and_a_bounded_quote() -> None:
    hit = _hit(content="字" * 400)

    [citation] = citations_for_hits([hit])

    assert citation["citation_id"] == citation_id_for(
        chunk_id="doc-1:3", document_id="doc-1", knowledge_base_id="base-1"
    )
    assert citation["filename"] == "manual.md"
    assert citation["section"] == "安装"
    assert citation["page"] == 7
    assert citation["chunk_index"] == 3
    assert citation["knowledge_base_name"] == "产品文档"
    assert len(citation["quote"]) == 240
    assert citation["quote"] == "字" * 240


def test_citations_from_search_payload_accepts_only_well_formed_sources() -> None:
    valid = citations_for_hits([_hit()])
    payload = {
        "sources": [
            valid[0],
            {"citation_id": "", "chunk_id": "x"},
            "not-a-source",
        ]
    }

    assert citations_from_search_payload(payload) == valid


def test_resolve_citations_keeps_only_known_ids_in_first_seen_order() -> None:
    known = {item["citation_id"]: item for item in citations_for_hits([
        _hit(chunk_id="doc-1:1", chunk_index=1),
        _hit(chunk_id="doc-1:2", chunk_index=2),
    ])}
    first, second = list(known)

    resolved = resolve_citations(
        f"结论甲 [{second}]，结论乙 [{first}]，重复 [{second}]，伪造 [kdeadbeef]。",
        known,
    )

    assert [item["citation_id"] for item in resolved] == [second, first]


def test_collect_known_citations_reads_task_metadata_and_search_payloads() -> None:
    first = citations_for_hits([_hit()])[0]
    second = citations_for_hits([_hit(chunk_id="doc-2:0", document_id="doc-2")])[0]
    messages = [
        ToolMessage(
            content="任务成功。\n结果：答案 [kdeadbeef]。",
            tool_call_id="call-1",
            name="task",
            additional_kwargs={CITATION_KEY: [first]},
        ),
        ToolMessage(
            content=json.dumps(
                {"sources": [second], "results": []}, ensure_ascii=False
            ),
            tool_call_id="call-2",
            name="knowledge_search",
        ),
    ]

    known = collect_known_citations(messages)

    assert known == {
        first["citation_id"]: first,
        second["citation_id"]: second,
    }
