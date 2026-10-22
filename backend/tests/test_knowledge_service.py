"""Knowledge service: ingestion state machine, counters, reindex, search routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from agentkit.knowledge.config import KnowledgeBaseConfig
from agentkit.knowledge.errors import (
    KeywordSearchUnavailable,
    KnowledgeDocumentNotFound,
    KnowledgeUnavailable,
)
from agentkit.knowledge.models import ParsedDocument, SearchHit
from agentkit.knowledge.rerank import RerankScore
from agentkit.knowledge.service import KnowledgeService
from agentkit.knowledge.store.memory import MemoryKnowledgeStore


class FakeUpload:
    def __init__(self, filename: str, data: bytes, content_type: str = "text/markdown") -> None:
        self.filename = filename
        self.content_type = content_type
        self._data = data
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeParser:
    def __init__(self, markdown: str = "# 文档\n\n这是解析出来的正文。") -> None:
        self.markdown = markdown
        self.calls: list[str] = []

    async def parse(self, path: Path, *, filename: str) -> ParsedDocument:
        self.calls.append(filename)
        return ParsedDocument(markdown=self.markdown)

    async def aclose(self) -> None:
        return None


class FakeEmbedder:
    def __init__(self) -> None:
        self.document_batches: list[list[str]] = []
        self.queries: list[str] = []

    async def embed_documents(self, texts):
        self.document_batches.append(list(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def embed_query(self, text: str):
        self.queries.append(text)
        return [0.1, 0.2, 0.3, 0.4]

    async def aclose(self) -> None:
        return None


class FakeVectorStore:
    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.deleted_documents: list[str] = []
        self.deleted_bases: list[str] = []
        self.searches: list[dict] = []
        self.hybrid_searches: list[dict] = []

    async def upsert_document(self, **kwargs) -> None:
        self.upserts.append(kwargs)

    async def delete_document(self, *, user_id: str, document_id: str) -> None:
        self.deleted_documents.append(document_id)

    async def delete_base(self, *, user_id: str, knowledge_base_id: str) -> None:
        self.deleted_bases.append(knowledge_base_id)

    async def search(self, **kwargs):
        self.searches.append(kwargs)
        return self._hits(score=0.9)

    async def hybrid_search(self, **kwargs):
        self.hybrid_searches.append(kwargs)
        return [
            SearchHit(
                chunk_id="d1:1", document_id="d1", filename="manual.md",
                section="回滚", content="关键词命中内容" * 5, score=0.0328,
                knowledge_base_id="b1", knowledge_base_name="产品文档",
                knowledge_base_type="technical", rrf_score=0.0328,
            ),
            *self._hits(score=0.9),
        ]

    @staticmethod
    def _hits(*, score: float):
        return [
            SearchHit(
                chunk_id="d1:0", document_id="d1", filename="manual.md",
                section="安装", content="内容" * 700, score=score,
                knowledge_base_id="b1", knowledge_base_name="产品文档",
                knowledge_base_type="technical",
            )
        ]

    async def aclose(self) -> None:
        return None


class FakeReranker:
    enabled = True

    def __init__(self, *, order=None, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._order = order
        self._fail = fail

    async def rerank(self, *, query: str, documents):
        self.calls.append({"query": query, "documents": list(documents)})
        if self._fail:
            raise KnowledgeUnavailable("rerank down")
        order = self._order or list(range(len(documents)))
        return [
            RerankScore(index=index, score=0.99 - position * 0.1)
            for position, index in enumerate(order)
        ]

    async def aclose(self) -> None:
        return None


def _config(tmp_path: Path, **overrides) -> KnowledgeBaseConfig:
    base = {
        "enabled": True,
        "storage_dir": str(tmp_path / "storage"),
        "max_file_size_bytes": 1024 * 1024,
        "max_total_size_bytes": 4 * 1024 * 1024,
        "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
        "embedding": {"base_url": "http://embedding.test/v1", "dimensions": 4},
        "vector_store": {"uri": "http://milvus:19530"},
        "retrieval": {"max_context_chars": 1200},
        "chunking": {
            "tokenizer": {"provider": "approximate"},
            "types": {
                "technical": {"chunk_size_tokens": 64, "chunk_overlap_tokens": 0},
                "operations": {"chunk_size_tokens": 64, "chunk_overlap_tokens": 0},
                "faq": {"chunk_size_tokens": 64, "chunk_overlap_tokens": 0},
                "security": {"chunk_size_tokens": 64, "chunk_overlap_tokens": 0},
                "api": {"chunk_size_tokens": 64, "chunk_overlap_tokens": 0},
            }
        },
    }
    base.update(overrides)
    return KnowledgeBaseConfig.model_validate(base)


async def _service(
    tmp_path: Path, *, parser=None, embedder=None, vector=None, config=None, reranker=None
):
    store = MemoryKnowledgeStore()
    service = KnowledgeService(
        config or _config(tmp_path),
        store=store,
        parser=parser or FakeParser(),
        embedder=embedder or FakeEmbedder(),
        vector_store=vector or FakeVectorStore(),
        reranker=reranker,
    )
    return service, store


@pytest.mark.asyncio
async def test_markdown_upload_runs_pipeline_and_updates_counters(tmp_path) -> None:
    parser = FakeParser()
    vector = FakeVectorStore()
    service, store = await _service(tmp_path, parser=parser, vector=vector)
    base = await service.create_base(user_id="u1", name="产品文档", knowledge_type="technical")

    created = await service.add_documents(
        user_id="u1", base_id=base.id,
        uploads=[FakeUpload("manual.md", ("# 标题\n\n" + "正文内容。" * 40).encode())],
    )
    assert len(created) == 1
    await service.wait_for_idle(timeout=5)
    document = (await store.list_documents("u1", base.id))[0]
    assert document.status == "ready"
    assert document.chunk_count > 0
    assert parser.calls == []  # markdown bypasses MinerU
    assert vector.upserts and vector.upserts[0]["knowledge_base_id"] == base.id
    refreshed = await store.get_base("u1", base.id)
    assert refreshed.document_count == 1
    assert refreshed.chunk_count == document.chunk_count
    await service.aclose()


@pytest.mark.asyncio
async def test_pdf_routes_to_mineru_and_failure_is_recorded(tmp_path) -> None:
    class FailingParser(FakeParser):
        async def parse(self, path: Path, *, filename: str):
            self.calls.append(filename)
            raise RuntimeError("secret provider detail")

    parser = FailingParser()
    service, store = await _service(tmp_path, parser=parser)
    base = await service.create_base(user_id="u1", name="PDF", knowledge_type="technical")
    await service.add_documents(
        user_id="u1", base_id=base.id,
        uploads=[FakeUpload("manual.pdf", b"%PDF", "application/pdf")],
    )
    await service.wait_for_idle(timeout=5)
    document = (await store.list_documents("u1", base.id))[0]
    assert parser.calls == ["manual.pdf"]
    assert document.status == "failed"
    assert document.chunk_count == 0
    assert "secret provider detail" not in (document.error_message or "")
    await service.aclose()


@pytest.mark.asyncio
async def test_reindex_replaces_vectors_and_keeps_document_count(tmp_path) -> None:
    vector = FakeVectorStore()
    service, store = await _service(tmp_path, vector=vector)
    base = await service.create_base(user_id="u1", name="API", knowledge_type="api")
    await service.add_documents(
        user_id="u1", base_id=base.id,
        uploads=[FakeUpload("api.md", ("# API\n\n## GET /users\n\n" + "内容。" * 30).encode())],
    )
    await service.wait_for_idle(timeout=5)
    first = (await store.list_documents("u1", base.id))[0]
    assert first.status == "ready"

    await service.reindex_document(user_id="u1", document_id=first.id)
    await service.wait_for_idle(timeout=5)
    second = (await store.get_document("u1", first.id))
    assert second is not None and second.status == "ready"
    assert second.chunk_count == first.chunk_count
    assert vector.deleted_documents == [first.id]
    assert len(vector.upserts) == 2
    refreshed = await store.get_base("u1", base.id)
    assert refreshed.document_count == 1
    assert refreshed.chunk_count == second.chunk_count
    await service.aclose()


@pytest.mark.asyncio
async def test_delete_document_and_base_clean_vectors(tmp_path) -> None:
    vector = FakeVectorStore()
    service, store = await _service(tmp_path, vector=vector)
    base = await service.create_base(user_id="u1", name="FAQ", knowledge_type="faq")
    await service.add_documents(
        user_id="u1", base_id=base.id,
        uploads=[FakeUpload("faq.md", ("## Q: 如何重置密码？\n\nA: " + "在设置页重置密码并检查邮箱。" * 5).encode())],
    )
    await service.wait_for_idle(timeout=5)
    document = (await store.list_documents("u1", base.id))[0]
    await service.delete_document(user_id="u1", document_id=document.id)
    assert vector.deleted_documents == [document.id]
    assert await store.get_document("u1", document.id) is None

    service2, store2 = await _service(tmp_path)
    base2 = await service2.create_base(user_id="u2", name="SEC", knowledge_type="security")
    await service2.add_documents(
        user_id="u2", base_id=base2.id,
        uploads=[FakeUpload("sec.md", "第1条 访问控制。".encode())],
    )
    await service2.wait_for_idle(timeout=5)
    await service2.delete_base(user_id="u2", base_id=base2.id)
    assert base2.id in service2._vector_store.deleted_bases
    assert await store2.get_base("u2", base2.id) is None
    await service.aclose()
    await service2.aclose()


@pytest.mark.asyncio
async def test_search_routes_by_type_name_and_truncates_context(tmp_path) -> None:
    embedder = FakeEmbedder()
    vector = FakeVectorStore()
    service, _ = await _service(tmp_path, embedder=embedder, vector=vector)
    await service.create_base(user_id="u1", name="产品文档 A", knowledge_type="technical")
    await service.create_base(user_id="u1", name="产品文档 B", knowledge_type="faq")
    await service.create_base(user_id="u2", name="别人的", knowledge_type="technical")

    outcome = await service.search(user_id="u1", query="怎么安装", knowledge_base_type="technical")
    assert outcome.hits and embedder.queries == ["怎么安装"]
    assert vector.hybrid_searches[0]["base_ids"]
    assert len(outcome.hits[0].content) < 2000
    assert outcome.truncated is True

    await service.search(user_id="u1", query="问题", knowledge_base_name="产品文档 B")
    assert vector.hybrid_searches[1]["base_ids"]
    await service.aclose()


@pytest.mark.asyncio
async def test_search_is_hybrid_by_default_and_defaults_the_keyword_to_the_query(tmp_path) -> None:
    vector = FakeVectorStore()
    service, _ = await _service(tmp_path, vector=vector)
    await service.create_base(user_id="u1", name="运维手册", knowledge_type="operations")

    outcome = await service.search(user_id="u1", query="升级到 3.2 要做什么")
    assert outcome.retrieval_mode == "hybrid"
    assert outcome.keyword_query == "升级到 3.2 要做什么"
    assert outcome.reranked is False
    call = vector.hybrid_searches[0]
    assert call["keyword_query"] == "升级到 3.2 要做什么"
    assert call["rrf_k"] == 60
    assert call["limit"] == 20
    assert vector.searches == []
    # Fusion order is preserved when no reranker is configured.
    assert [hit.chunk_id for hit in outcome.hits] == ["d1:1", "d1:0"]
    assert outcome.hits[0].rrf_score == 0.0328
    await service.aclose()


@pytest.mark.asyncio
async def test_explicit_keyword_query_only_touches_the_bm25_channel(tmp_path) -> None:
    embedder = FakeEmbedder()
    vector = FakeVectorStore()
    service, _ = await _service(tmp_path, embedder=embedder, vector=vector)
    await service.create_base(user_id="u1", name="运维手册", knowledge_type="operations")

    outcome = await service.search(
        user_id="u1", query="升级步骤", keyword_query="  E1024 回滚  "
    )
    assert embedder.queries == ["升级步骤"]
    assert outcome.keyword_query == "E1024 回滚"
    assert vector.hybrid_searches[0]["keyword_query"] == "E1024 回滚"
    await service.aclose()


@pytest.mark.asyncio
async def test_keyword_channel_off_or_unavailable_degrades_to_vector(tmp_path) -> None:
    config = _config(tmp_path, retrieval={"max_context_chars": 1200, "keyword": {"enabled": False}})
    vector = FakeVectorStore()
    service, _ = await _service(tmp_path, vector=vector, config=config)
    await service.create_base(user_id="u1", name="运维手册", knowledge_type="operations")

    outcome = await service.search(user_id="u1", query="升级步骤")
    assert outcome.retrieval_mode == "vector"
    assert vector.hybrid_searches == []
    assert vector.searches[0]["score_threshold"] is None
    await service.aclose()

    broken = FakeVectorStore()
    broken.hybrid_search = _unavailable_hybrid  # type: ignore[method-assign]
    service2, _ = await _service(tmp_path, vector=broken)
    await service2.create_base(user_id="u1", name="运维手册", knowledge_type="operations")
    outcome2 = await service2.search(user_id="u1", query="升级步骤")
    assert outcome2.retrieval_mode == "vector"
    assert broken.searches
    await service2.aclose()


async def _unavailable_hybrid(**kwargs):
    raise KeywordSearchUnavailable("no sparse field")


@pytest.mark.asyncio
async def test_rerank_reorders_hits_and_records_the_score(tmp_path) -> None:
    vector = FakeVectorStore()
    reranker = FakeReranker(order=[1, 0])
    config = _config(
        tmp_path,
        retrieval={
            "max_context_chars": 8000,
            "rerank": {"enabled": True, "base_url": "http://rerank.test", "model": "r"},
        },
    )
    service, _ = await _service(tmp_path, vector=vector, config=config, reranker=reranker)
    await service.create_base(user_id="u1", name="运维手册", knowledge_type="operations")

    outcome = await service.search(user_id="u1", query="升级步骤")
    assert outcome.reranked is True
    assert reranker.calls[0]["query"] == "升级步骤"
    assert len(reranker.calls[0]["documents"]) == 2
    assert [hit.chunk_id for hit in outcome.hits] == ["d1:0", "d1:1"]
    assert outcome.hits[0].score == 0.99
    assert outcome.hits[0].rerank_score == 0.99
    await service.aclose()


@pytest.mark.asyncio
async def test_rerank_failure_keeps_the_fusion_order(tmp_path) -> None:
    vector = FakeVectorStore()
    reranker = FakeReranker(fail=True)
    config = _config(
        tmp_path,
        retrieval={
            "max_context_chars": 8000,
            "rerank": {"enabled": True, "base_url": "http://rerank.test", "model": "r"},
        },
    )
    service, _ = await _service(tmp_path, vector=vector, config=config, reranker=reranker)
    await service.create_base(user_id="u1", name="运维手册", knowledge_type="operations")

    outcome = await service.search(user_id="u1", query="升级步骤")
    assert outcome.reranked is False
    assert [hit.chunk_id for hit in outcome.hits] == ["d1:1", "d1:0"]
    assert outcome.hits[0].rerank_score is None
    await service.aclose()


@pytest.mark.asyncio
async def test_missing_document_is_404_and_disabled_state(tmp_path) -> None:
    service, _ = await _service(tmp_path)
    with pytest.raises(KnowledgeDocumentNotFound):
        await service.delete_document(user_id="u1", document_id="missing")
    await service.aclose()
