import json
from typing import Any

import pytest

from agentkit.config.contextforge_config import ToolSearchConfig
from agentkit.contextforge.catalog import ToolCatalogEntry
from agentkit.contextforge.index import ToolVectorHit
from agentkit.contextforge.store import ContextForgeToolRecord, call_name_for
from agentkit.tools.search.service import (
    CATALOG_UNAVAILABLE,
    MAX_RETRIEVED_TOOLS,
    RERANK_UNAVAILABLE,
    ContextForgeToolSearchService,
    ToolSearchService,
)


def _entry(name: str, description: str, owner: str | None = None) -> ToolCatalogEntry:
    content = f"{name}\n{name}\n{description}"
    return ToolCatalogEntry(
        name=name,
        display_name=name,
        description=description,
        tags=(),
        parameters=("query",),
        server="contextforge",
        content=content,
        content_hash=name + "-hash",
        updated_at=1,
        owner=owner,
    )


class FakeIndex:
    def __init__(self):
        self.fail = False

    async def hybrid_search(self, *, vector, query, limit):
        if self.fail:
            raise RuntimeError("milvus down")
        return [
            ToolVectorHit("github_search", 0.9),
            ToolVectorHit("gitlab_search", 0.7),
            ToolVectorHit("stale_tool", 0.8),
        ]


class FakeEmbedder:
    async def embed_query(self, text):
        return [0.0] * 8


class FakeReranker:
    async def rerank(self, *, query, documents):
        return [type("Score", (), {"index": 1}), type("Score", (), {"index": 0})]


@pytest.mark.asyncio
async def test_search_confirms_hits_and_reranks() -> None:
    entries = (_entry("github_search", "search github code"), _entry("gitlab_search", "search gitlab code"))
    service = ToolSearchService(
        index=FakeIndex(),
        embedder=FakeEmbedder(),
        reranker=FakeReranker(),
        config=ToolSearchConfig(
            candidate_k=5,
            top_k=2,
            embedding={"base_url": "http://embed", "dimensions": 8},
        ),
        snapshot_provider=lambda: entries,
    )
    outcome = await service.search(task_query="github code")
    assert [hit.entry.name for hit in outcome.hits] == ["gitlab_search", "github_search"]
    assert outcome.reranked is True
    assert "stale_tool" not in [hit.entry.name for hit in outcome.hits]


@pytest.mark.asyncio
async def test_vector_failure_falls_back_to_local_bm25() -> None:
    entries = (_entry("github_search", "search github code"),)
    index = FakeIndex()
    index.fail = True
    service = ToolSearchService(
        index=index,
        embedder=FakeEmbedder(),
        reranker=None,
        config=ToolSearchConfig(
            candidate_k=5,
            top_k=2,
            embedding={"base_url": "http://embed", "dimensions": 8},
        ),
        snapshot_provider=lambda: entries,
    )
    outcome = await service.search(task_query="github")
    assert outcome.index_kind == "local_bm25"
    assert outcome.hits[0].entry.name == "github_search"
    assert "vector_unavailable" in outcome.warnings


@pytest.mark.asyncio
async def test_empty_query_returns_empty() -> None:
    service = ToolSearchService(
        index=None,
        embedder=None,
        reranker=None,
        config=ToolSearchConfig(embedding={"enabled": False}),
        snapshot_provider=lambda: (),
    )
    assert (await service.search(task_query=" ")).hits == ()

@pytest.mark.asyncio
async def test_search_only_confirms_the_callers_own_tools() -> None:
    entries = (
        _entry("github_search", "search github code", "u1"),
        _entry("gitlab_search", "search gitlab code", "u2"),
    )
    service = ToolSearchService(
        index=FakeIndex(),
        embedder=FakeEmbedder(),
        reranker=None,
        config=ToolSearchConfig(
            candidate_k=5,
            top_k=5,
            embedding={"base_url": "http://embed", "dimensions": 8},
        ),
        snapshot_provider=lambda: entries,
    )

    outcome = await service.search(task_query="github gitlab", user_id="u1")

    assert [hit.entry.name for hit in outcome.hits] == ["github_search"]


# --------------------------------------------------------------------------- #
# ContextForgeToolSearchService: Milvus hit -> DB row -> tool card
# --------------------------------------------------------------------------- #

EMBEDDING = {"base_url": "http://embed", "dimensions": 8}


def _record(
    name: str = "github_search",
    *,
    owner: str = "u1",
    schema: dict[str, Any] | None = None,
    callable_: bool = True,
) -> ContextForgeToolRecord:
    return ContextForgeToolRecord(
        id="tool-" + name,
        name=name,
        call_name=call_name_for(name),
        display_name=name,
        description=name + " tool",
        input_schema=(
            dict(schema)
            if schema is not None
            else {"type": "object", "properties": {"query": {"type": "string"}}}
        ),
        tags=("github", "code"),
        parameters=("query",),
        owner_user_id=owner,
        enabled=True,
        reachable=True,
        callable=callable_,
        content=name,
    )


class JoinedIndex:
    """The derived Milvus view: it recalls ids, never schemas."""

    def __init__(self, hits=(), *, error: Exception | None = None) -> None:
        self._hits = list(hits)
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def hybrid_search(self, *, vector, query, owner_user_id, limit):
        self.calls.append(
            {"channel": "hybrid", "query": query, "owner": owner_user_id, "limit": limit}
        )
        if self._error is not None:
            raise self._error
        return self._hits[:limit]

    async def dense_search(self, *, vector, owner_user_id, limit):
        self.calls.append(
            {"channel": "dense", "owner": owner_user_id, "limit": limit}
        )
        if self._error is not None:
            raise self._error
        return self._hits[:limit]


class JoinedStore:
    """Stands in for ContextForgeToolStore.get_by_ids: owner + callable only."""

    def __init__(self, records=(), *, error: Exception | None = None) -> None:
        self._by_id = {record.id: record for record in records}
        self._error = error
        self.calls: list[tuple[list[str], str]] = []

    async def get_by_ids(self, ids, owner_user_id):
        self.calls.append((list(ids), owner_user_id))
        if self._error is not None:
            raise self._error
        return [
            self._by_id[tool_id]
            for tool_id in ids
            if tool_id in self._by_id
            and self._by_id[tool_id].owner_user_id == owner_user_id
            and self._by_id[tool_id].callable
        ]


class JoinedEmbedder:
    async def embed_query(self, text):
        return [0.0] * 8


class ReverseReranker:
    async def rerank(self, *, query, documents):
        return [type("Score", (), {"index": position}) for position in reversed(range(len(documents)))]


class BrokenReranker:
    async def rerank(self, *, query, documents):
        raise RuntimeError("rerank down")


def _joined_service(
    *,
    index: Any,
    store: Any,
    reranker: Any = None,
    top_k: int = 5,
    min_score: float | None = None,
    keyword: bool = True,
) -> ContextForgeToolSearchService:
    return ContextForgeToolSearchService(
        config=ToolSearchConfig(
            candidate_k=max(top_k, 10),
            top_k=top_k,
            min_score=min_score,
            embedding=EMBEDDING,
            keyword={"enabled": keyword},
        ),
        index=index,
        store=store,
        embedder=JoinedEmbedder(),
        reranker=reranker,
    )


def _hits(*pairs: tuple[ContextForgeToolRecord, float]) -> list[ToolVectorHit]:
    return [
        ToolVectorHit(record.name, score, tool_id=record.id) for record, score in pairs
    ]


@pytest.mark.asyncio
async def test_cards_join_the_database_in_milvus_rank_order() -> None:
    first, second = _record("first"), _record("second")
    index = JoinedIndex(_hits((second, 0.9), (first, 0.8)))
    store = JoinedStore([first, second])

    outcome = await _joined_service(index=index, store=store).search(
        task_query="github code", user_id="u1"
    )

    assert [hit.tool_name for hit in outcome.hits] == ["second", "first"]
    assert [hit.tool_id for hit in outcome.hits] == [second.id, first.id]
    assert [hit.call_name for hit in outcome.hits] == [
        call_name_for("second"),
        call_name_for("first"),
    ]
    assert outcome.hits[0].input_schema == second.input_schema
    assert outcome.hits[0].score == 1.0
    assert outcome.candidates == 2
    assert outcome.index_kind == "milvus"
    assert store.calls == [([second.id, first.id], "u1")]


@pytest.mark.asyncio
async def test_the_owner_reaches_milvus_and_the_database_join() -> None:
    record = _record()
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record])

    await _joined_service(index=index, store=store).search(
        task_query="github code", domain_keywords=["github"], user_id="u1"
    )

    assert index.calls[0]["owner"] == "u1"
    assert index.calls[0]["channel"] == "hybrid"
    assert index.calls[0]["query"] == "github code github"
    assert store.calls == [([record.id], "u1")]


@pytest.mark.asyncio
async def test_candidates_the_catalog_does_not_confirm_are_dropped() -> None:
    own, foreign, retired = (
        _record("own"),
        _record("foreign", owner="u2"),
        _record("retired", callable_=False),
    )
    index = JoinedIndex(
        _hits((own, 0.9), (foreign, 0.85), (retired, 0.8), (_record("gone"), 0.7))
    )
    store = JoinedStore([own, foreign, retired])

    outcome = await _joined_service(index=index, store=store).search(
        task_query="github code", user_id="u1"
    )

    assert [hit.tool_name for hit in outcome.hits] == ["own"]
    assert outcome.candidates == 1


@pytest.mark.asyncio
async def test_cards_are_capped_at_five() -> None:
    records = [_record(f"tool_{position}") for position in range(8)]
    index = JoinedIndex(
        _hits(*[(record, 1.0 - position / 100) for position, record in enumerate(records)])
    )
    store = JoinedStore(records)

    outcome = await _joined_service(index=index, store=store, top_k=8).search(
        task_query="github code", user_id="u1"
    )

    assert MAX_RETRIEVED_TOOLS == 5
    assert [hit.tool_name for hit in outcome.hits] == [
        f"tool_{position}" for position in range(5)
    ]
    assert outcome.candidates == 8


@pytest.mark.asyncio
async def test_a_milvus_failure_returns_no_hits_without_a_catalog_read() -> None:
    store = JoinedStore([_record()])
    index = JoinedIndex(error=RuntimeError("milvus down"))

    outcome = await _joined_service(index=index, store=store).search(
        task_query="github code", user_id="u1"
    )

    assert outcome.hits == ()
    assert outcome.warnings == (CATALOG_UNAVAILABLE,)
    assert store.calls == []


@pytest.mark.asyncio
async def test_a_missing_index_never_scans_the_catalog() -> None:
    store = JoinedStore([_record()])

    outcome = await _joined_service(index=None, store=store).search(
        task_query="github code", user_id="u1"
    )

    assert outcome.hits == ()
    assert outcome.warnings == (CATALOG_UNAVAILABLE,)
    assert store.calls == []


@pytest.mark.asyncio
async def test_a_database_failure_reports_catalog_unavailable() -> None:
    record = _record()
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record], error=RuntimeError("db down"))

    outcome = await _joined_service(index=index, store=store).search(
        task_query="github code", user_id="u1"
    )

    assert outcome.hits == ()
    assert outcome.warnings == (CATALOG_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_rerank_reorders_the_confirmed_cards() -> None:
    first, second = _record("first"), _record("second")
    index = JoinedIndex(_hits((first, 0.9), (second, 0.8)))
    store = JoinedStore([first, second])

    outcome = await _joined_service(
        index=index, store=store, reranker=ReverseReranker()
    ).search(task_query="github code", user_id="u1")

    assert outcome.reranked is True
    assert [hit.tool_name for hit in outcome.hits] == ["second", "first"]


@pytest.mark.asyncio
async def test_a_rerank_failure_keeps_the_fused_order() -> None:
    first, second = _record("first"), _record("second")
    index = JoinedIndex(_hits((first, 0.9), (second, 0.8)))
    store = JoinedStore([first, second])

    outcome = await _joined_service(
        index=index, store=store, reranker=BrokenReranker()
    ).search(task_query="github code", user_id="u1")

    assert outcome.reranked is False
    assert RERANK_UNAVAILABLE in outcome.warnings
    assert [hit.tool_name for hit in outcome.hits] == ["first", "second"]


@pytest.mark.asyncio
async def test_min_score_filters_normalized_scores() -> None:
    strong, weak = _record("strong"), _record("weak")
    index = JoinedIndex(_hits((strong, 0.9), (weak, 0.3)))
    store = JoinedStore([strong, weak])

    outcome = await _joined_service(
        index=index, store=store, min_score=0.6
    ).search(task_query="github code", user_id="u1")

    assert [hit.tool_name for hit in outcome.hits] == ["strong"]


@pytest.mark.asyncio
async def test_a_large_schema_keeps_the_semantic_keys() -> None:
    schema = {
        "type": "object",
        "properties": {
            f"p{position:03d}": {"type": "string", "description": "d" * 100}
            for position in range(60)
        },
        "required": ["p000", "p059"],
        "examples": [{"p000": "x"}],
    }
    record = _record(schema=schema)
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record])

    outcome = await _joined_service(index=index, store=store).search(
        task_query="github code", user_id="u1"
    )

    card = outcome.hits[0]
    assert card.schema_truncated is True
    serialized = json.dumps(card.input_schema, ensure_ascii=False)
    assert len(serialized) <= 3000
    assert card.input_schema["type"] == "object"
    assert "description" not in json.dumps(card.input_schema)
    assert "examples" not in card.input_schema
    assert set(card.input_schema["required"]) <= set(card.input_schema["properties"])


@pytest.mark.asyncio
async def test_an_empty_query_or_missing_owner_never_searches() -> None:
    record = _record()
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record])
    service = _joined_service(index=index, store=store)

    assert (await service.search(task_query="   ", user_id="u1")).hits == ()
    assert (await service.search(task_query="github code", user_id=None)).hits == ()

    assert index.calls == []
    assert store.calls == []
