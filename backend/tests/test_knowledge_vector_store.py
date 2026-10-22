"""Milvus adapter: deterministic filters, entities, and no cross-user leakage."""

from __future__ import annotations

import pytest
from agentkit.knowledge.config import MilvusConfig
from agentkit.knowledge.errors import KeywordSearchUnavailable
from agentkit.knowledge.models import KnowledgeChunk, KnowledgeDocument
from agentkit.knowledge.vector_store.milvus import MilvusVectorStore


class FakeMilvus:
    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.deleted: list[str] = []
        self.searches: list[dict] = []
        self.hybrid_searches: list[dict] = []
        self.flushes = 0
        self.closed = False
        self.extra_fields: list[dict] = [{"name": "sparse"}]

    def has_collection(self, name: str) -> bool:
        return True

    def load_collection(self, name: str) -> None:
        return None

    def insert(self, collection_name: str, data: list[dict]) -> dict:
        self.inserted.extend(data)
        return {"insert_count": len(data)}

    def delete(self, collection_name: str, filter: str) -> dict:
        self.deleted.append(filter)
        return {"delete_count": 1}

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
    assert sparse.anns_field == "sparse"
    assert sparse.data == ["E1024"]
    assert sparse.param["metric_type"] == "BM25"
    assert call["ranker"].dict() == {"strategy": "rrf", "params": {"k": 60}}
    await store.aclose()


@pytest.mark.asyncio
async def test_hybrid_search_requires_a_sparse_field() -> None:
    fake = FakeMilvus()
    fake.extra_fields = []
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
