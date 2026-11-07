"""Milvus retrieval index for ContextForge tool documents.

The collection is a derived view: Milvus narrows candidates, while the current
MCP snapshot is the authority used by the search service to confirm hits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.config.contextforge_config import (
    TOOLS_COLLECTION,
    ToolKeywordConfig,
    ToolVectorStoreConfig,
)
from agentkit.contextforge.models import ContextForgeTool

logger = logging.getLogger(__name__)

OUTPUT_FIELDS = [
    "pk",
    "tool_name",
    "display_name",
    "description",
    "content",
    "tags",
    "parameters",
    "server",
    "content_hash",
    "updated_at",
]
_VECTOR_FIELD = "embedding"
_SPARSE_FIELD = "sparse"
_QUERY_LIMIT = 16384


class ToolIndexUnavailable(RuntimeError):
    """Milvus cannot serve the tool index."""


class Embedder(Protocol):
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class ToolVectorHit:
    tool_name: str
    score: float
    display_name: str = ""
    description: str = ""
    content: str = ""
    tags: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class ToolIndexSyncStats:
    scanned: int = 0
    changed: int = 0
    upserted: int = 0
    deleted: int = 0
    duration_ms: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCatalogDocument:
    """Projection of ToolCatalogEntry used by the index without a circular import."""

    name: str
    display_name: str
    description: str
    tags: tuple[str, ...]
    parameters: tuple[str, ...]
    server: str
    content: str
    content_hash: str
    updated_at: int


class ToolVectorIndex:
    def __init__(
        self,
        config: ToolVectorStoreConfig,
        *,
        dimensions: int,
        client: Any | None = None,
        keyword: ToolKeywordConfig | None = None,
        rrf_k: int = 60,
    ) -> None:
        if int(dimensions) <= 0:
            raise ValueError("dimensions must be positive")
        if int(rrf_k) <= 0:
            raise ValueError("rrf_k must be positive")
        self._config = config
        self._dimensions = int(dimensions)
        self._collection = TOOLS_COLLECTION
        self._client = client
        self._keyword = keyword or ToolKeywordConfig()
        self._rrf_k = int(rrf_k)

    def _client_instance(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise ToolIndexUnavailable(
                "pymilvus is not installed; install the contextforge extra"
            ) from exc
        kwargs: dict[str, Any] = {"uri": self._config.uri}
        if self._config.token:
            kwargs["token"] = self._config.token
        if self._config.db_name:
            kwargs["db_name"] = self._config.db_name
        self._client = MilvusClient(**kwargs)
        return self._client

    async def ensure_collection(self, dimensions: int | None = None) -> None:
        if dimensions is not None:
            self._dimensions = int(dimensions)
        await asyncio.to_thread(self._ensure_collection_sync)

    async def query_hashes(self, names: Sequence[str]) -> dict[str, str]:
        values = [name for name in dict.fromkeys(names) if name]
        if not values:
            return {}
        existing: dict[str, str] = {}
        for start in range(0, len(values), 500):
            batch = values[start : start + 500]
            rows = await asyncio.to_thread(self._query_hashes_sync, batch)
            for row in rows:
                name = str(row.get("tool_name") or "")
                if name:
                    existing[name] = str(row.get("content_hash") or "")
        return existing

    async def upsert(
        self,
        documents: Sequence[ToolCatalogDocument],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        if len(documents) != len(vectors):
            raise ValueError("documents and vectors must have the same length")
        if not documents:
            return 0
        await asyncio.to_thread(self._upsert_sync, list(documents), [list(v) for v in vectors])
        return len(documents)

    async def delete_not_in(self, names: Sequence[str]) -> int:
        keep = set(names)
        rows = await asyncio.to_thread(self._all_keys_sync)
        stale = [str(row.get("pk") or "") for row in rows if str(row.get("pk") or "") not in keep]
        stale = [name for name in stale if name]
        if not stale:
            return 0
        for start in range(0, len(stale), 500):
            await asyncio.to_thread(self._delete_names_sync, stale[start : start + 500])
        return len(stale)

    async def hybrid_search(
        self,
        *,
        vector: Sequence[float],
        query: str,
        limit: int,
    ) -> list[ToolVectorHit]:
        values = list(vector)
        if len(values) != self._dimensions:
            raise ToolIndexUnavailable(
                f"query vector dimension mismatch: expected {self._dimensions}"
            )
        if not query.strip() or limit <= 0:
            return []
        rows = await asyncio.to_thread(self._hybrid_search_sync, values, query, int(limit))
        return self._to_hits(rows)

    async def dense_search(
        self,
        *,
        vector: Sequence[float],
        limit: int,
    ) -> list[ToolVectorHit]:
        values = list(vector)
        if len(values) != self._dimensions:
            raise ToolIndexUnavailable(
                f"query vector dimension mismatch: expected {self._dimensions}"
            )
        rows = await asyncio.to_thread(self._dense_search_sync, values, int(limit))
        return self._to_hits(rows)

    async def sync(
        self,
        documents: Sequence[ToolCatalogDocument],
        *,
        embedder: Embedder,
        full: bool = False,
    ) -> ToolIndexSyncStats:
        started = time.monotonic()
        try:
            await self.ensure_collection(self._dimensions)
            existing = await self.query_hashes([document.name for document in documents])
            changed = [
                document
                for document in documents
                if existing.get(document.name) != document.content_hash
            ]
            upserted = 0
            if changed:
                vectors = await embedder.embed_documents([document.content for document in changed])
                upserted = await self.upsert(changed, vectors)
            deleted = await self.delete_not_in([document.name for document in documents]) if full else 0
            return ToolIndexSyncStats(
                scanned=len(documents),
                changed=len(changed),
                upserted=upserted,
                deleted=deleted,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as status, never a crash
            logger.warning("Tool index sync failed: %s", type(exc).__name__)
            return ToolIndexSyncStats(
                scanned=len(documents),
                duration_ms=int((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
            )

    async def aclose(self) -> None:
        client = self._client
        if client is None:
            return
        self._client = None
        close = getattr(client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    # ------------------------------------------------------------------
    # synchronous Milvus operations
    # ------------------------------------------------------------------

    def _ensure_collection_sync(self) -> None:
        client = self._client_instance()
        try:
            if not client.has_collection(self._collection):
                self._create_collection(client)
                return
            self._inspect_collection(client)
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._collection)
        except ToolIndexUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool collection setup failed: {type(exc).__name__}"
            ) from exc

    def _inspect_collection(self, client: Any) -> None:
        describe = getattr(client, "describe_collection", None)
        if not callable(describe):
            return
        try:
            description = describe(self._collection)
        except Exception:  # noqa: BLE001
            return
        fields = description.get("fields", []) if isinstance(description, dict) else []
        names = {field.get("name") for field in fields if isinstance(field, dict)}
        if _SPARSE_FIELD not in names:
            raise ToolIndexUnavailable(
                "Milvus tool collection has no BM25 sparse field; recreate it"
            )
        for field in fields:
            if not isinstance(field, dict) or field.get("name") != _VECTOR_FIELD:
                continue
            dim = (field.get("params") or {}).get("dim")
            if dim and int(dim) != self._dimensions:
                raise ToolIndexUnavailable(
                    "Milvus tool collection dimension mismatch: "
                    f"expected {self._dimensions}, got {dim}"
                )

    def _create_collection(self, client: Any) -> None:
        try:
            from pymilvus import DataType, Function, FunctionType
        except ImportError as exc:
            raise ToolIndexUnavailable("pymilvus is not installed") from exc
        try:
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=255)
            schema.add_field("tool_name", DataType.VARCHAR, max_length=255)
            schema.add_field("display_name", DataType.VARCHAR, max_length=512)
            schema.add_field("description", DataType.VARCHAR, max_length=65535)
            schema.add_field(
                "content",
                DataType.VARCHAR,
                max_length=65535,
                enable_analyzer=True,
                analyzer_params={"type": self._keyword.analyzer},
            )
            schema.add_field("tags", DataType.VARCHAR, max_length=2048)
            schema.add_field("parameters", DataType.VARCHAR, max_length=2048)
            schema.add_field("server", DataType.VARCHAR, max_length=128)
            schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
            schema.add_field("updated_at", DataType.INT64)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self._dimensions)
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_function(
                Function(
                    name="tools_bm25",
                    input_field_names=["content"],
                    output_field_names=["sparse"],
                    function_type=FunctionType.BM25,
                )
            )
            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name=_VECTOR_FIELD,
                index_type=self._config.index_type,
                metric_type=self._config.metric_type,
                params={},
            )
            index_params.add_index(
                field_name=_SPARSE_FIELD,
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="BM25",
                params={
                    "bm25_k1": self._keyword.bm25_k1,
                    "bm25_b": self._keyword.bm25_b,
                },
            )
            client.create_collection(
                collection_name=self._collection,
                schema=schema,
                index_params=index_params,
                consistency_level=self._config.consistency_level,
            )
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._collection)
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool collection creation failed: {type(exc).__name__}"
            ) from exc

    def _query_hashes_sync(self, names: list[str]) -> list[dict[str, Any]]:
        client = self._client_instance()
        if not client.has_collection(self._collection):
            return []
        expression = "tool_name in " + json.dumps(names)
        try:
            return list(
                client.query(
                    collection_name=self._collection,
                    filter=expression,
                    output_fields=["tool_name", "content_hash"],
                    limit=len(names),
                    consistency_level=self._config.consistency_level,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool hash query failed: {type(exc).__name__}"
            ) from exc

    def _upsert_sync(self, documents: list[ToolCatalogDocument], vectors: list[list[float]]) -> None:
        client = self._client_instance()
        self._ensure_collection_sync()
        records = [
            {
                "pk": document.name,
                "tool_name": document.name,
                "display_name": document.display_name[:512],
                "description": document.description[:65535],
                "content": document.content[:65535],
                "tags": json.dumps(list(document.tags), ensure_ascii=False)[:2048],
                "parameters": json.dumps(list(document.parameters), ensure_ascii=False)[:2048],
                "server": document.server[:128],
                "content_hash": document.content_hash,
                "updated_at": document.updated_at,
                "embedding": vector,
            }
            for document, vector in zip(documents, vectors, strict=True)
        ]
        try:
            for start in range(0, len(records), 64):
                client.upsert(collection_name=self._collection, data=records[start : start + 64])
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool upsert failed: {type(exc).__name__}"
            ) from exc

    def _all_keys_sync(self) -> list[dict[str, Any]]:
        client = self._client_instance()
        if not client.has_collection(self._collection):
            return []
        make_iterator = getattr(client, "query_iterator", None)
        if not callable(make_iterator):
            return list(client.query(collection_name=self._collection, filter="", output_fields=["pk"], limit=_QUERY_LIMIT))
        iterator = make_iterator(
            collection_name=self._collection,
            batch_size=1000,
            limit=-1,
            filter="",
            output_fields=["pk"],
        )
        rows: list[dict[str, Any]] = []
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                rows.extend(batch)
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
        return rows

    def _delete_names_sync(self, names: list[str]) -> None:
        client = self._client_instance()
        if not client.has_collection(self._collection):
            return
        expression = "pk in " + json.dumps(names)
        client.delete(collection_name=self._collection, filter=expression)

    def _hybrid_search_sync(self, vector: list[float], query: str, limit: int) -> list[dict[str, Any]]:
        client = self._client_instance()
        self._ensure_collection_sync()
        try:
            from pymilvus import AnnSearchRequest, RRFRanker
        except ImportError as exc:
            raise ToolIndexUnavailable("pymilvus has no hybrid search") from exc
        requests = [
            AnnSearchRequest(
                data=[vector],
                anns_field=_VECTOR_FIELD,
                param={"metric_type": self._config.metric_type, "params": {}},
                limit=limit,
            ),
            AnnSearchRequest(
                data=[query],
                anns_field=_SPARSE_FIELD,
                param={"metric_type": "BM25", "params": {}},
                limit=limit,
            ),
        ]
        try:
            return _first_rows(
                client.hybrid_search(
                    collection_name=self._collection,
                    reqs=requests,
                    ranker=RRFRanker(self._rrf_k),
                    limit=limit,
                    output_fields=OUTPUT_FIELDS,
                    consistency_level=self._config.consistency_level,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool hybrid search failed: {type(exc).__name__}"
            ) from exc

    def _dense_search_sync(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        client = self._client_instance()
        self._ensure_collection_sync()
        try:
            return _first_rows(
                client.search(
                    collection_name=self._collection,
                    data=[vector],
                    anns_field=_VECTOR_FIELD,
                    search_params={"metric_type": self._config.metric_type, "params": {}},
                    limit=limit,
                    output_fields=OUTPUT_FIELDS,
                    consistency_level=self._config.consistency_level,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool dense search failed: {type(exc).__name__}"
            ) from exc

    def _to_hits(self, rows: list[dict[str, Any]]) -> list[ToolVectorHit]:
        hits: list[ToolVectorHit] = []
        for row in rows:
            entity = row.get("entity") if isinstance(row, dict) else None
            if not isinstance(entity, dict):
                entity = row if isinstance(row, dict) else {}
            name = str(entity.get("tool_name") or "")
            if not name:
                continue
            hits.append(
                ToolVectorHit(
                    tool_name=name,
                    score=float(row.get("distance", row.get("score", 0.0)) or 0.0),
                    display_name=str(entity.get("display_name") or ""),
                    description=str(entity.get("description") or ""),
                    content=str(entity.get("content") or ""),
                    tags=tuple(json.loads(str(entity.get("tags") or "[]"))),
                    parameters=tuple(json.loads(str(entity.get("parameters") or "[]"))),
                    content_hash=str(entity.get("content_hash") or ""),
                )
            )
        return hits


def _first_rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        result = result.get("data") or []
    if not result:
        return []
    first = result[0]
    return list(first) if isinstance(first, list) else list(result)


__all__ = [
    "Embedder",
    "ToolCatalogDocument",
    "ToolIndexSyncStats",
    "ToolIndexUnavailable",
    "ToolVectorHit",
    "ToolVectorIndex",
]
