"""Milvus vector store using the official pymilvus MilvusClient.

pymilvus is imported lazily so a disabled knowledge base does not need the extra.
All filters are built server-side; a model-supplied user id can never reach them.
The collection carries a BM25 sparse field so keyword recall also happens server-side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from typing import Any

from agentkit.knowledge.config import MilvusConfig
from agentkit.knowledge.errors import KeywordSearchUnavailable, KnowledgeUnavailable
from agentkit.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeParentChunk,
    SearchHit,
)

logger = logging.getLogger(__name__)

OUTPUT_FIELDS = [
    "chunk_id", "document_id", "filename", "section", "content",
    "knowledge_base_id", "knowledge_base_name", "knowledge_base_type",
    "chunk_index", "page", "parent_id",
]
PARENT_OUTPUT_FIELDS = ["parent_id", "content"]
PARENT_COLLECTION_SUFFIX = "_parents"
# Milvus requires every collection to own a vector field and rejects dimensions
# below 2. Parent rows are only ever fetched by primary key/filter, never
# searched, so a constant placeholder vector is enough to satisfy the schema.
PARENT_PLACEHOLDER_VECTOR = (0.0, 0.0)
PARENT_QUERY_LIMIT = 2048
CHUNK_LIST_FIELDS = ["chunk_index", "section", "page", "content", "parent_id"]
# A single document is bounded by the upload limits; 16k covers the largest
# supported package while keeping one query per detail page.
CHUNK_QUERY_LIMIT = 16_384


class MilvusVectorStore:
    def __init__(
        self,
        config: MilvusConfig,
        *,
        dimensions: int,
        client: Any | None = None,
        keyword_analyzer: str = "chinese",
        bm25_k1: float = 1.2,
        bm25_b: float = 0.75,
    ) -> None:
        self._config = config
        self._parent_collection = config.collection + PARENT_COLLECTION_SUFFIX
        self._dimensions = int(dimensions)
        self._client = client
        self._owns_client = client is None
        self._keyword_analyzer = keyword_analyzer
        self._bm25_k1 = float(bm25_k1)
        self._bm25_b = float(bm25_b)
        # Unknown until the schema is inspected; False means "old collection, no BM25".
        self._has_sparse: bool | None = None
        self._warned_missing_sparse = False

    def _client_instance(self):
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise KnowledgeUnavailable(
                    "pymilvus is not installed; install the knowledge extra"
                ) from exc
            kwargs: dict[str, Any] = {"uri": self._config.uri}
            if self._config.token:
                kwargs["token"] = self._config.token
            if self._config.db_name:
                kwargs["db_name"] = self._config.db_name
            self._client = MilvusClient(**kwargs)
        return self._client

    async def upsert_document(
        self,
        *,
        user_id: str,
        knowledge_base_id: str,
        knowledge_base_name: str,
        knowledge_base_type: str,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        vectors: Sequence[Sequence[float]],
        parents: Sequence[KnowledgeParentChunk] = (),
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunk and vector counts differ")
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise ValueError(
                    f"vector dimension mismatch: expected {self._dimensions}, got {len(vector)}"
                )
        now = int(time.time())
        rows = [
            {
                "pk": f"{document.id}:{chunk.index}",
                "user_id": user_id,
                "knowledge_base_id": knowledge_base_id,
                "knowledge_base_name": knowledge_base_name[:120],
                "knowledge_base_type": knowledge_base_type,
                "document_id": document.id,
                "chunk_id": f"{document.id}:{chunk.index}",
                "filename": document.filename[:255],
                "section": (chunk.section or "")[:512],
                "page": int(chunk.page or 0),
                "chunk_index": int(chunk.index),
                "parent_id": (chunk.parent_id or "")[:80],
                "content": chunk.content[:65535],
                "embedding": list(vector),
                "created_at": now,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        parent_rows = [
            {
                "parent_id": parent.parent_id[:80],
                "user_id": user_id,
                "knowledge_base_id": knowledge_base_id,
                "document_id": document.id,
                "filename": document.filename[:255],
                "section": (parent.section or "")[:512],
                "page": int(parent.page or 0),
                "content": parent.content[:65535],
                # Milvus requires a vector field even though parents are only
                # fetched by primary key/filter and never searched. Milvus also
                # rejects dimensions below 2, hence this 2-D placeholder.
                "embedding": list(PARENT_PLACEHOLDER_VECTOR),
                "created_at": now,
            }
            for parent in parents
        ]
        if parent_rows:
            await self._to_thread(
                self._upsert_parents_sync, user_id, document.id, parent_rows
            )
        else:
            await self._to_thread(self._delete_parents_sync, user_id, document.id)
        await self._to_thread(self._upsert_sync, user_id, document.id, rows)

    def _upsert_sync(self, user_id: str, document_id: str, rows: list[dict]) -> None:
        client = self._client_instance()
        try:
            self._ensure_collection(client)
            client.delete(
                collection_name=self._config.collection,
                filter=self._document_filter(user_id, document_id),
            )
            if rows:
                client.insert(collection_name=self._config.collection, data=rows)
                if self._config.flush:
                    client.flush(collection_name=self._config.collection)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(f"Milvus write failed: {type(exc).__name__}") from exc

    def _upsert_parents_sync(
        self, user_id: str, document_id: str, rows: list[dict[str, Any]]
    ) -> None:
        client = self._client_instance()
        try:
            self._ensure_parent_collection(client)
            client.delete(
                collection_name=self._parent_collection,
                filter=self._document_filter(user_id, document_id),
            )
            if rows:
                client.insert(collection_name=self._parent_collection, data=rows)
                if self._config.flush:
                    client.flush(collection_name=self._parent_collection)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(
                f"Milvus parent write failed: {type(exc).__name__}"
            ) from exc

    def _delete_parents_sync(self, user_id: str, document_id: str) -> None:
        client = self._client_instance()
        try:
            if not client.has_collection(self._parent_collection):
                return
            client.delete(
                collection_name=self._parent_collection,
                filter=self._document_filter(user_id, document_id),
            )
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(
                f"Milvus parent delete failed: {type(exc).__name__}"
            ) from exc

    async def get_parents(
        self, *, user_id: str, parent_ids: Sequence[str]
    ) -> dict[str, str]:
        ids = [item for item in dict.fromkeys(parent_ids) if item]
        if not ids:
            return {}
        rows: list[dict[str, Any]] = []
        for start in range(0, len(ids), PARENT_QUERY_LIMIT):
            rows.extend(
                await self._to_thread(
                    self._query_parents_sync,
                    self._parent_filter(user_id, ids[start : start + PARENT_QUERY_LIMIT]),
                )
            )
        return {
            str(row.get("parent_id") or ""): str(row.get("content") or "")
            for row in rows
            if row.get("parent_id")
        }

    def _query_parents_sync(self, expression: str) -> list[dict[str, Any]]:
        client = self._client_instance()
        try:
            if not client.has_collection(self._parent_collection):
                return []
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._parent_collection)
            return list(
                client.query(
                    collection_name=self._parent_collection,
                    filter=expression,
                    output_fields=PARENT_OUTPUT_FIELDS,
                    limit=PARENT_QUERY_LIMIT,
                    consistency_level=self._config.consistency_level,
                )
            )
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(
                f"Milvus parent read failed: {type(exc).__name__}"
            ) from exc

    def _ensure_parent_collection(self, client) -> None:
        if client.has_collection(self._parent_collection):
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._parent_collection)
            return
        try:
            from pymilvus import DataType
        except ImportError as exc:
            raise KnowledgeUnavailable(
                "pymilvus is not installed; install the knowledge extra"
            ) from exc
        try:
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("parent_id", DataType.VARCHAR, is_primary=True, max_length=80)
            schema.add_field("user_id", DataType.VARCHAR, max_length=64)
            schema.add_field("knowledge_base_id", DataType.VARCHAR, max_length=64)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("filename", DataType.VARCHAR, max_length=255)
            schema.add_field("section", DataType.VARCHAR, max_length=512)
            schema.add_field("page", DataType.INT64)
            schema.add_field("content", DataType.VARCHAR, max_length=65535)
            schema.add_field(
                "embedding",
                DataType.FLOAT_VECTOR,
                dim=len(PARENT_PLACEHOLDER_VECTOR),
            )
            schema.add_field("created_at", DataType.INT64)
            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type="FLAT",
                metric_type="COSINE",
                params={},
            )
            client.create_collection(
                collection_name=self._parent_collection,
                schema=schema,
                index_params=index_params,
            )
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._parent_collection)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            detail = " ".join(str(exc).split())[:500]
            raise KnowledgeUnavailable(
                f"Milvus parent collection setup failed: "
                f"{type(exc).__name__}: {detail}"
            ) from exc

    @staticmethod
    def _parent_filter(user_id: str, parent_ids: Sequence[str]) -> str:
        ids = ",".join(json.dumps(item) for item in parent_ids)
        return f"user_id == {json.dumps(user_id)} and parent_id in [{ids}]"

    async def list_chunks(
        self, *, user_id: str, document_id: str
    ) -> list[KnowledgeChunk]:
        rows = await self._to_thread(
            self._list_chunks_sync, self._document_filter(user_id, document_id)
        )
        chunks = [
            KnowledgeChunk(
                index=int(row.get("chunk_index") or 0),
                content=str(row.get("content") or ""),
                section=str(row.get("section") or ""),
                page=int(row.get("page") or 0),
                parent_id=str(row.get("parent_id") or "") or None,
            )
            for row in rows
        ]
        chunks.sort(key=lambda chunk: chunk.index)
        return chunks

    def _list_chunks_sync(self, expression: str) -> list[dict[str, Any]]:
        client = self._client_instance()
        try:
            if not client.has_collection(self._config.collection):
                return []
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._config.collection)
            return list(
                client.query(
                    collection_name=self._config.collection,
                    filter=expression,
                    output_fields=CHUNK_LIST_FIELDS,
                    limit=CHUNK_QUERY_LIMIT,
                    consistency_level=self._config.consistency_level,
                )
            )
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(
                f"Milvus chunk read failed: {type(exc).__name__}"
            ) from exc

    async def delete_document(self, *, user_id: str, document_id: str) -> None:
        expression = self._document_filter(user_id, document_id)
        await self._to_thread(self._delete_sync, expression)
        await self._to_thread(self._delete_sync, expression, self._parent_collection)

    async def delete_base(self, *, user_id: str, knowledge_base_id: str) -> None:
        expression = (
            f"user_id == {json.dumps(user_id)} and "
            f"knowledge_base_id == {json.dumps(knowledge_base_id)}"
        )
        await self._to_thread(self._delete_sync, expression)
        await self._to_thread(self._delete_sync, expression, self._parent_collection)

    def _delete_sync(self, expression: str, collection_name: str | None = None) -> None:
        client = self._client_instance()
        target = collection_name or self._config.collection
        try:
            # A failed first ingestion must not create an empty collection just to
            # delete vectors that were never written.
            if not client.has_collection(target):
                return
            client.delete(collection_name=target, filter=expression)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(f"Milvus delete failed: {type(exc).__name__}") from exc

    async def search(
        self,
        *,
        user_id: str,
        vector: Sequence[float],
        base_ids: Sequence[str] | None = None,
        limit: int = 5,
        score_threshold: float | None = None,
    ) -> list[SearchHit]:
        if len(vector) != self._dimensions:
            raise ValueError(
                f"query vector dimension mismatch: expected {self._dimensions}, got {len(vector)}"
            )
        rows = await self._to_thread(
            self._search_sync, self._filter(user_id, base_ids), list(vector), int(limit)
        )
        return self._to_hits(rows, score_threshold=score_threshold)

    async def hybrid_search(
        self,
        *,
        user_id: str,
        vector: Sequence[float],
        keyword_query: str,
        base_ids: Sequence[str] | None = None,
        limit: int = 20,
        rrf_k: int = 60,
    ) -> list[SearchHit]:
        """Dense + BM25 recall fused server-side by RRFRanker (same formula, k configurable)."""

        if len(vector) != self._dimensions:
            raise ValueError(
                f"query vector dimension mismatch: expected {self._dimensions}, got {len(vector)}"
            )
        keyword = keyword_query.strip()
        if not keyword:
            raise ValueError("keyword query must not be empty")
        rows = await self._to_thread(
            self._hybrid_search_sync,
            self._filter(user_id, base_ids),
            list(vector),
            keyword,
            int(limit),
            int(rrf_k),
        )
        # Hybrid scores are RRF fusion scores: a similarity threshold has no meaning here.
        return self._to_hits(rows, score_threshold=None, rrf=True)

    def _search_sync(self, expression: str, vector: list[float], limit: int) -> list[dict]:
        client = self._client_instance()
        try:
            self._ensure_collection(client)
            result = client.search(
                collection_name=self._config.collection,
                data=[vector],
                anns_field="embedding",
                search_params={
                    "metric_type": self._config.metric_type,
                    "params": {},
                },
                limit=limit,
                filter=expression,
                output_fields=list(OUTPUT_FIELDS),
                consistency_level=self._config.consistency_level,
            )
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(f"Milvus search failed: {type(exc).__name__}") from exc
        return self._first_rows(result)

    def _hybrid_search_sync(
        self, expression: str, vector: list[float], keyword_query: str, limit: int, rrf_k: int
    ) -> list[dict]:
        client = self._client_instance()
        try:
            from pymilvus import AnnSearchRequest, RRFRanker
        except ImportError as exc:
            raise KeywordSearchUnavailable(
                "pymilvus has no hybrid search; install a 2.5+ client"
            ) from exc
        try:
            self._ensure_collection(client)
            if self._has_sparse is False:
                raise KeywordSearchUnavailable(
                    "Milvus collection has no BM25 sparse field; recreate the collection"
                )
            # MilvusClient.hybrid_search has no top-level filter: dropping it into kwargs
            # silently disables tenant isolation, so the expression goes on both channels.
            requests = [
                AnnSearchRequest(
                    data=[vector],
                    anns_field="embedding",
                    param={
                        "metric_type": self._config.metric_type,
                        "params": {},
                    },
                    limit=limit,
                    expr=expression,
                ),
                AnnSearchRequest(
                    data=[keyword_query],
                    anns_field="sparse",
                    param={"metric_type": "BM25", "params": {}},
                    limit=limit,
                    expr=expression,
                ),
            ]
            result = client.hybrid_search(
                collection_name=self._config.collection,
                reqs=requests,
                ranker=RRFRanker(rrf_k),
                limit=limit,
                output_fields=list(OUTPUT_FIELDS),
                consistency_level=self._config.consistency_level,
            )
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if "sparse" in message or "bm25" in message:
                raise KeywordSearchUnavailable(
                    "Milvus rejected the keyword channel; recreate the collection with BM25"
                ) from exc
            raise KnowledgeUnavailable(
                f"Milvus hybrid search failed: {type(exc).__name__}"
            ) from exc
        return self._first_rows(result)

    @staticmethod
    def _first_rows(result: Any) -> list[dict]:
        if isinstance(result, dict):
            result = result.get("data") or []
        if not result:
            return []
        first = result[0]
        return list(first) if isinstance(first, list) else list(result)

    def _to_hits(
        self, rows: list[dict], *, score_threshold: float | None, rrf: bool = False
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for row in rows:
            entity = row.get("entity") if isinstance(row, dict) else None
            if not isinstance(entity, dict):
                entity = row if isinstance(row, dict) else {}
            score = float(row.get("distance", row.get("score", 0.0))) if isinstance(row, dict) else 0.0
            if score_threshold is not None:
                if self._config.metric_type == "L2":
                    if score > score_threshold:
                        continue
                elif score < score_threshold:
                    continue
            hits.append(
                SearchHit(
                    chunk_id=str(entity.get("chunk_id") or row.get("id") or ""),
                    document_id=str(entity.get("document_id") or ""),
                    filename=str(entity.get("filename") or ""),
                    section=str(entity.get("section") or ""),
                    content=str(entity.get("content") or ""),
                    score=score,
                    knowledge_base_id=str(entity.get("knowledge_base_id") or ""),
                    knowledge_base_name=str(entity.get("knowledge_base_name") or ""),
                    knowledge_base_type=str(entity.get("knowledge_base_type") or ""),
                    chunk_index=int(entity.get("chunk_index") or 0),
                    page=int(entity.get("page") or 0),
                    parent_id=str(entity.get("parent_id") or ""),
                    rrf_score=score if rrf else None,
                )
            )
        return hits

    def _ensure_collection(self, client) -> None:
        if client.has_collection(self._config.collection):
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._config.collection)
            self._inspect_collection(client)
            return
        self._create_collection(client)
        self._has_sparse = True

    def _inspect_collection(self, client) -> None:
        describe = getattr(client, "describe_collection", None)
        if not callable(describe):
            return
        try:
            description = describe(self._config.collection)
        except Exception:
            logger.debug("Could not inspect Milvus collection schema", exc_info=True)
            return
        fields = description.get("fields", []) if isinstance(description, dict) else []
        names = {field.get("name") for field in fields if isinstance(field, dict)}
        if names:
            if "parent_id" not in names:
                raise KnowledgeUnavailable(
                    "knowledge collection is missing parent_id; stop the gateway, run "
                    "\"python -m backend.scripts.reset_knowledge_milvus --apply\", then "
                    "re-ingest documents (this release drops old knowledge data)"
                )
            self._has_sparse = "sparse" in names
            if not self._has_sparse and not self._warned_missing_sparse:
                self._warned_missing_sparse = True
                logger.warning(
                    "Milvus collection %s has no BM25 sparse field; keyword search is "
                    "skipped until the collection is recreated",
                    self._config.collection,
                )
        for field in fields:
            if not isinstance(field, dict) or field.get("name") != "embedding":
                continue
            params = field.get("params") or {}
            dim = params.get("dim") if isinstance(params, dict) else None
            if dim and int(dim) != self._dimensions:
                raise KnowledgeUnavailable(
                    f"Milvus collection dimension mismatch: expected {self._dimensions}, got {dim}"
                )

    def _create_collection(self, client) -> None:
        try:
            from pymilvus import DataType, Function, FunctionType
        except ImportError as exc:
            raise KnowledgeUnavailable(
                "pymilvus is not installed; install the knowledge extra"
            ) from exc
        try:
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("user_id", DataType.VARCHAR, max_length=64)
            schema.add_field("knowledge_base_id", DataType.VARCHAR, max_length=64)
            schema.add_field("knowledge_base_name", DataType.VARCHAR, max_length=120)
            schema.add_field("knowledge_base_type", DataType.VARCHAR, max_length=24)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("chunk_id", DataType.VARCHAR, max_length=64)
            schema.add_field("filename", DataType.VARCHAR, max_length=255)
            schema.add_field("section", DataType.VARCHAR, max_length=512)
            schema.add_field("page", DataType.INT64)
            schema.add_field("chunk_index", DataType.INT64)
            schema.add_field("parent_id", DataType.VARCHAR, max_length=80)
            schema.add_field(
                "content",
                DataType.VARCHAR,
                max_length=65535,
                enable_analyzer=True,
                analyzer_params={"type": self._keyword_analyzer},
            )
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self._dimensions)
            schema.add_field("created_at", DataType.INT64)
            schema.add_function(
                Function(
                    name="knowledge_bm25",
                    input_field_names=["content"],
                    output_field_names=["sparse"],
                    function_type=FunctionType.BM25,
                )
            )
            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type=self._config.index_type,
                metric_type=self._config.metric_type,
                params={},
            )
            index_params.add_index(
                field_name="sparse",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="BM25",
                params={"bm25_k1": self._bm25_k1, "bm25_b": self._bm25_b},
            )
            client.create_collection(
                collection_name=self._config.collection,
                schema=schema,
                index_params=index_params,
                consistency_level=self._config.consistency_level,
            )
            client.load_collection(self._config.collection)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(
                f"Milvus collection creation failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _document_filter(user_id: str, document_id: str) -> str:
        return (
            f"user_id == {json.dumps(user_id)} and "
            f"document_id == {json.dumps(document_id)}"
        )

    @staticmethod
    def _filter(user_id: str, base_ids: Sequence[str] | None) -> str:
        expression = f"user_id == {json.dumps(user_id)}"
        if base_ids:
            expression += f" and knowledge_base_id in {json.dumps(list(base_ids))}"
        return expression

    @staticmethod
    async def _to_thread(fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def aclose(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
            self._client = None
