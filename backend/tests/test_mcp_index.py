"""Contract tests for the derived MCP tool index.

The collection is a derived view of mcp_tools: it holds retrieval fields only,
keyed by the tool id, and every search is owner-scoped on both recall channels.
The server identity rides along so a card can say which MCP server owns a tool
and the call namespace is unambiguous.
"""

from __future__ import annotations

import json

import pytest
from pymilvus import DataType, FunctionType

from agentkit.config.tool_search_config import (
    MCP_TOOL_COLLECTION,
    ToolKeywordConfig,
    ToolVectorStoreConfig,
)
from agentkit.mcp.index import (
    COLLECTION_NAME,
    OUTPUT_FIELDS,
    McpToolVectorIndex,
    ToolIndexUnavailable,
)
from agentkit.mcp.store.tool_repository import (
    McpToolRecord,
    call_name_for,
    content_hash_for,
    tool_id_for,
)

#: Everything hybrid retrieval needs. input_schema/display_name stay in the DB.
REQUIRED_FIELDS = [
    "tool_id",
    "server_id",
    "server_name",
    "owner_user_id",
    "tool_name",
    "call_name",
    "description",
    "tags",
    "parameters",
    "content",
    "content_hash",
]


def _record(**overrides) -> McpToolRecord:
    server_id = overrides.pop("server_id", "server-1")
    server_name = overrides.pop("server_name", "anysearch")
    name = overrides.pop("name", "search")
    owner = overrides.pop("owner_user_id", "u1")
    content = overrides.pop("content", "search AnySearch web search")
    call_name = overrides.pop("call_name", call_name_for(server_name, name))
    values = {
        "id": tool_id_for(server_id, name),
        "server_id": server_id,
        "server_name": server_name,
        "name": name,
        "call_name": call_name,
        "owner_user_id": owner,
        "description": "search the web",
        "parameters": ("query",),
        "enabled": True,
        "reachable": True,
        "callable": True,
        "content": content,
        "content_hash": content_hash_for(content, owner, call_name),
    }
    values.update(overrides)
    return McpToolRecord(**values)


def _index(client, *, dimensions: int = 4, **kwargs) -> McpToolVectorIndex:
    return McpToolVectorIndex(
        ToolVectorStoreConfig(uri="http://milvus"),
        dimensions=dimensions,
        client=client,
        **kwargs,
    )


class Embedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_documents(self, texts):
        self.texts.extend(texts)
        return [[0.5] * 4 for _ in texts]


class FakeSchema:
    def __init__(self) -> None:
        self.fields: dict[str, dict] = {}
        self.functions: list[object] = []

    def add_field(self, name, dtype, **kwargs) -> None:
        self.fields[name] = {"dtype": dtype, **kwargs}

    def add_function(self, function) -> None:
        self.functions.append(function)


class FakeIndexParams:
    def __init__(self) -> None:
        self.indexes: list[dict] = []

    def add_index(self, **kwargs) -> None:
        self.indexes.append(kwargs)


class FakeIterator:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = list(rows)
        self.closed = False

    def next(self) -> list[dict]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        self.closed = True


def _ids_from_filter(expression: str) -> list[str]:
    marker = " in "
    if marker not in expression:
        return []
    return list(json.loads(expression.split(marker, 1)[1]))


class FakeMilvus:
    """MilvusClient stand-in that records every call the index makes."""

    def __init__(self, *, dim: int = 4, stored: dict[str, str] | None = None) -> None:
        self.dim = dim
        self.stored = dict(stored or {})
        self.collections: set[str] = {COLLECTION_NAME}
        self.created: list[str] = []
        self.loaded: list[str] = []
        self.written: list[dict] = []
        self.deletes: list[str] = []
        self.queries: list[dict] = []
        self.searches: list[dict] = []
        self.hybrid_searches: list[dict] = []
        self.schema = FakeSchema()
        self.index_params = FakeIndexParams()
        self.entity: dict = {
            "tool_id": "tool-1",
            "server_id": "server-1",
            "server_name": "anysearch",
            "tool_name": "search",
            "call_name": "mcp__anysearch__search",
            "owner_user_id": "u1",
            "description": "search the web",
            "tags": json.dumps([]),
            "parameters": json.dumps(["query"]),
            "content": "search AnySearch web search",
            "content_hash": "hash-1",
        }

    def has_collection(self, name: str) -> bool:
        assert name == COLLECTION_NAME, name
        return name in self.collections

    def load_collection(self, name: str) -> None:
        self.loaded.append(name)

    def describe_collection(self, name: str) -> dict:
        assert name == COLLECTION_NAME, name
        fields = [{"name": field} for field in REQUIRED_FIELDS]
        fields.append({"name": "embedding", "params": {"dim": self.dim}})
        fields.append({"name": "sparse"})
        return {"fields": fields}

    def create_schema(self, **kwargs) -> FakeSchema:
        return self.schema

    def prepare_index_params(self) -> FakeIndexParams:
        return self.index_params

    def create_collection(self, collection_name: str, **kwargs) -> None:
        self.created.append(collection_name)
        self.collections.add(collection_name)

    def upsert(self, collection_name: str, data: list[dict]) -> dict:
        assert collection_name == COLLECTION_NAME, collection_name
        self.written.extend(data)
        for row in data:
            self.stored[row["tool_id"]] = row["content_hash"]
        return {"upsert_count": len(data)}

    def delete(self, collection_name: str, filter: str) -> dict:
        assert collection_name == COLLECTION_NAME, collection_name
        self.deletes.append(filter)
        for tool_id in _ids_from_filter(filter):
            self.stored.pop(tool_id, None)
        return {"delete_count": 1}

    def query(self, collection_name: str, **kwargs) -> list[dict]:
        assert collection_name == COLLECTION_NAME, collection_name
        self.queries.append(kwargs)
        wanted = _ids_from_filter(kwargs.get("filter", ""))
        return [
            {"tool_id": tool_id, "content_hash": self.stored[tool_id]}
            for tool_id in wanted
            if tool_id in self.stored
        ]

    def query_iterator(self, **kwargs) -> FakeIterator:
        self.queries.append({"iterator": True, **kwargs})
        return FakeIterator([{"tool_id": tool_id} for tool_id in self.stored])

    def search(self, collection_name: str, **kwargs):
        self.searches.append(kwargs)
        return [[{"entity": dict(self.entity), "distance": 0.7}]]

    def hybrid_search(self, collection_name: str, **kwargs):
        self.hybrid_searches.append(kwargs)
        return [[{"entity": dict(self.entity), "distance": 0.8}]]


class FakeUnsearchableMilvus(FakeMilvus):
    def search(self, collection_name: str, **kwargs):
        raise RuntimeError("milvus down")


class FakeEmptyMilvus(FakeMilvus):
    def __init__(self, *, dim: int = 4) -> None:
        super().__init__(dim=dim)
        self.collections = set()


class FakeLegacyMilvus(FakeMilvus):
    def describe_collection(self, name: str) -> dict:
        return {
            "fields": [
                {"name": "embedding", "params": {"dim": 4}},
                {"name": "sparse"},
            ]
        }


def test_collection_name_is_pinned_to_the_mcp_tool_collection() -> None:
    assert COLLECTION_NAME == MCP_TOOL_COLLECTION == "myflow_mcp_tools"


def test_output_fields_carry_server_identity() -> None:
    assert "server_id" in OUTPUT_FIELDS
    assert "server_name" in OUTPUT_FIELDS
    assert "input_schema" not in OUTPUT_FIELDS


@pytest.mark.asyncio
async def test_existing_collection_is_loaded_by_its_fixed_name() -> None:
    fake = FakeMilvus()

    await _index(fake).ensure_collection(4)

    assert fake.loaded == [COLLECTION_NAME]


@pytest.mark.asyncio
async def test_new_collection_has_the_retrieval_fields_and_indexes() -> None:
    fake = FakeEmptyMilvus()
    index = _index(
        fake,
        dimensions=4,
        keyword=ToolKeywordConfig(analyzer="english", bm25_k1=2.0, bm25_b=0.5),
    )

    await index.ensure_collection(4)

    assert fake.created == [COLLECTION_NAME]
    assert list(fake.schema.fields) == ["pk", *REQUIRED_FIELDS, "embedding", "sparse"]
    assert fake.schema.fields["pk"]["is_primary"] is True
    lengths = {
        "pk": 64,
        "tool_id": 64,
        "server_id": 64,
        "server_name": 128,
        "owner_user_id": 64,
        "tool_name": 255,
        "call_name": 255,
        "description": 65535,
        "tags": 2048,
        "parameters": 2048,
        "content": 65535,
        "content_hash": 64,
    }
    for name, length in lengths.items():
        assert fake.schema.fields[name]["dtype"] == DataType.VARCHAR, name
        assert fake.schema.fields[name]["max_length"] == length, name
    assert fake.schema.fields["content"]["enable_analyzer"] is True
    assert fake.schema.fields["content"]["analyzer_params"] == {"type": "english"}
    assert fake.schema.fields["embedding"]["dtype"] == DataType.FLOAT_VECTOR
    assert fake.schema.fields["embedding"]["dim"] == 4
    assert fake.schema.fields["sparse"]["dtype"] == DataType.SPARSE_FLOAT_VECTOR
    (function,) = fake.schema.functions
    assert function.type == FunctionType.BM25
    assert function.input_field_names == ["content"]
    assert function.output_field_names == ["sparse"]
    dense_index, sparse_index = fake.index_params.indexes
    assert dense_index["field_name"] == "embedding"
    assert dense_index["index_type"] == ToolVectorStoreConfig(uri="http://milvus").index_type
    assert sparse_index["field_name"] == "sparse"
    assert sparse_index["metric_type"] == "BM25"
    assert sparse_index["params"] == {"bm25_k1": 2.0, "bm25_b": 0.5}
    assert "input_schema" not in fake.schema.fields
    assert "display_name" not in fake.schema.fields
    assert "schema_hash" not in fake.schema.fields


@pytest.mark.asyncio
async def test_collection_without_the_retrieval_fields_is_rejected() -> None:
    with pytest.raises(ToolIndexUnavailable, match="recreate"):
        await _index(FakeLegacyMilvus()).ensure_collection(4)


@pytest.mark.asyncio
async def test_hybrid_search_filters_the_owner_on_both_channels() -> None:
    fake = FakeMilvus()

    hits = await _index(fake).hybrid_search(
        vector=[0.0] * 4, query="web search", owner_user_id="u1", limit=5
    )

    assert hits[0].tool_id == "tool-1"
    assert hits[0].server_id == "server-1"
    assert hits[0].server_name == "anysearch"
    assert hits[0].tool_name == "search"
    assert hits[0].call_name == "mcp__anysearch__search"
    assert hits[0].content_hash == "hash-1"
    assert hits[0].parameters == ("query",)
    assert hits[0].score == 0.8
    call = fake.hybrid_searches[0]
    assert "filter" not in call
    assert call["output_fields"] == OUTPUT_FIELDS
    dense, sparse = call["reqs"]
    assert dense.expr == 'owner_user_id == "u1"'
    assert sparse.expr == dense.expr
    assert (dense.anns_field, sparse.anns_field) == ("embedding", "sparse")
    assert dense.param["metric_type"] == ToolVectorStoreConfig(uri="http://milvus").metric_type
    assert sparse.param["metric_type"] == "BM25"
    assert dense.data == [[0.0] * 4]
    assert sparse.data == ["web search"]


@pytest.mark.asyncio
async def test_owner_filter_quotes_the_owner_instead_of_splicing_expressions() -> None:
    fake = FakeMilvus()
    owner = 'u1" or owner_user_id != "u2'

    await _index(fake).hybrid_search(
        vector=[0.0] * 4, query="web", owner_user_id=owner, limit=5
    )

    dense, sparse = fake.hybrid_searches[0]["reqs"]
    assert dense.expr == "owner_user_id == " + json.dumps(owner, ensure_ascii=False)
    assert sparse.expr == dense.expr
    assert json.loads(dense.expr.split(" == ", 1)[1]) == owner


@pytest.mark.asyncio
async def test_search_requires_an_owner_scope() -> None:
    fake = FakeMilvus()
    index = _index(fake)

    with pytest.raises(TypeError):
        await index.hybrid_search(vector=[0.0] * 4, query="web", limit=5)
    with pytest.raises(ValueError, match="owner_user_id"):
        await index.hybrid_search(
            vector=[0.0] * 4, query="web", owner_user_id="  ", limit=5
        )
    with pytest.raises(ValueError, match="owner_user_id"):
        await index.dense_search(vector=[0.0] * 4, owner_user_id=None, limit=5)

    assert fake.hybrid_searches == []
    assert fake.searches == []


@pytest.mark.asyncio
async def test_dense_search_is_owner_scoped_and_returns_server_identity() -> None:
    fake = FakeMilvus()

    hits = await _index(fake).dense_search(
        vector=[0.0] * 4, owner_user_id="u1", limit=3
    )

    assert hits[0].server_name == "anysearch"
    call = fake.searches[0]
    assert call["filter"] == 'owner_user_id == "u1"'
    assert call["anns_field"] == "embedding"
    assert call["output_fields"] == OUTPUT_FIELDS


@pytest.mark.asyncio
async def test_keyword_search_is_sparse_only_and_owner_scoped() -> None:
    fake = FakeMilvus()

    hits = await _index(fake).keyword_search(
        query="web search", owner_user_id="u1", limit=3
    )

    assert hits[0].call_name == "mcp__anysearch__search"
    call = fake.searches[0]
    assert call["anns_field"] == "sparse"
    assert call["data"] == ["web search"]
    assert call["search_params"]["metric_type"] == "BM25"
    assert call["filter"] == 'owner_user_id == "u1"'
    assert call["limit"] == 3
    assert call["output_fields"] == OUTPUT_FIELDS
    assert fake.hybrid_searches == []


@pytest.mark.asyncio
async def test_keyword_search_skips_milvus_for_a_blank_query_or_zero_limit() -> None:
    fake = FakeMilvus()
    index = _index(fake)

    assert await index.keyword_search(query="   ", owner_user_id="u1", limit=5) == []
    assert await index.keyword_search(query="web", owner_user_id="u1", limit=0) == []

    assert fake.searches == []


@pytest.mark.asyncio
async def test_a_keyword_search_failure_is_reported_as_tool_index_unavailable() -> None:
    with pytest.raises(ToolIndexUnavailable, match="keyword search"):
        await _index(FakeUnsearchableMilvus()).keyword_search(
            query="web", owner_user_id="u1", limit=5
        )


@pytest.mark.asyncio
async def test_collection_built_with_another_dimension_is_rejected() -> None:
    fake = FakeMilvus(dim=8)
    index = _index(fake, dimensions=4)

    with pytest.raises(ToolIndexUnavailable, match="dimension mismatch: expected 4, got 8"):
        await index.ensure_collection(4)

    assert fake.created == []
    assert fake.loaded == []


@pytest.mark.asyncio
async def test_hybrid_search_uses_the_configured_rrf_k() -> None:
    fake = FakeMilvus()
    index = _index(fake, rrf_k=37)

    await index.hybrid_search(
        vector=[0.0] * 4, query="web search", owner_user_id="u1", limit=5
    )

    assert fake.hybrid_searches[0]["ranker"]._k == 37


@pytest.mark.asyncio
async def test_hits_ignore_malformed_metadata_and_orphan_rows() -> None:
    fake = FakeMilvus()
    fake.entity["tags"] = "not json"
    fake.entity["parameters"] = ""
    index = _index(fake)

    hits = await index.hybrid_search(
        vector=[0.0] * 4, query="web", owner_user_id="u1", limit=5
    )
    assert hits[0].tags == ()
    assert hits[0].parameters == ()

    fake.entity = {"description": "orphan"}
    assert (
        await index.hybrid_search(
            vector=[0.0] * 4, query="web", owner_user_id="u1", limit=5
        )
        == []
    )


@pytest.mark.asyncio
async def test_sync_indexes_only_owner_scoped_callable_rows() -> None:
    fake = FakeMilvus()
    index = _index(fake)
    embedder = Embedder()
    records = [
        _record(),
        _record(name="ownerless", owner_user_id=None),
        _record(name="anonymous", id=""),
        _record(name="disabled_tool", enabled=False),
        _record(name="unreachable_tool", reachable=False),
        _record(name="not_callable", callable=False),
    ]

    stats = await index.sync(records, embedder=embedder)

    assert stats.scanned == 6
    assert stats.changed == 1
    assert stats.upserted == 1
    assert stats.error is None
    assert embedder.texts == ["search AnySearch web search"]
    assert [row["call_name"] for row in fake.written] == ["mcp__anysearch__search"]


@pytest.mark.asyncio
async def test_sync_embeds_only_rows_whose_hash_changed() -> None:
    unchanged = _record()
    changed = _record(name="fetch", server_name="anysearch")
    fake = FakeMilvus(stored={unchanged.id: unchanged.content_hash})
    index = _index(fake)
    embedder = Embedder()

    stats = await index.sync([unchanged, changed], embedder=embedder)

    assert (stats.scanned, stats.changed, stats.upserted) == (2, 1, 1)
    assert embedder.texts == [changed.content]
    assert [row["tool_id"] for row in fake.written] == [changed.id]


@pytest.mark.asyncio
async def test_records_without_a_hash_are_hashed_over_content_owner_and_call_name() -> None:
    record = _record(content_hash="")
    fake = FakeMilvus()
    index = _index(fake)

    stats = await index.sync([record], embedder=Embedder())

    expected = content_hash_for(record.content, "u1", "mcp__anysearch__search")
    assert stats.upserted == 1
    assert fake.written[0]["content_hash"] == expected
    assert (await index.sync([record], embedder=Embedder())).changed == 0


@pytest.mark.asyncio
async def test_upsert_writes_retrieval_fields_only() -> None:
    fake = FakeMilvus()
    index = _index(fake)
    record = _record(description="x" * 70_000)

    await index.sync([record], embedder=Embedder())

    row = fake.written[0]
    assert set(row) == {
        "pk",
        "tool_id",
        "server_id",
        "server_name",
        "owner_user_id",
        "tool_name",
        "call_name",
        "description",
        "tags",
        "parameters",
        "content",
        "content_hash",
        "embedding",
    }
    assert row["pk"] == row["tool_id"] == record.id
    assert row["server_id"] == "server-1"
    assert row["server_name"] == "anysearch"
    assert row["owner_user_id"] == "u1"
    assert row["tool_name"] == "search"
    assert row["call_name"] == "mcp__anysearch__search"
    assert len(row["description"]) == 65535
    assert json.loads(row["tags"]) == []
    assert json.loads(row["parameters"]) == ["query"]
    assert row["embedding"] == [0.5] * 4


@pytest.mark.asyncio
async def test_full_sync_deletes_rows_that_left_the_catalog() -> None:
    kept = _record()
    fake = FakeMilvus(stored={kept.id: kept.content_hash, "tool-gone": "stale-hash"})
    index = _index(fake)

    stats = await index.sync([kept], embedder=Embedder(), full=True)

    assert stats.deleted == 1
    assert json.loads(fake.deletes[-1].split(" in ", 1)[1]) == ["tool-gone"]
    assert sorted(fake.stored) == [kept.id]


@pytest.mark.asyncio
async def test_sync_refuses_a_snapshot_whose_records_carry_no_tool_id() -> None:
    fake = FakeMilvus(stored={"tool-1": "h1", "tool-2": "h2"})
    index = _index(fake)

    stats = await index.sync([_record(id="")], embedder=Embedder(), full=True)

    assert stats.error == "UnrecognizedToolRecords"
    assert fake.written == []
    assert fake.deletes == []
    assert fake.stored == {"tool-1": "h1", "tool-2": "h2"}


@pytest.mark.asyncio
async def test_sync_reports_embedding_failures_as_stats() -> None:
    class BrokenEmbedder:
        async def embed_documents(self, texts):
            raise RuntimeError("embedding down")

    stats = await _index(FakeMilvus()).sync([_record()], embedder=BrokenEmbedder())

    assert stats.error == "RuntimeError"
    assert stats.upserted == 0

# --------------------------------------------------------------------------- #
# pruning: the collection only ever mirrors the catalog it was handed
#
# Ported from the retired tool-vector-index suite: these are the deletion and
# diff behaviours the sync service relies on, and they must keep holding for
# mcp_tools records (one row per server-owned tool).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_query_hashes_is_keyed_by_tool_id() -> None:
    fake = FakeMilvus(stored={"tool-1": "h1", "tool-2": "h2"})
    index = _index(fake)

    hashes = await index.query_hashes(["tool-1", "tool-missing", "tool-1"])

    # Duplicates collapse, a missing tool is absent (not an empty hash), and the
    # query only asks for the two diff fields.
    assert hashes == {"tool-1": "h1"}
    assert fake.queries[-1]["output_fields"] == ["tool_id", "content_hash"]


@pytest.mark.asyncio
async def test_delete_by_ids_removes_the_rows_and_reports_the_count() -> None:
    fake = FakeMilvus(stored={"tool-1": "h1", "tool-2": "h2", "tool-3": "h3"})
    index = _index(fake)

    assert await index.delete_by_ids(["tool-1", "tool-2", "tool-1"]) == 2

    assert fake.deletes[0].startswith("tool_id in ")
    assert json.loads(fake.deletes[0].split(" in ", 1)[1]) == ["tool-1", "tool-2"]
    assert sorted(fake.stored) == ["tool-3"]
    assert await index.delete_by_ids([]) == 0
    assert len(fake.deletes) == 1


@pytest.mark.asyncio
async def test_an_owner_change_reupserts_the_row() -> None:
    """Only the owner moves: content and call name stay byte-identical."""
    content = "search AnySearch web search"
    before = _record(content=content)
    moved = _record(content=content, owner_user_id="u2")

    # Isolation: content and call name are identical, so the owner is the only
    # hash input that can make the stored row look up to date.
    assert moved.call_name == before.call_name == "mcp__anysearch__search"

    fake = FakeMilvus(stored={before.id: before.content_hash})
    index = _index(fake)

    stats = await index.sync([moved], embedder=Embedder())

    assert (stats.changed, stats.upserted) == (1, 1)
    assert fake.written[-1]["owner_user_id"] == "u2"
    assert fake.written[-1]["content_hash"] == moved.content_hash


@pytest.mark.asyncio
async def test_a_call_name_change_reupserts_the_row() -> None:
    """Only the call name moves: content and owner stay byte-identical."""
    content = "search AnySearch web search"
    before = _record(content=content, call_name="mcp__alpha__search")
    renamed = _record(content=content, call_name="mcp__beta__search")

    # Isolation: content and owner are identical (u1), so the call name is the
    # only hash input that can make the stored row look up to date.
    assert renamed.owner_user_id == before.owner_user_id == "u1"

    fake = FakeMilvus(stored={before.id: before.content_hash})
    index = _index(fake)

    stats = await index.sync([renamed], embedder=Embedder())

    assert (stats.changed, stats.upserted) == (1, 1)
    assert fake.written[-1]["call_name"] == "mcp__beta__search"
    assert fake.written[-1]["owner_user_id"] == "u1"
    assert fake.written[-1]["content_hash"] == renamed.content_hash


@pytest.mark.asyncio
async def test_full_sync_drops_rows_that_stopped_being_owner_scoped() -> None:
    orphan = _record()
    fake = FakeMilvus(stored={orphan.id: orphan.content_hash})
    index = _index(fake)

    stats = await index.sync(
        [_record(owner_user_id=None)], embedder=Embedder(), full=True
    )

    assert (stats.upserted, stats.deleted) == (0, 1)
    assert fake.stored == {}


@pytest.mark.asyncio
async def test_full_sync_still_prunes_a_catalog_of_nothing_but_non_callable_tools() -> None:
    """An empty *keep* set is not a caller bug when the records do carry ids."""
    records = [
        _record(callable=False),
        _record(name="fetch", enabled=False),
        _record(name="offline_tool", reachable=False),
    ]
    fake = FakeMilvus(stored={record.id: "stale-hash" for record in records})
    index = _index(fake)
    embedder = Embedder()

    stats = await index.sync(records, embedder=embedder, full=True)

    assert stats.error is None
    assert (stats.changed, stats.upserted, stats.deleted) == (0, 0, 3)
    assert embedder.texts == []
    assert fake.written == []
    assert fake.stored == {}


@pytest.mark.asyncio
async def test_incremental_sync_drops_the_row_of_a_record_that_became_unindexable() -> None:
    record = _record()
    fake = FakeMilvus(stored={record.id: record.content_hash})
    index = _index(fake)
    embedder = Embedder()

    stats = await index.sync([_record(owner_user_id=None)], embedder=embedder)

    assert (stats.changed, stats.upserted, stats.deleted) == (0, 0, 1)
    assert embedder.texts == []
    assert fake.stored == {}
    assert json.loads(fake.deletes[-1].split(" in ", 1)[1]) == [record.id]


@pytest.mark.asyncio
async def test_incremental_sync_leaves_rows_it_was_not_handed_alone() -> None:
    """Only a full sync prunes rows outside the snapshot it was given."""
    kept = _record()
    fake = FakeMilvus(stored={kept.id: kept.content_hash, "tool-gone": "stale-hash"})
    index = _index(fake)

    stats = await index.sync([kept], embedder=Embedder())

    assert stats.deleted == 0
    assert fake.deletes == []
    assert sorted(fake.stored) == [kept.id, "tool-gone"]

