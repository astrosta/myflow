import pytest

from agentkit.config.contextforge_config import ToolVectorStoreConfig
from agentkit.contextforge.index import (
    ToolCatalogDocument,
    ToolIndexUnavailable,
    ToolVectorIndex,
)


class FakeClient:
    def __init__(self, *, dim: int = 8) -> None:
        self.dim = dim
        self.reqs = []
        self.ranker = None

    def has_collection(self, name):
        assert name == "myflow_tools_catalog"
        return True

    def describe_collection(self, name):
        return {
            "fields": [
                {"name": "embedding", "params": {"dim": self.dim}},
                {"name": "sparse"},
            ]
        }

    def load_collection(self, name):
        return None

    def hybrid_search(self, **kwargs):
        self.reqs = kwargs["reqs"]
        self.ranker = kwargs["ranker"]
        return [[{"entity": {"tool_name": "github_search", "description": "d", "tags": "[]", "parameters": "[]"}, "distance": 0.8}]]

    def search(self, **kwargs):
        self.searches = getattr(self, "searches", [])
        self.searches.append(kwargs)
        return [[{"entity": {"tool_name": "github_search"}, "distance": 0.7}]]


@pytest.mark.asyncio
async def test_hybrid_search_passes_dense_and_sparse_requests() -> None:
    client = FakeClient()
    index = ToolVectorIndex(ToolVectorStoreConfig(uri="http://milvus"), dimensions=8, client=client)
    hits = await index.hybrid_search(vector=[0.0] * 8, query="github code", limit=5)
    assert hits[0].tool_name == "github_search"
    assert len(client.reqs) == 2


@pytest.mark.asyncio
async def test_keyword_search_uses_milvus_bm25_on_the_sparse_field() -> None:
    client = FakeClient()
    index = ToolVectorIndex(ToolVectorStoreConfig(uri="http://milvus"), dimensions=8, client=client)
    hits = await index.keyword_search(query="github code", limit=5)

    assert hits[0].tool_name == "github_search"
    call = client.searches[0]
    assert call["anns_field"] == "sparse"
    assert call["data"] == ["github code"]
    assert call["search_params"]["metric_type"] == "BM25"


@pytest.mark.asyncio
async def test_dimension_mismatch_is_reported() -> None:
    index = ToolVectorIndex(ToolVectorStoreConfig(uri="http://milvus"), dimensions=8, client=FakeClient(dim=4))
    with pytest.raises(ToolIndexUnavailable):
        await index.hybrid_search(vector=[0.0] * 8, query="github", limit=5)


@pytest.mark.asyncio
async def test_sync_embeds_only_changed_documents(monkeypatch) -> None:
    index = ToolVectorIndex(ToolVectorStoreConfig(uri="http://milvus"), dimensions=8, client=FakeClient())
    docs = [
        ToolCatalogDocument("a", "A", "a", (), (), "contextforge", "a", "same", 1),
        ToolCatalogDocument("b", "B", "b", (), (), "contextforge", "b", "new", 2),
    ]

    async def fake_hashes(names):
        return {"a": "same"}

    async def fake_upsert(documents, vectors):
        return len(documents)

    embedded: list[str] = []

    class Embedder:
        async def embed_documents(self, texts):
            embedded.extend(texts)
            return [[0.0] * 8 for _ in texts]

    monkeypatch.setattr(index, "ensure_collection", lambda dimensions=None: _async_none())
    monkeypatch.setattr(index, "query_hashes", fake_hashes)
    monkeypatch.setattr(index, "upsert", fake_upsert)
    stats = await index.sync(docs, embedder=Embedder())
    assert stats.changed == 1
    assert stats.upserted == 1
    assert embedded == ["b"]


async def _async_none():
    return None

@pytest.mark.asyncio
async def test_sync_without_an_embedder_indexes_with_placeholder_vectors(monkeypatch) -> None:
    index = ToolVectorIndex(ToolVectorStoreConfig(uri="http://milvus"), dimensions=8, client=FakeClient())
    docs = [ToolCatalogDocument("a", "A", "a", (), (), "contextforge", "a", "h", 1)]
    captured: list[list[float]] = []

    async def fake_hashes(names):
        return {}

    async def fake_upsert(documents, vectors):
        captured.extend(vectors)
        return len(documents)

    monkeypatch.setattr(index, "ensure_collection", lambda dimensions=None: _async_none())
    monkeypatch.setattr(index, "query_hashes", fake_hashes)
    monkeypatch.setattr(index, "upsert", fake_upsert)

    stats = await index.sync(docs, embedder=None)

    assert stats.upserted == 1
    assert captured == [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]


@pytest.mark.asyncio
async def test_hybrid_search_uses_configured_rrf_k() -> None:
    from agentkit.config.contextforge_config import ToolKeywordConfig

    client = FakeClient()
    index = ToolVectorIndex(
        ToolVectorStoreConfig(uri="http://milvus"),
        dimensions=8,
        client=client,
        keyword=ToolKeywordConfig(analyzer="english", bm25_k1=2.0, bm25_b=0.5),
        rrf_k=37,
    )
    await index.hybrid_search(vector=[0.0] * 8, query="github code", limit=5)
    assert client.ranker._k == 37
    assert index._keyword.analyzer == "english"
