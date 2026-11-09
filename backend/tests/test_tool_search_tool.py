"""tool_search: MCP tool cards, owner scoping, budgets, and state writes.

A Milvus hit is only a tool id plus a score. These tests pin the database join
that confirms the row (owner + callable) and turns it into a card whose
server_name and call_name (mcp__server__tool) are the MCP identity, plus the
output budget and the retrieved_tool_names state write: only a real answer
writes that key, while an unavailable retrieval leaves earlier names for
tool_execute.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.thread_state import merge_retrieved_tool_names
from agentkit.config.tool_search_config import ToolSearchConfig
from agentkit.mcp.crypto import HeaderCipher
from agentkit.mcp.index import ToolVectorHit
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.runtime import McpServerSyncResult
from agentkit.mcp.service import McpServerService, ServerUrlPolicy
from agentkit.mcp.store import McpServerStore
from agentkit.mcp.store.tool_repository import (
    McpToolRecord,
    McpToolStore,
    call_name_for,
    tool_id_for,
)
from agentkit.persistence.schema import Base
from agentkit.runtime.user_context import reset_current_user, set_current_user
from agentkit.tools.builtin.tool_search import TOOL_NAME, tool_search
from agentkit.tools.search.service import (
    CATALOG_UNAVAILABLE,
    MAX_RESULT_CHARS,
    MAX_RETRIEVED_TOOLS,
    McpToolSearchService,
    ToolSearchCard,
    ToolSearchOutcome,
    get_tool_search_runtime,
    reset_tool_search_runtime,
    set_tool_search_runtime,
)

EMBEDDING = {"base_url": "http://embed", "dimensions": 8}

VERBATIM_SCHEMA: dict[str, Any] = {
    "$defs": {
        "Filter": {
            "type": "object",
            "properties": {"q": {"type": "string", "default": "all"}},
        }
    },
    "type": "object",
    "properties": {
        "query": {"$ref": "#/$defs/Filter"},
        "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 10},
    },
    "required": ["query"],
}


@pytest.fixture(autouse=True)
def _reset_tool_search() -> Any:
    reset_tool_search_runtime()
    yield
    reset_tool_search_runtime()


@asynccontextmanager
async def catalog(tmp_path: Path) -> AsyncIterator[McpToolStore]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield McpToolStore(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _record(
    name: str = "search",
    *,
    server_name: str = "anysearch",
    owner: str = "u1",
    schema: dict[str, Any] | None = None,
    description: str = "Search the web",
    callable_: bool = True,
) -> McpToolRecord:
    server_id = "server-" + server_name
    return McpToolRecord(
        id=tool_id_for(server_id, name),
        server_id=server_id,
        server_name=server_name,
        owner_user_id=owner,
        name=name,
        call_name=call_name_for(server_name, name),
        description=description,
        input_schema=dict(VERBATIM_SCHEMA) if schema is None else schema,
        parameters=("query",),
        enabled=True,
        reachable=True,
        callable=callable_,
        content=f"{server_name} {name} {description}",
    )


async def _store_catalog(
    store: McpToolStore, records: Sequence[McpToolRecord]
) -> None:
    """Store the tools behind their server rows, as a real sync would.

    tool_search now joins mcp_servers for the enabled flag, so a tool without its
    owning server is exactly the orphan the join must reject.
    """
    servers = McpServerStore(store._sf)
    for record in records:
        if await servers.get(record.server_id, record.owner_user_id) is not None:
            continue
        await servers.create(
            McpServerRecord(
                id=record.server_id,
                owner_user_id=record.owner_user_id,
                name=record.server_name,
                url="https://mcp.example.test/mcp",
            )
        )
    await store.upsert_snapshot(list(records), [record.id for record in records])


class HitIndex:
    """The derived Milvus view: tool ids and scores, never a schema."""

    def __init__(
        self,
        hits: Sequence[ToolVectorHit] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self._hits = list(hits)
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def hybrid_search(self, *, vector, query, owner_user_id, limit):
        self.calls.append(
            {
                "channel": "hybrid",
                "query": query,
                "owner_user_id": owner_user_id,
                "limit": limit,
            }
        )
        if self._error is not None:
            raise self._error
        return self._hits[:limit]

    async def dense_search(self, *, vector, owner_user_id, limit):
        self.calls.append(
            {"channel": "dense", "owner_user_id": owner_user_id, "limit": limit}
        )
        if self._error is not None:
            raise self._error
        return self._hits[:limit]

    async def keyword_search(self, *, query, owner_user_id, limit):
        self.calls.append(
            {
                "channel": "keyword",
                "query": query,
                "owner_user_id": owner_user_id,
                "limit": limit,
            }
        )
        if self._error is not None:
            raise self._error
        return self._hits[:limit]


class FakeEmbedder:
    async def embed_query(self, text: str) -> list[float]:
        return [0.0] * 8


class ReverseReranker:
    async def rerank(self, *, query, documents):
        return [
            type("Score", (), {"index": position})
            for position in reversed(range(len(documents)))
        ]


class BrokenReranker:
    async def rerank(self, *, query, documents):
        raise RuntimeError("rerank down")


_DEFAULT_EMBEDDER = object()


def _service(
    *,
    store: Any,
    index: Any,
    top_k: int = 5,
    reranker: Any = None,
    embedding: bool = True,
    keyword: bool = True,
    embedder: Any = _DEFAULT_EMBEDDER,
    min_score: float | None = None,
) -> McpToolSearchService:
    return McpToolSearchService(
        config=ToolSearchConfig(
            candidate_k=max(top_k, 10),
            top_k=top_k,
            min_score=min_score,
            embedding=EMBEDDING if embedding else {"enabled": False},
            keyword={"enabled": keyword},
        ),
        index=index,
        store=store,
        embedder=FakeEmbedder() if embedder is _DEFAULT_EMBEDDER else embedder,
        reranker=reranker,
    )


def _runtime(user_id: str | None = "u1") -> Any:
    context = {"user_id": user_id} if user_id else {}
    return type("Runtime", (), {"context": context})()


async def _run(
    service: Any,
    *,
    task_query: str = "search the web",
    domain_keywords: Sequence[str] = (),
    runtime: Any = None,
    tool_call_id: str = "call-1",
) -> dict[str, Any]:
    set_tool_search_runtime(service)
    try:
        result = await tool_search.coroutine(
            task_query=task_query,
            domain_keywords=list(domain_keywords),
            runtime=runtime if runtime is not None else _runtime(),
            tool_call_id=tool_call_id,
        )
    finally:
        reset_tool_search_runtime()
    assert hasattr(result, "update")
    return result.update


def _payload(update: dict[str, Any]) -> dict[str, Any]:
    message = update["messages"][0]
    assert isinstance(message, ToolMessage)
    assert message.name == TOOL_NAME
    return json.loads(message.content)


def test_the_service_accepts_the_documented_constructor_order() -> None:
    config = ToolSearchConfig()
    service = McpToolSearchService(config, None, None, None, None)

    assert service.max_cards == 5


def test_the_tool_description_speaks_about_extended_tools() -> None:
    description = tool_search.description or ""

    assert "extended tools" in description
    assert "MCP" not in description


class FailingRetirementSync:
    """A retirement pass that either raises or reports its failure in the result."""

    def __init__(self, *, raises: bool) -> None:
        self.raises = raises
        self.calls: list[tuple[str, str | None]] = []

    async def sync_server(self, server_id: str, reason: str | None = None) -> Any:
        self.calls.append((server_id, reason))
        if self.raises:
            raise RuntimeError("catalog is down")
        return McpServerSyncResult(server_id=server_id, error="catalog is down")


@pytest.mark.parametrize("failure", ["raises", "result_error"])
@pytest.mark.asyncio
async def test_a_disabled_server_is_not_searchable_when_retirement_fails(
    tmp_path: Path, failure: str
) -> None:
    """The DB join, not a successful sync, is what retires a disabled server.

    Both shapes of a failed retirement leave mcp_tools.callable=True, so a stale
    Milvus id can still arrive.  tool_search must drop it because the owning server
    is disabled, otherwise the model is offered a tool it cannot execute.
    """
    async with catalog(tmp_path) as tools:
        servers = McpServerStore(tools._sf)
        server = await servers.create(
            McpServerRecord(
                id="server-anysearch",
                owner_user_id="u1",
                name="anysearch",
                url="https://mcp.example.test/mcp",
            )
        )
        record = _record()
        await _store_catalog(tools, [record])
        sync = FailingRetirementSync(raises=failure == "raises")
        service = McpServerService(
            store=servers,
            cipher=HeaderCipher(HeaderCipher.generate_key()),
            client=object(),
            sync=sync,
            tool_store=tools,
            policy=ServerUrlPolicy(),
        )

        disabled = await service.set_enabled(server.id, "u1", False)

        assert disabled.enabled is False
        assert sync.calls == [(server.id, "server_disabled")]
        # The retirement did not rewrite the row; the join has to do the work.
        assert (await tools.list_for_server(server.id, "u1"))[0].callable is True
        index = HitIndex([ToolVectorHit("search", 0.9, tool_id=record.id)])
        update = await _run(_service(store=tools, index=index))

    assert _payload(update)["tools"] == []
    assert update["retrieved_tool_names"] == []


@pytest.mark.asyncio
async def test_card_uses_mcp_namespace_and_server_name(tmp_path: Path) -> None:
    record = _record()
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [record])
        index = HitIndex(
            [
                ToolVectorHit(
                    "search",
                    0.9,
                    tool_id=record.id,
                    server_id=record.server_id,
                    server_name=record.server_name,
                )
            ]
        )

        outcome = await _service(store=store, index=index).search(
            task_query="search the web", user_id="u1"
        )

    assert outcome.hits[0].call_name == "mcp__anysearch__search"
    assert outcome.hits[0].server_name == "anysearch"
    assert outcome.hits[0].tool_name == "search"
    assert isinstance(outcome.hits[0], ToolSearchCard)


@pytest.mark.asyncio
async def test_tool_search_returns_db_schemas_and_server_aware_cards(
    tmp_path: Path,
) -> None:
    record = _record()
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [record])
        index = HitIndex([ToolVectorHit("search", 0.9, tool_id=record.id)])

        update = await _run(_service(store=store, index=index))
        assert index.calls[0]["owner_user_id"] == "u1"

    assert update["retrieved_tool_names"] == ["mcp__anysearch__search"]
    payload = _payload(update)
    card = payload["tools"][0]
    assert card["tool_id"] == record.id
    assert card["tool_name"] == "search"
    assert card["server_name"] == "anysearch"
    assert card["call_name"] == "mcp__anysearch__search"
    assert card["description"] == "Search the web"
    # McpToolRecord has no tags; the card carries the DB row's empty default.
    assert card["tags"] == []
    assert card["parameters"] == ["query"]
    assert card["input_schema"] == VERBATIM_SCHEMA
    assert card["schema_truncated"] is False
    assert card["score"] > 0
    assert payload["retrieval"]["index"] == "milvus"
    assert payload["retrieval"]["channels"] == ["embedding", "keyword"]
    assert "tool_execute" in payload["message"]


@pytest.mark.asyncio
async def test_tool_search_never_returns_another_users_tool(tmp_path: Path) -> None:
    own = _record("search", owner="u1")
    other = _record("search", server_name="other", owner="u2")
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [own, other])
        index = HitIndex(
            [
                ToolVectorHit("search", 0.9, tool_id=other.id),
                ToolVectorHit("search", 0.8, tool_id=own.id),
            ]
        )

        update = await _run(_service(store=store, index=index))

    payload = _payload(update)
    assert [card["call_name"] for card in payload["tools"]] == [
        "mcp__anysearch__search"
    ]
    assert payload["tools"][0]["server_name"] == "anysearch"
    assert update["retrieved_tool_names"] == ["mcp__anysearch__search"]


@pytest.mark.asyncio
async def test_tool_search_drops_hits_the_store_does_not_confirm(
    tmp_path: Path,
) -> None:
    retired = _record("retired", callable_=False)
    async with catalog(tmp_path) as store:
        servers = McpServerStore(store._sf)
        await servers.create(
            McpServerRecord(
                id="server-anysearch",
                owner_user_id="u1",
                name="anysearch",
                url="https://mcp.example.test/mcp",
            )
        )
        # The retired tool exists but was not seen upstream, so the store keeps
        # the row non-callable; a stale Milvus id must not revive it.
        await store.upsert_snapshot([_record(), retired], [_record().id])
        index = HitIndex(
            [
                ToolVectorHit("search", 0.9, tool_id=_record().id),
                ToolVectorHit("retired", 0.8, tool_id=retired.id),
                ToolVectorHit("stale", 0.7, tool_id="stale-id"),
            ]
        )

        update = await _run(_service(store=store, index=index))

    payload = _payload(update)
    assert [card["call_name"] for card in payload["tools"]] == [
        "mcp__anysearch__search"
    ]
    assert payload["retrieval"]["candidates"] == 1


@pytest.mark.asyncio
async def test_empty_result_clears_retrieved_tool_names(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])
        index = HitIndex([])

        update = await _run(_service(store=store, index=index))

    assert update["retrieved_tool_names"] == []
    payload = _payload(update)
    assert payload["tools"] == []
    assert "No matching tool" in payload["message"]


@pytest.mark.asyncio
async def test_a_retrieval_failure_leaves_the_retrieved_names_untouched(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])
        index = HitIndex(error=RuntimeError("milvus down"))

        update = await _run(_service(store=store, index=index))

    assert "retrieved_tool_names" not in update
    assert merge_retrieved_tool_names(
        ["mcp__anysearch__search"], update.get("retrieved_tool_names")
    ) == ["mcp__anysearch__search"]
    payload = _payload(update)
    assert payload["tools"] == []
    assert payload["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]
    assert "tool_execute" in payload["message"]


class RaisingService:
    async def search(self, *, task_query, domain_keywords=(), user_id=None):
        raise RuntimeError("retrieval exploded")


@pytest.mark.asyncio
async def test_a_service_exception_leaves_the_retrieved_names_untouched() -> None:
    update = await _run(RaisingService())

    assert "retrieved_tool_names" not in update
    assert merge_retrieved_tool_names(
        ["mcp__anysearch__search"], update.get("retrieved_tool_names")
    ) == ["mcp__anysearch__search"]
    assert _payload(update)["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]


@pytest.mark.asyncio
async def test_tool_search_caps_cards_and_retrieved_names_at_five(
    tmp_path: Path,
) -> None:
    records = [
        _record(f"tool_{position}", description=f"tool number {position}")
        for position in range(8)
    ]
    async with catalog(tmp_path) as store:
        await _store_catalog(store, records)
        index = HitIndex(
            [
                ToolVectorHit(
                    record.name, 1.0 - position / 100, tool_id=record.id
                )
                for position, record in enumerate(records)
            ]
        )

        update = await _run(_service(store=store, index=index, top_k=8))

    payload = _payload(update)
    assert MAX_RETRIEVED_TOOLS == 5
    assert len(payload["tools"]) == 5
    assert [tool["tool_name"] for tool in payload["tools"]] == [
        f"tool_{position}" for position in range(5)
    ]
    assert update["retrieved_tool_names"] == [
        call_name_for("anysearch", f"tool_{position}") for position in range(5)
    ]


@pytest.mark.asyncio
async def test_tool_search_keeps_its_payload_inside_the_result_budget(
    tmp_path: Path,
) -> None:
    fat_schema = {
        "type": "object",
        "properties": {
            f"p{position:03d}": {"type": "string"} for position in range(160)
        },
        "required": ["p000"],
    }
    records = [
        _record(f"tool_{position}", schema=fat_schema, description="d" * 900)
        for position in range(5)
    ]
    async with catalog(tmp_path) as store:
        await _store_catalog(store, records)
        index = HitIndex(
            [
                ToolVectorHit(record.name, 1.0 - position / 100, tool_id=record.id)
                for position, record in enumerate(records)
            ]
        )

        update = await _run(_service(store=store, index=index))

    message = update["messages"][0]
    assert len(message.content) <= MAX_RESULT_CHARS == 16000
    payload = _payload(update)
    assert 1 <= len(payload["tools"]) < 5
    assert update["retrieved_tool_names"] == [
        tool["call_name"] for tool in payload["tools"]
    ]
    assert "budget" in payload["message"]


@pytest.mark.asyncio
async def test_tool_search_truncates_an_oversized_schema_to_valid_json(
    tmp_path: Path,
) -> None:
    huge_schema = {
        "$defs": {"Filter": {"type": "string", "description": "x" * 2000}},
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "y" * 5000, "examples": ["a"]},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query", "limit"],
        "examples": [{"query": "z" * 1000}],
    }
    async with catalog(tmp_path) as store:
        record = _record(schema=huge_schema)
        await _store_catalog(store, [record])
        index = HitIndex([ToolVectorHit("search", 0.9, tool_id=record.id)])

        update = await _run(_service(store=store, index=index))

    card = _payload(update)["tools"][0]
    schema = card["input_schema"]
    assert card["schema_truncated"] is True
    assert len(json.dumps(schema, ensure_ascii=False)) <= 3000
    assert schema["type"] == "object"
    assert schema["required"] == ["query", "limit"]
    assert schema["properties"]["query"] == {"type": "string"}
    assert schema["properties"]["limit"] == {"type": "integer", "default": 10}
    assert json.loads(json.dumps(schema)) == schema


@pytest.mark.asyncio
async def test_tool_search_without_a_runtime_reports_catalog_unavailable() -> None:
    reset_tool_search_runtime()

    result = await tool_search.coroutine(
        task_query="search the web",
        domain_keywords=[],
        tool_call_id="call-1",
    )

    update = result.update
    assert "retrieved_tool_names" not in update
    payload = _payload(update)
    assert payload["tools"] == []
    assert payload["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    async def get_by_ids(self, ids, owner_user_id):
        self.calls.append((list(ids), owner_user_id))
        raise AssertionError("tool_search must not read the catalog when Milvus is down")


@pytest.mark.asyncio
async def test_tool_search_never_falls_back_to_a_catalog_scan() -> None:
    store = RecordingStore()
    index = HitIndex(error=RuntimeError("milvus down"))

    update = await _run(_service(store=store, index=index))

    assert store.calls == []
    payload = _payload(update)
    assert payload["tools"] == []
    assert payload["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]
    assert "retrieved_tool_names" not in update


class RecordingService:
    def __init__(self, outcome: ToolSearchOutcome | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcome = outcome if outcome is not None else ToolSearchOutcome()

    async def search(self, *, task_query, domain_keywords=(), user_id=None):
        self.calls.append(
            {
                "task_query": task_query,
                "domain_keywords": list(domain_keywords),
                "user_id": user_id,
            }
        )
        return self.outcome


@pytest.mark.asyncio
async def test_tool_search_passes_the_runtime_user_to_the_service() -> None:
    service = RecordingService()

    await _run(service, runtime=_runtime("u1"))

    assert service.calls[0]["user_id"] == "u1"
    assert service.calls[0]["task_query"] == "search the web"


@pytest.mark.asyncio
async def test_tool_search_falls_back_to_the_default_user() -> None:
    service = RecordingService()
    token = set_current_user(None)
    try:
        await _run(service, runtime=_runtime(None))
    finally:
        reset_current_user(token)

    assert service.calls[0]["user_id"] == "default"


@pytest.mark.asyncio
async def test_tool_search_rejects_a_blank_query_without_searching() -> None:
    service = RecordingService()

    update = await _run(service, task_query="   ")

    assert service.calls == []
    payload = _payload(update)
    assert payload["tools"] == []
    assert "task_query" in payload["message"]
    assert "retrieved_tool_names" not in update


@pytest.mark.asyncio
async def test_embedding_only_config_uses_the_dense_channel(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        record = _record()
        await _store_catalog(store, [record])
        index = HitIndex([ToolVectorHit("search", 0.9, tool_id=record.id)])

        update = await _run(_service(store=store, index=index, keyword=False))

    assert [call["channel"] for call in index.calls] == ["dense"]
    payload = _payload(update)
    assert payload["retrieval"]["channels"] == ["embedding"]
    assert payload["retrieval"]["index"] == "milvus"
    assert update["retrieved_tool_names"] == ["mcp__anysearch__search"]


@pytest.mark.asyncio
async def test_keyword_only_config_uses_the_sparse_channel(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        record = _record()
        await _store_catalog(store, [record])
        index = HitIndex([ToolVectorHit("search", 0.9, tool_id=record.id)])
        service = _service(
            store=store, index=index, embedding=False, embedder=None
        )

        update = await _run(service, domain_keywords=["web"])

    assert [call["channel"] for call in index.calls] == ["keyword"]
    assert index.calls[0]["owner_user_id"] == "u1"
    assert index.calls[0]["query"] == "search the web web"
    payload = _payload(update)
    assert payload["retrieval"]["channels"] == ["keyword"]
    assert payload["retrieval"]["index"] == "milvus"
    assert payload["tools"][0]["call_name"] == "mcp__anysearch__search"
    assert update["retrieved_tool_names"] == ["mcp__anysearch__search"]


@pytest.mark.asyncio
async def test_an_embedding_search_without_an_embedder_is_unavailable(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        record = _record()
        await _store_catalog(store, [record])
        index = HitIndex([ToolVectorHit("search", 0.9, tool_id=record.id)])

        update = await _run(_service(store=store, index=index, embedder=None))

    assert index.calls == []
    assert "retrieved_tool_names" not in update
    payload = _payload(update)
    assert payload["tools"] == []
    assert payload["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]


@pytest.mark.asyncio
async def test_rerank_reorders_the_confirmed_cards() -> None:
    first, second = _record("first"), _record("second")
    index = HitIndex(
        [
            ToolVectorHit("first", 0.9, tool_id=first.id),
            ToolVectorHit("second", 0.8, tool_id=second.id),
        ]
    )

    class Store:
        async def get_by_ids(self, ids, owner_user_id):
            return [first, second]

    outcome = await _service(
        store=Store(), index=index, reranker=ReverseReranker()
    ).search(task_query="search the web", user_id="u1")

    assert outcome.reranked is True
    assert [hit.tool_name for hit in outcome.hits] == ["second", "first"]


@pytest.mark.asyncio
async def test_a_rerank_failure_keeps_the_fused_order() -> None:
    first, second = _record("first"), _record("second")
    index = HitIndex(
        [
            ToolVectorHit("first", 0.9, tool_id=first.id),
            ToolVectorHit("second", 0.8, tool_id=second.id),
        ]
    )

    class Store:
        async def get_by_ids(self, ids, owner_user_id):
            return [first, second]

    outcome = await _service(
        store=Store(), index=index, reranker=BrokenReranker()
    ).search(task_query="search the web", user_id="u1")

    assert outcome.reranked is False
    assert outcome.warnings == ("rerank_unavailable",)
    assert [hit.tool_name for hit in outcome.hits] == ["first", "second"]


@pytest.mark.asyncio
async def test_min_score_filters_normalized_scores() -> None:
    strong, weak = _record("strong"), _record("weak")
    index = HitIndex(
        [
            ToolVectorHit("strong", 0.9, tool_id=strong.id),
            ToolVectorHit("weak", 0.3, tool_id=weak.id),
        ]
    )

    class Store:
        async def get_by_ids(self, ids, owner_user_id):
            return [strong, weak]

    outcome = await _service(
        store=Store(), index=index, min_score=0.6
    ).search(task_query="search the web", user_id="u1")

    assert [hit.tool_name for hit in outcome.hits] == ["strong"]


@pytest.mark.asyncio
async def test_an_empty_query_or_missing_owner_never_searches() -> None:
    record = _record()
    index = HitIndex([ToolVectorHit("search", 0.9, tool_id=record.id)])

    class Store:
        async def get_by_ids(self, ids, owner_user_id):
            return [record]

    service = _service(store=Store(), index=index)

    assert (await service.search(task_query="   ", user_id="u1")).hits == ()
    assert (await service.search(task_query="search the web", user_id=None)).hits == ()

    assert index.calls == []


def test_get_tool_search_runtime_is_empty_by_default() -> None:
    assert get_tool_search_runtime() is None
