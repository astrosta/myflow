"""Tool search retrieval: Milvus hit -> catalog row -> tool card."""

import json
from typing import Any

import pytest

from agentkit.config.contextforge_config import ToolSearchConfig
from agentkit.contextforge.index import ToolVectorHit
from agentkit.contextforge.store import ContextForgeToolRecord, call_name_for
from agentkit.tools.search.service import (
    CATALOG_UNAVAILABLE,
    MAX_RETRIEVED_TOOLS,
    RERANK_UNAVAILABLE,
    ContextForgeToolSearchService,
)


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

    async def keyword_search(self, *, query, owner_user_id, limit):
        self.calls.append(
            {
                "channel": "keyword",
                "query": query,
                "owner": owner_user_id,
                "limit": limit,
            }
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


#: "leave the embedder as the fake one"; None means "no embedder at all".
_DEFAULT_EMBEDDER = object()


def _joined_service(
    *,
    index: Any,
    store: Any,
    reranker: Any = None,
    top_k: int = 5,
    min_score: float | None = None,
    keyword: bool = True,
    embedding: bool = True,
    embedder: Any = _DEFAULT_EMBEDDER,
) -> ContextForgeToolSearchService:
    return ContextForgeToolSearchService(
        config=ToolSearchConfig(
            candidate_k=max(top_k, 10),
            top_k=top_k,
            min_score=min_score,
            embedding=EMBEDDING if embedding else {"enabled": False},
            keyword={"enabled": keyword},
        ),
        index=index,
        store=store,
        embedder=JoinedEmbedder() if embedder is _DEFAULT_EMBEDDER else embedder,
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
async def test_embedding_only_config_runs_the_dense_channel() -> None:
    record = _record()
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record])

    outcome = await _joined_service(
        index=index, store=store, keyword=False
    ).search(task_query="github code", user_id="u1")

    assert [call["channel"] for call in index.calls] == ["dense"]
    assert outcome.channels == ("embedding",)
    assert [hit.tool_name for hit in outcome.hits] == ["github_search"]
    assert outcome.index_kind == "milvus"


@pytest.mark.asyncio
async def test_keyword_only_config_runs_the_sparse_channel_without_an_embedder() -> None:
    record = _record()
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record])

    outcome = await _joined_service(
        index=index, store=store, embedding=False, embedder=None
    ).search(task_query="github code", domain_keywords=["github"], user_id="u1")

    assert [call["channel"] for call in index.calls] == ["keyword"]
    assert index.calls[0]["owner"] == "u1"
    assert index.calls[0]["query"] == "github code github"
    assert outcome.channels == ("keyword",)
    assert [hit.tool_name for hit in outcome.hits] == ["github_search"]
    assert store.calls == [([record.id], "u1")]


@pytest.mark.asyncio
async def test_an_index_without_a_sparse_channel_is_unavailable() -> None:
    class DenseOnlyIndex:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def dense_search(self, *, vector, owner_user_id, limit):  # pragma: no cover
            self.calls.append("dense")
            raise AssertionError("embedding is disabled")

        async def hybrid_search(self, *, vector, query, owner_user_id, limit):  # pragma: no cover
            self.calls.append("hybrid")
            raise AssertionError("embedding is disabled")

    store = JoinedStore([_record()])
    index = DenseOnlyIndex()

    outcome = await _joined_service(
        index=index, store=store, embedding=False, embedder=None
    ).search(task_query="github code", user_id="u1")

    assert index.calls == []
    assert store.calls == []
    assert outcome.hits == ()
    assert outcome.warnings == (CATALOG_UNAVAILABLE,)
    assert outcome.index_kind == "unavailable"
    assert outcome.channels == ("keyword",)


@pytest.mark.asyncio
async def test_an_embedding_search_without_an_embedder_is_unavailable() -> None:
    record = _record()
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record])

    outcome = await _joined_service(
        index=index, store=store, embedder=None
    ).search(task_query="github code", user_id="u1")

    assert index.calls == []
    assert store.calls == []
    assert outcome.hits == ()
    assert outcome.warnings == (CATALOG_UNAVAILABLE,)
    assert outcome.channels == ("embedding", "keyword")


@pytest.mark.asyncio
async def test_a_config_with_no_recall_channel_is_unavailable() -> None:
    """The config model refuses this; a hand-built service must still say so."""
    record = _record()
    index = JoinedIndex(_hits((record, 0.9)))
    store = JoinedStore([record])
    config = ToolSearchConfig(
        candidate_k=10, top_k=5, embedding=EMBEDDING, keyword={"enabled": True}
    )
    config.keyword.enabled = False
    config.embedding.enabled = False
    service = ContextForgeToolSearchService(
        config=config, index=index, store=store, embedder=JoinedEmbedder()
    )

    outcome = await service.search(task_query="github code", user_id="u1")

    assert index.calls == []
    assert store.calls == []
    assert outcome.hits == ()
    assert outcome.warnings == (CATALOG_UNAVAILABLE,)
    assert outcome.index_kind == "unavailable"
    assert outcome.channels == ()


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
