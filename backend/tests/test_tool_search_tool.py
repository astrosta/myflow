"""tool_search: DB-joined tool cards, owner scoping, budgets, and state writes.

The Milvus collection is derived, so a hit is only a `tool_id` plus a score:
these tests pin the database join that turns it into a card (verbatim
`input_schema`, owner and callability confirmed by the store), the output
budget (max 5 cards, 3000-char schemas, 16000-char payload), and the
`retrieved_tool_names` state write: only a real answer writes that key - an
empty one clears the previous run's names - while a blank query, a missing
service, a service exception or an unavailable retrieval omits it, so an
earlier search's names survive for tool_execute.  A retrieval failure never
falls back to a catalog-wide scan.  The recall channel follows the
embedding/keyword flags, and both hit shapes (card and legacy snapshot entry)
produce a card.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.thread_state import merge_retrieved_tool_names
from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.index import ToolVectorHit
from agentkit.contextforge.runtime import (
    build_contextforge_runtime,
    reset_contextforge_runtime,
)
from agentkit.contextforge.store import (
    ContextForgeToolRecord,
    ContextForgeToolStore,
    call_name_for,
)
from agentkit.persistence.schema import Base
from agentkit.runtime.user_context import reset_current_user, set_current_user
from agentkit.tools.builtin.tool_search import TOOL_NAME, tool_search
from agentkit.tools.search.service import (
    CATALOG_UNAVAILABLE,
    MAX_RESULT_CHARS,
    MAX_RETRIEVED_TOOLS,
    ContextForgeToolSearchService,
    ToolSearchCard,
    ToolSearchOutcome,
    get_tool_search_runtime,
    reset_tool_search_runtime,
    set_tool_search_runtime,
)

EMBEDDING = {"base_url": "http://embed", "dimensions": 8}

#: A schema with every shape a Pydantic round-trip would rewrite plus the keys a
#: truncated card is expected to keep; tool_search must return it verbatim.
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
async def catalog(tmp_path: Path) -> AsyncIterator[ContextForgeToolStore]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "contextforge.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield ContextForgeToolStore(
            async_sessionmaker(engine, expire_on_commit=False)
        )
    finally:
        await engine.dispose()


def _record(
    name: str = "github_search",
    *,
    owner: str = "u1",
    schema: dict[str, Any] | None = None,
    description: str = "Search GitHub code",
) -> ContextForgeToolRecord:
    return ContextForgeToolRecord(
        id="tool-" + name,
        name=name,
        call_name=call_name_for(name),
        display_name=name,
        description=description,
        input_schema=dict(VERBATIM_SCHEMA) if schema is None else schema,
        tags=("github", "code"),
        parameters=("query",),
        owner_user_id=owner,
        enabled=True,
        reachable=True,
        callable=True,
        content=name + " " + description,
    )


async def _store_catalog(
    store: ContextForgeToolStore, records: Sequence[ContextForgeToolRecord]
) -> None:
    await store.upsert_snapshot(list(records), [record.name for record in records])


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
            {
                "channel": "dense",
                "owner_user_id": owner_user_id,
                "limit": limit,
            }
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


#: "leave the embedder as the fake one"; None means "no embedder at all".
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
) -> ContextForgeToolSearchService:
    from agentkit.config.contextforge_config import ToolSearchConfig

    return ContextForgeToolSearchService(
        config=ToolSearchConfig(
            candidate_k=max(top_k, 10),
            top_k=top_k,
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
    task_query: str = "search github code",
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


@pytest.mark.asyncio
async def test_tool_search_returns_db_schemas_for_milvus_hits(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])
        index = HitIndex([ToolVectorHit("github_search", 0.9, tool_id="tool-github_search")])

        update = await _run(_service(store=store, index=index))
        assert index.calls[0]["owner_user_id"] == "u1"

    assert update["retrieved_tool_names"] == ["cf__github_search"]
    payload = _payload(update)
    card = payload["tools"][0]
    assert card["tool_id"] == "tool-github_search"
    assert card["tool_name"] == "github_search"
    assert card["call_name"] == "cf__github_search"
    assert card["description"] == "Search GitHub code"
    assert card["tags"] == ["github", "code"]
    assert card["parameters"] == ["query"]
    assert card["input_schema"] == VERBATIM_SCHEMA
    assert card["schema_truncated"] is False
    assert card["score"] > 0
    assert payload["retrieval"]["index"] == "milvus"
    assert payload["retrieval"]["channels"] == ["embedding", "keyword"]
    assert "tool_execute" in payload["message"]


@pytest.mark.asyncio
async def test_tool_search_never_returns_another_users_tool(tmp_path: Path) -> None:
    own = _record("github_search", owner="u1")
    other = _record("gitlab_search", owner="u2")
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [own, other])
        index = HitIndex(
            [
                ToolVectorHit("gitlab_search", 0.9, tool_id=other.id),
                ToolVectorHit("github_search", 0.8, tool_id=own.id),
            ]
        )

        update = await _run(_service(store=store, index=index))

    payload = _payload(update)
    assert [card["tool_name"] for card in payload["tools"]] == ["github_search"]
    assert update["retrieved_tool_names"] == ["cf__github_search"]


@pytest.mark.asyncio
async def test_tool_search_drops_hits_the_catalog_does_not_confirm(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])
        index = HitIndex(
            [
                ToolVectorHit("github_search", 0.9, tool_id="tool-github_search"),
                ToolVectorHit("stale_tool", 0.8, tool_id="tool-stale_tool"),
            ]
        )

        update = await _run(_service(store=store, index=index))

    payload = _payload(update)
    assert [card["call_name"] for card in payload["tools"]] == ["cf__github_search"]
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
    """Design 13: an unavailable index must not disarm the tools already found.

    Writing [] here would clear the names of the last successful search, and
    tool_execute would then refuse a tool the model was already shown.
    """
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])
        index = HitIndex(error=RuntimeError("milvus down"))

        update = await _run(_service(store=store, index=index))

    assert "retrieved_tool_names" not in update
    assert merge_retrieved_tool_names(
        ["cf__github_search"], update.get("retrieved_tool_names")
    ) == ["cf__github_search"]
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
        ["cf__github_search"], update.get("retrieved_tool_names")
    ) == ["cf__github_search"]
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
                ToolVectorHit(record.name, 1.0 - position / 100, tool_id=record.id)
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
        call_name_for(f"tool_{position}") for position in range(5)
    ]


@pytest.mark.asyncio
async def test_tool_search_keeps_its_payload_inside_the_result_budget(
    tmp_path: Path,
) -> None:
    # Each schema is bigger than max_schema_chars, so every card carries a
    # 3000-char schema plus a 500-char description: five of those do not fit a
    # 16000-char payload, and the lowest-ranked cards have to go.
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
        await _store_catalog(store, [_record(schema=huge_schema)])
        index = HitIndex([ToolVectorHit("github_search", 0.9, tool_id="tool-github_search")])

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
        task_query="search github code",
        domain_keywords=[],
        tool_call_id="call-1",
    )

    update = result.update
    # No search ran, so the state key is absent and the reducer leaves the
    # names of an earlier successful search alone.
    assert "retrieved_tool_names" not in update
    payload = _payload(update)
    assert payload["tools"] == []
    assert payload["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]


class RecordingStore:
    """A catalog read here would be the full-scan fallback the design forbids."""

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
    assert service.calls[0]["task_query"] == "search github code"


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
        await _store_catalog(store, [_record()])
        index = HitIndex(
            [ToolVectorHit("github_search", 0.9, tool_id="tool-github_search")]
        )

        update = await _run(_service(store=store, index=index, keyword=False))

    assert [call["channel"] for call in index.calls] == ["dense"]
    payload = _payload(update)
    assert payload["retrieval"]["channels"] == ["embedding"]
    assert payload["retrieval"]["index"] == "milvus"
    assert update["retrieved_tool_names"] == ["cf__github_search"]


@pytest.mark.asyncio
async def test_keyword_only_config_uses_the_sparse_channel(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])
        index = HitIndex(
            [ToolVectorHit("github_search", 0.9, tool_id="tool-github_search")]
        )
        service = _service(
            store=store, index=index, embedding=False, embedder=None
        )

        update = await _run(service, domain_keywords=["github"])

    # No embedding is required: the sparse channel is the whole search.
    assert [call["channel"] for call in index.calls] == ["keyword"]
    assert index.calls[0]["owner_user_id"] == "u1"
    assert index.calls[0]["query"] == "search github code github"
    payload = _payload(update)
    assert payload["retrieval"]["channels"] == ["keyword"]
    assert payload["retrieval"]["index"] == "milvus"
    assert payload["tools"][0]["call_name"] == "cf__github_search"
    assert update["retrieved_tool_names"] == ["cf__github_search"]


@pytest.mark.asyncio
async def test_an_embedding_search_without_an_embedder_is_unavailable(
    tmp_path: Path,
) -> None:
    index = HitIndex([ToolVectorHit("github_search", 0.9, tool_id="tool-github_search")])
    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])

        update = await _run(_service(store=store, index=index, embedder=None))

    assert index.calls == []
    assert "retrieved_tool_names" not in update
    payload = _payload(update)
    assert payload["tools"] == []
    assert payload["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]


@pytest.mark.asyncio
async def test_a_keyword_only_config_without_keyword_search_is_unavailable(
    tmp_path: Path,
) -> None:
    class NoSparseIndex:
        async def dense_search(self, *, vector, owner_user_id, limit):  # pragma: no cover
            raise AssertionError("embedding is disabled")

        async def hybrid_search(self, *, vector, query, owner_user_id, limit):  # pragma: no cover
            raise AssertionError("embedding is disabled")

    async with catalog(tmp_path) as store:
        await _store_catalog(store, [_record()])

        update = await _run(
            _service(store=store, index=NoSparseIndex(), embedding=False, embedder=None)
        )

    assert "retrieved_tool_names" not in update
    assert _payload(update)["retrieval"]["warnings"] == [CATALOG_UNAVAILABLE]


def _enabled_config() -> ContextForgeConfig:
    return ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="adm",
        search={
            "enabled": True,
            "embedding": EMBEDDING,
            "vector_store": {"uri": "http://milvus:19530"},
        },
    )


def test_build_contextforge_runtime_installs_the_tool_search_service(
    tmp_path: Path,
) -> None:
    async def _build() -> Any:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "wiring.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            return (
                build_contextforge_runtime(
                    _enabled_config(),
                    session_factory=async_sessionmaker(engine, expire_on_commit=False),
                ),
                engine,
            )
        except BaseException:  # pragma: no cover - cleanup on a failed build
            await engine.dispose()
            raise

    runtime, engine = asyncio.run(_build())
    try:
        assert runtime is not None
        service = get_tool_search_runtime()
        assert isinstance(service, ContextForgeToolSearchService)
        assert service.max_result_chars == MAX_RESULT_CHARS
    finally:
        reset_tool_search_runtime()
        reset_contextforge_runtime()
        assert runtime is not None
        asyncio.run(runtime.admin.aclose())
        asyncio.run(engine.dispose())


def test_a_disabled_runtime_clears_the_tool_search_service() -> None:
    set_tool_search_runtime(RecordingService())  # type: ignore[arg-type]

    assert build_contextforge_runtime(ContextForgeConfig()) is None
    assert get_tool_search_runtime() is None
