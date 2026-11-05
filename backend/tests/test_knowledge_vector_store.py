"""Milvus adapter: deterministic filters, entities, and no cross-user leakage."""

from __future__ import annotations

import pytest
from agentkit.knowledge.config import MilvusConfig
from agentkit.knowledge.errors import KeywordSearchUnavailable, KnowledgeUnavailable
from agentkit.knowledge.models import KnowledgeChunk, KnowledgeDocument, KnowledgeParentChunk
from agentkit.knowledge.vector_store.milvus import MilvusVectorStore


class FakeMilvus:
    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.deleted: list[str] = []
        self.searches: list[dict] = []
        self.hybrid_searches: list[dict] = []
        self.queries: list[dict] = []
        self.deleted_calls: list[tuple[str, str]] = []
        self.inserted_by_collection: dict[str, list[dict]] = {}
        self.parent_rows: list[dict] = []
        self.collections: set[str] = {"kb"}
        self.flushes = 0
        self.closed = False
        self.extra_fields: list[dict] = [{"name": "sparse"}, {"name": "parent_id"}]

    def has_collection(self, name: str) -> bool:
        return name in self.collections

    def load_collection(self, name: str) -> None:
        return None

    def insert(self, collection_name: str, data: list[dict]) -> dict:
        self.inserted.extend(data)
        self.inserted_by_collection.setdefault(collection_name, []).extend(data)
        return {"insert_count": len(data)}

    def delete(self, collection_name: str, filter: str) -> dict:
        self.deleted.append(filter)
        self.deleted_calls.append((collection_name, filter))
        return {"delete_count": 1}

    def query(self, collection_name: str, **kwargs):
        self.queries.append({"collection_name": collection_name, **kwargs})
        if collection_name.endswith("_parents"):
            return list(self.parent_rows)
        return [
            {
                "chunk_index": 1, "section": "回滚", "page": 2,
                "content": "second", "parent_id": "d1:p0",
            },
            {"chunk_index": 0, "section": "安装", "page": 1, "content": "first"},
        ]

    def create_schema(self, **kwargs):
        return FakeSchema()

    def prepare_index_params(self):
        return FakeIndexParams()

    def create_collection(self, collection_name: str, **kwargs) -> None:
        self.collections.add(collection_name)

    def flush(self, collection_name: str) -> None:
        self.flushes += 1

    def search(self, collection_name: str, **kwargs):
        self.searches.append(kwargs)
        return [[
            {"id": "d1:0", "distance": 0.91, "entity": {"content": "hello", "filename": "a.md", "section": "S", "document_id": "d1", "knowledge_base_id": "b1", "knowledge_base_name": "Docs", "knowledge_base_type": "technical", "chunk_index": 0, "page": 1}},
            {"id": "d1:1", "distance": 0.12, "entity": {"content": "low", "filename": "a.md", "section": "", "document_id": "d1", "knowledge_base_id": "b1", "knowledge_base_name": "Docs", "knowledge_base_type": "technical", "chunk_index": 1, "page": 0}},
        ]]

    def hybrid_search(self, collection_name: str, **kwargs):
        self.hybrid_searches.append(kwargs)
        return [[
            {"id": "d1:1", "distance": 0.0328, "entity": {"content": "keyword hit", "filename": "a.md", "section": "K", "document_id": "d1", "knowledge_base_id": "b1", "knowledge_base_name": "Docs", "knowledge_base_type": "technical", "chunk_index": 1, "page": 0}},
        ]]

    def describe_collection(self, name: str) -> dict:
        return {"fields": [{"name": "embedding", "params": {"dim": "4"}}, {"name": "content"}, *self.extra_fields]}

    def close(self) -> None:
        self.closed = True


def _document() -> KnowledgeDocument:
    return KnowledgeDocument(
        id="d1", user_id="u1", knowledge_base_id="b1", filename="a.md",
        stored_name="d1.md", mime_type="text/markdown", size_bytes=3,
        status="ready", chunk_count=1, error_message=None,
        created_at="now", updated_at="now",
    )


def _store(fake: FakeMilvus) -> MilvusVectorStore:
    return MilvusVectorStore(
        MilvusConfig(uri="http://milvus:19530", collection="kb", flush=True),
        dimensions=4,
        client=fake,
    )


class FakeParentCreation(FakeMilvus):
    """Records the declared fields of every collection created in one call."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[tuple[str, dict[str, dict]]] = []

    def create_schema(self, **kwargs) -> FakeSchema:
        return FakeSchema()

    def prepare_index_params(self) -> FakeIndexParams:
        return FakeIndexParams()

    def create_collection(self, collection_name: str, **kwargs) -> None:
        schema = kwargs.get("schema")
        self.created.append(
            (collection_name, dict(getattr(schema, "fields", {})))
        )
        self.collections.add(collection_name)


@pytest.mark.asyncio
async def test_parent_collection_placeholder_vector_meets_milvus_min_dimension() -> None:
    # Milvus rejects FLOAT_VECTOR dim < 2 (code 1100), so a 1-D placeholder
    # made every parent-collection write fail with "invalid dimension: 1".
    fake = FakeParentCreation()
    store = _store(fake)
    await store.upsert_document(
        user_id="u1",
        knowledge_base_id="b1",
        knowledge_base_name="Docs",
        knowledge_base_type="operations",
        document=_document(),
        chunks=[KnowledgeChunk(index=0, content="child", parent_id="d1:p0")],
        vectors=[[1.0, 0.0, 0.0, 0.0]],
        parents=[KnowledgeParentChunk(parent_id="d1:p0", content="parent full text")],
    )

    created = dict(fake.created)
    parent_fields = created["kb_parents"]
    assert parent_fields["embedding"]["dim"] >= 2
    assert "sparse" not in parent_fields
    inserted = fake.inserted_by_collection["kb_parents"]
    assert inserted[0]["embedding"] == [0.0, 0.0]
    await store.aclose()


@pytest.mark.asyncio
async def test_upsert_replaces_only_the_same_users_document() -> None:
    fake = FakeMilvus()
    store = _store(fake)
    await store.upsert_document(
        user_id="u1",
        knowledge_base_id="b1",
        knowledge_base_name="Docs",
        knowledge_base_type="technical",
        document=_document(),
        chunks=[KnowledgeChunk(index=0, content="hello", section="S", page=1)],
        vectors=[[1.0, 0.0, 0.0, 0.0]],
    )
    assert fake.deleted == ['user_id == "u1" and document_id == "d1"']
    assert fake.inserted[0]["document_id"] == "d1"
    assert fake.inserted[0]["knowledge_base_id"] == "b1"
    assert fake.inserted[0]["embedding"] == [1.0, 0.0, 0.0, 0.0]
    assert fake.flushes == 1
    await store.aclose()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_parent_rows_are_written_and_fetched_with_user_scope() -> None:
    fake = FakeMilvus()
    store = _store(fake)
    parent = KnowledgeParentChunk(
        parent_id="d1:p0", content="parent full text", section="S", page=1
    )

    await store.upsert_document(
        user_id="u1",
        knowledge_base_id="b1",
        knowledge_base_name="Docs",
        knowledge_base_type="operations",
        document=_document(),
        chunks=[KnowledgeChunk(index=0, content="child", parent_id="d1:p0")],
        vectors=[[1.0, 0.0, 0.0, 0.0]],
        parents=[parent],
    )

    parent_rows = fake.inserted_by_collection["kb_parents"]
    assert parent_rows[0]["parent_id"] == "d1:p0"
    assert parent_rows[0]["content"] == "parent full text"
    # Milvus rejects dimensions below 2, so the placeholder vector is 2-D.
    assert parent_rows[0]["embedding"] == [0.0, 0.0]
    assert fake.inserted_by_collection["kb"][0]["parent_id"] == "d1:p0"

    fake.parent_rows = [{"parent_id": "d1:p0", "content": "parent full text"}]
    fetched = await store.get_parents(user_id="u1", parent_ids=["d1:p0"])
    assert fetched == {"d1:p0": "parent full text"}
    call = fake.queries[-1]
    assert call["filter"] == 'user_id == "u1" and parent_id in ["d1:p0"]'
    assert call["output_fields"] == ["parent_id", "content"]

    await store.delete_document(user_id="u1", document_id="d1")
    assert ("kb", 'user_id == "u1" and document_id == "d1"') in fake.deleted_calls
    assert ("kb_parents", 'user_id == "u1" and document_id == "d1"') in fake.deleted_calls
    await store.aclose()


@pytest.mark.asyncio
async def test_list_chunks_is_scoped_by_user_and_document_and_sorted() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    chunks = await store.list_chunks(user_id="u1", document_id="d1")

    assert [chunk.index for chunk in chunks] == [0, 1]
    assert chunks[0].content == "first"
    assert chunks[1].section == "回滚"
    assert chunks[0].parent_id is None
    assert chunks[1].parent_id == "d1:p0"
    call = fake.queries[0]
    assert call["collection_name"] == "kb"
    assert call["filter"] == 'user_id == "u1" and document_id == "d1"'
    assert call["output_fields"] == [
        "chunk_index", "section", "page", "content", "parent_id",
    ]
    assert call["limit"] == 16384
    await store.aclose()


@pytest.mark.asyncio
async def test_old_collection_without_parent_id_is_rejected_with_actionable_error() -> None:
    fake = FakeMilvus()
    fake.extra_fields = [{"name": "sparse"}]
    store = _store(fake)

    with pytest.raises(KnowledgeUnavailable, match="reset_knowledge_milvus"):
        await store.search(user_id="u1", vector=[1.0, 0.0, 0.0, 0.0])

    await store.aclose()


@pytest.mark.asyncio
async def test_search_filters_by_user_and_optional_bases_and_threshold() -> None:
    fake = FakeMilvus()
    store = _store(fake)
    hits = await store.search(
        user_id="u1", vector=[1.0, 0.0, 0.0, 0.0], base_ids=["b1"], limit=5,
        score_threshold=0.2,
    )
    assert len(hits) == 1
    assert hits[0].content == "hello"
    assert hits[0].score == 0.91
    call = fake.searches[0]
    assert call["filter"] == 'user_id == "u1" and knowledge_base_id in ["b1"]'
    assert call["limit"] == 5
    assert call["data"] == [[1.0, 0.0, 0.0, 0.0]]
    assert call["search_params"]["params"] == {}
    await store.aclose()


@pytest.mark.asyncio
async def test_search_without_bases_has_no_extra_filter_and_delete_is_scoped() -> None:
    fake = FakeMilvus()
    store = _store(fake)
    await store.search(user_id="u1", vector=[1.0, 0.0, 0.0, 0.0], limit=3)
    assert fake.searches[0]["filter"] == 'user_id == "u1"'
    await store.delete_document(user_id="u1", document_id="d1")
    assert fake.deleted[-1] == 'user_id == "u1" and document_id == "d1"'
    await store.aclose()


@pytest.mark.asyncio
async def test_hybrid_search_fuses_both_channels_with_rrf() -> None:
    fake = FakeMilvus()
    store = _store(fake)
    hits = await store.hybrid_search(
        user_id="u1", vector=[1.0, 0.0, 0.0, 0.0], keyword_query="E1024",
        base_ids=["b1"], limit=20, rrf_k=60,
    )
    assert hits[0].content == "keyword hit"
    assert hits[0].score == 0.0328
    assert hits[0].rrf_score == 0.0328
    call = fake.hybrid_searches[0]
    # MilvusClient.hybrid_search has no filter argument: both channels carry the expression.
    assert "filter" not in call
    assert call["limit"] == 20
    dense, sparse = call["reqs"]
    assert dense.expr == 'user_id == "u1" and knowledge_base_id in ["b1"]'
    assert sparse.expr == dense.expr
    assert dense.anns_field == "embedding"
    assert dense.data == [[1.0, 0.0, 0.0, 0.0]]
    assert dense.param["params"] == {}
    assert sparse.anns_field == "sparse"
    assert sparse.data == ["E1024"]
    assert sparse.param["metric_type"] == "BM25"
    assert call["ranker"].dict() == {"strategy": "rrf", "params": {"k": 60}}
    await store.aclose()


@pytest.mark.asyncio
async def test_hybrid_search_requires_a_sparse_field() -> None:
    fake = FakeMilvus()
    fake.extra_fields = [{"name": "parent_id"}]
    store = _store(fake)
    with pytest.raises(KeywordSearchUnavailable):
        await store.hybrid_search(
            user_id="u1", vector=[1.0, 0.0, 0.0, 0.0], keyword_query="E1024", limit=5
        )
    assert fake.hybrid_searches == []
    await store.aclose()


@pytest.mark.asyncio
async def test_hybrid_search_rejects_an_empty_keyword_query() -> None:
    fake = FakeMilvus()
    store = _store(fake)
    with pytest.raises(ValueError, match="keyword"):
        await store.hybrid_search(
            user_id="u1", vector=[1.0, 0.0, 0.0, 0.0], keyword_query="   ", limit=5
        )
    await store.aclose()


class FakeSchema:
    def __init__(self) -> None:
        self.fields: dict[str, dict] = {}
        self.functions: list[object] = []

    def add_field(self, name: str, dtype, **kwargs) -> None:
        self.fields[name] = {"dtype": dtype, **kwargs}

    def add_function(self, function) -> None:
        self.functions.append(function)


class FakeIndexParams:
    def __init__(self) -> None:
        self.indexes: list[dict] = []

    def add_index(self, **kwargs) -> None:
        self.indexes.append(kwargs)


class FakeEmptyMilvus(FakeMilvus):
    def __init__(self) -> None:
        super().__init__()
        self.schema = FakeSchema()
        self.index_params = FakeIndexParams()
        self.created: list[dict] = []

    def has_collection(self, name: str) -> bool:
        return False

    def create_schema(self, **kwargs) -> FakeSchema:
        return self.schema

    def prepare_index_params(self) -> FakeIndexParams:
        return self.index_params

    def create_collection(self, **kwargs) -> None:
        self.created.append(kwargs)


@pytest.mark.asyncio
async def test_collection_is_created_with_bm25_sparse_field_and_analyzer() -> None:
    pytest.importorskip("pymilvus")
    from pymilvus import FunctionType

    fake = FakeEmptyMilvus()
    store = MilvusVectorStore(
        MilvusConfig(uri="http://milvus:19530", collection="kb"),
        dimensions=4,
        client=fake,
        keyword_analyzer="chinese",
        bm25_k1=1.5,
        bm25_b=0.7,
    )
    await store.upsert_document(
        user_id="u1",
        knowledge_base_id="b1",
        knowledge_base_name="Docs",
        knowledge_base_type="technical",
        document=_document(),
        chunks=[KnowledgeChunk(index=0, content="hello")],
        vectors=[[1.0, 0.0, 0.0, 0.0]],
    )
    content = fake.schema.fields["content"]
    assert content["enable_analyzer"] is True
    assert content["analyzer_params"] == {"type": "chinese"}
    assert "sparse" in fake.schema.fields
    function = fake.schema.functions[0]
    assert function.input_field_names == ["content"]
    assert function.output_field_names == ["sparse"]
    assert function.type == FunctionType.BM25
    sparse_index = next(i for i in fake.index_params.indexes if i["field_name"] == "sparse")
    assert sparse_index["metric_type"] == "BM25"
    assert sparse_index["params"] == {"bm25_k1": 1.5, "bm25_b": 0.7}
    embedding_index = next(i for i in fake.index_params.indexes if i["field_name"] == "embedding")
    assert embedding_index["params"] == {}
    assert fake.created[0]["collection_name"] == "kb"
    await store.aclose()


@pytest.mark.asyncio
async def test_vector_shape_is_validated() -> None:
    fake = FakeMilvus()
    store = _store(fake)
    with pytest.raises(ValueError, match="dimension"):
        await store.upsert_document(
            user_id="u1", knowledge_base_id="b1", knowledge_base_name="Docs",
            knowledge_base_type="technical", document=_document(),
            chunks=[KnowledgeChunk(index=0, content="x")], vectors=[[1.0]],
        )
    with pytest.raises(ValueError, match="count"):
        await store.upsert_document(
            user_id="u1", knowledge_base_id="b1", knowledge_base_name="Docs",
            knowledge_base_type="technical", document=_document(),
            chunks=[KnowledgeChunk(index=0, content="x"), KnowledgeChunk(index=1, content="y")],
            vectors=[[1.0, 0.0, 0.0, 0.0]],
        )
    await store.aclose()
