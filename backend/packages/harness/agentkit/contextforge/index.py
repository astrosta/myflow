"""Milvus retrieval index for the ContextForge tool catalog.

``contextforge_tools`` is the authoritative catalog; this collection is a
derived view rebuilt from ``ContextForgeToolRecord`` rows and keyed by
ContextForge's own tool id.  Two rules shape this module:

* the index stores only what hybrid retrieval needs.  ``content`` is the BM25
  and embedding text, and the verbatim ``input_schema`` stays in the database,
  so a search hit carries a ``tool_id`` back to the store instead of a schema
  snapshot that may already be stale;
* every search is owner-scoped.  ``MilvusClient.hybrid_search`` has no
  top-level ``filter``, so the expression rides on both AnnSearchRequests:
  filtering only the dense channel would let RRFRanker fuse another user's tools
  into the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.config.contextforge_config import ToolKeywordConfig, ToolVectorStoreConfig
from agentkit.contextforge.store import ContextForgeToolRecord, content_hash_for

logger = logging.getLogger(__name__)

#: Fixed by the design: the derived tool index never shares the knowledge or
#: skill collections.  There is no historical data to migrate, so a schema
#: change is a drop and rebuild.
COLLECTION_NAME = "myflow_contextforge_tools"

#: What a hit needs to be joined back to ``contextforge_tools`` and displayed.
#: The full ``input_schema`` is deliberately absent: it lives in the database.
OUTPUT_FIELDS = [
    "tool_id",
    "tool_name",
    "call_name",
    "description",
    "tags",
    "parameters",
    "content",
    "content_hash",
]
_OWNER_FIELD = "owner_user_id"
_VECTOR_FIELD = "embedding"
_SPARSE_FIELD = "sparse"
#: Fields whose absence means the collection predates this schema.
_SCALAR_FIELDS = (
    "tool_id",
    "owner_user_id",
    "tool_name",
    "call_name",
    "description",
    "tags",
    "parameters",
    "content",
    "content_hash",
)
_QUERY_LIMIT = 16384
_WRITE_BATCH = 64
_DELETE_BATCH = 500


class ToolIndexUnavailable(RuntimeError):
    """Milvus cannot serve the tool index."""


class Embedder(Protocol):
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class ToolVectorHit:
    """One recalled tool row.

    ``tool_name``/``score`` keep their historical position so name-based
    ranking code stays valid; ``tool_id`` and ``call_name`` are the join keys
    the database row behind the hit is confirmed with.
    """

    tool_name: str
    score: float
    tool_id: str = ""
    call_name: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()
    content: str = ""
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class ToolIndexSyncStats:
    scanned: int = 0
    changed: int = 0
    upserted: int = 0
    deleted: int = 0
    duration_ms: int = 0
    error: str | None = None


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
        # Pinned here rather than read from config: the tool index is this one
        # collection, whatever a stale config block still says.
        self._collection = COLLECTION_NAME
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

    async def query_hashes(self, tool_ids: Sequence[str]) -> dict[str, str]:
        """Stored content_hash per tool id; missing tools are simply absent."""
        values = [str(tool_id) for tool_id in dict.fromkeys(tool_ids) if _text(tool_id)]
        existing: dict[str, str] = {}
        for start in range(0, len(values), _DELETE_BATCH):
            batch = values[start : start + _DELETE_BATCH]
            rows = await asyncio.to_thread(self._query_hashes_sync, batch)
            for row in rows:
                tool_id = _text(row.get("tool_id"))
                if tool_id:
                    existing[tool_id] = _text(row.get("content_hash"))
        return existing

    async def upsert(
        self,
        records: Sequence[ContextForgeToolRecord],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        if len(records) != len(vectors):
            raise ValueError("records and vectors must have the same length")
        if not records:
            return 0
        await asyncio.to_thread(
            self._upsert_sync, list(records), [list(vector) for vector in vectors]
        )
        return len(records)

    async def delete_by_ids(self, tool_ids: Sequence[str]) -> int:
        """Delete the given tool ids; the count is what the caller asked to drop."""
        values = [str(tool_id) for tool_id in dict.fromkeys(tool_ids) if _text(tool_id)]
        deleted = 0
        for start in range(0, len(values), _DELETE_BATCH):
            batch = values[start : start + _DELETE_BATCH]
            await asyncio.to_thread(self._delete_ids_sync, batch)
            deleted += len(batch)
        return deleted

    async def delete_not_in(self, tool_ids: Sequence[str]) -> int:
        """Delete every stored row whose tool id is absent from ``tool_ids``."""
        keep = {str(tool_id) for tool_id in tool_ids if _text(tool_id)}
        stored = await asyncio.to_thread(self._all_tool_ids_sync)
        stale = [tool_id for tool_id in stored if tool_id not in keep]
        return await self.delete_by_ids(stale) if stale else 0

    async def hybrid_search(
        self,
        *,
        vector: Sequence[float],
        query: str,
        owner_user_id: str | None,
        limit: int,
    ) -> list[ToolVectorHit]:
        expression = _owner_expression(owner_user_id)
        values = list(vector)
        if len(values) != self._dimensions:
            raise ToolIndexUnavailable(
                f"query vector dimension mismatch: expected {self._dimensions}"
            )
        text = (query or "").strip()
        if not text or int(limit) <= 0:
            return []
        rows = await asyncio.to_thread(
            self._hybrid_search_sync, values, text, expression, int(limit)
        )
        return self._to_hits(rows)

    async def dense_search(
        self,
        *,
        vector: Sequence[float],
        owner_user_id: str | None,
        limit: int,
    ) -> list[ToolVectorHit]:
        expression = _owner_expression(owner_user_id)
        values = list(vector)
        if len(values) != self._dimensions:
            raise ToolIndexUnavailable(
                f"query vector dimension mismatch: expected {self._dimensions}"
            )
        if int(limit) <= 0:
            return []
        rows = await asyncio.to_thread(
            self._dense_search_sync, values, expression, int(limit)
        )
        return self._to_hits(rows)

    async def sync(
        self,
        records: Sequence[ContextForgeToolRecord],
        *,
        embedder: Embedder,
        full: bool = False,
    ) -> ToolIndexSyncStats:
        """Bring the collection in line with ``records``, embedding only what changed.

        Deletion follows the tool id in both modes: a ``full`` sync drops every
        stored row the snapshot no longer indexes, and an incremental one drops
        the rows of the records it was handed that stopped being indexable (a
        tool that stopped being callable, lost its owner, was disabled or became
        unreachable).  Tools the sync service decides to retire are removed
        through ``delete_by_ids``.

        Because a full sync deletes every row outside the snapshot's tool ids, a
        non-empty snapshot in which no record carries a usable id is refused
        with an error stat instead of emptying the collection.  That is the
        shape produced by a caller still handing the pre-rev.9 catalog entries
        (no tool id, no call name, no owner).  An all-non-callable catalog is a
        real snapshot and still prunes, because its records do carry ids.
        """
        started = time.monotonic()
        try:
            if records and not any(_carries_tool_id(record) for record in records):
                logger.warning(
                    "Tool index sync refused: %d records carry no tool id", len(records)
                )
                return ToolIndexSyncStats(
                    scanned=len(records),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error="UnrecognizedToolRecords",
                )
            await self.ensure_collection(self._dimensions)
            indexable = [record for record in records if _is_indexable(record)]
            keep = {str(record.id) for record in indexable}
            stored = await self.query_hashes(
                [str(record.id) for record in records if _text(getattr(record, "id", ""))]
            )
            changed = [
                record
                for record in indexable
                if stored.get(str(record.id)) != _content_hash(record)
            ]
            upserted = 0
            if changed:
                vectors = await embedder.embed_documents(
                    [str(record.content or "") for record in changed]
                )
                upserted = await self.upsert(changed, vectors)
            if full:
                deleted = await self.delete_not_in(keep)
            else:
                deleted = await self.delete_by_ids(
                    [tool_id for tool_id in stored if tool_id not in keep]
                )
            return ToolIndexSyncStats(
                scanned=len(records),
                changed=len(changed),
                upserted=upserted,
                deleted=deleted,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as status, never a crash
            logger.warning("Tool index sync failed: %s", type(exc).__name__)
            return ToolIndexSyncStats(
                scanned=len(records),
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
        missing = [name for name in (*_SCALAR_FIELDS, _SPARSE_FIELD) if name not in names]
        if missing:
            raise ToolIndexUnavailable(
                "Milvus tool collection is missing "
                + ", ".join(missing)
                + "; drop and recreate it"
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
            schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("tool_id", DataType.VARCHAR, max_length=64)
            schema.add_field(_OWNER_FIELD, DataType.VARCHAR, max_length=64)
            schema.add_field("tool_name", DataType.VARCHAR, max_length=255)
            schema.add_field("call_name", DataType.VARCHAR, max_length=255)
            schema.add_field("description", DataType.VARCHAR, max_length=65535)
            schema.add_field("tags", DataType.VARCHAR, max_length=2048)
            schema.add_field("parameters", DataType.VARCHAR, max_length=2048)
            schema.add_field(
                "content",
                DataType.VARCHAR,
                max_length=65535,
                enable_analyzer=True,
                analyzer_params={"type": self._keyword.analyzer},
            )
            schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
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

    def _query_hashes_sync(self, tool_ids: list[str]) -> list[dict[str, Any]]:
        client = self._client_instance()
        if not client.has_collection(self._collection):
            return []
        expression = "tool_id in " + json.dumps(tool_ids)
        try:
            return list(
                client.query(
                    collection_name=self._collection,
                    filter=expression,
                    output_fields=["tool_id", "content_hash"],
                    limit=len(tool_ids),
                    consistency_level=self._config.consistency_level,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool hash query failed: {type(exc).__name__}"
            ) from exc

    def _upsert_sync(
        self, records: list[ContextForgeToolRecord], vectors: list[list[float]]
    ) -> None:
        client = self._client_instance()
        self._ensure_collection_sync()
        rows = [
            _row(record, vector)
            for record, vector in zip(records, vectors, strict=True)
        ]
        try:
            for start in range(0, len(rows), _WRITE_BATCH):
                client.upsert(
                    collection_name=self._collection, data=rows[start : start + _WRITE_BATCH]
                )
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool upsert failed: {type(exc).__name__}"
            ) from exc

    def _all_tool_ids_sync(self) -> list[str]:
        client = self._client_instance()
        if not client.has_collection(self._collection):
            return []
        make_iterator = getattr(client, "query_iterator", None)
        if not callable(make_iterator):
            rows = client.query(
                collection_name=self._collection,
                filter="",
                output_fields=["tool_id"],
                limit=_QUERY_LIMIT,
            )
            return [tool_id for tool_id in (_text(row.get("tool_id")) for row in rows) if tool_id]
        iterator = make_iterator(
            collection_name=self._collection,
            batch_size=1000,
            limit=-1,
            filter="",
            output_fields=["tool_id"],
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
        return [tool_id for tool_id in (_text(row.get("tool_id")) for row in rows) if tool_id]

    def _delete_ids_sync(self, tool_ids: list[str]) -> None:
        client = self._client_instance()
        if not client.has_collection(self._collection):
            return
        expression = "tool_id in " + json.dumps(tool_ids)
        try:
            client.delete(collection_name=self._collection, filter=expression)
        except Exception as exc:  # noqa: BLE001
            raise ToolIndexUnavailable(
                f"Milvus tool delete failed: {type(exc).__name__}"
            ) from exc

    def _hybrid_search_sync(
        self, vector: list[float], query: str, expression: str, limit: int
    ) -> list[dict[str, Any]]:
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
                expr=expression,
            ),
            AnnSearchRequest(
                data=[query],
                anns_field=_SPARSE_FIELD,
                param={"metric_type": "BM25", "params": {}},
                limit=limit,
                expr=expression,
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

    def _dense_search_sync(
        self, vector: list[float], expression: str, limit: int
    ) -> list[dict[str, Any]]:
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
                    filter=expression,
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
            tool_id = _text(entity.get("tool_id"))
            tool_name = _text(entity.get("tool_name"))
            if not tool_id and not tool_name:
                continue
            hits.append(
                ToolVectorHit(
                    tool_name=tool_name,
                    score=float(row.get("distance", row.get("score", 0.0)) or 0.0),
                    tool_id=tool_id,
                    call_name=_text(entity.get("call_name")),
                    description=str(entity.get("description") or ""),
                    tags=_json_tuple(entity.get("tags")),
                    parameters=_json_tuple(entity.get("parameters")),
                    content=str(entity.get("content") or ""),
                    content_hash=_text(entity.get("content_hash")),
                )
            )
        return hits


def _row(record: ContextForgeToolRecord, vector: Sequence[float]) -> dict[str, Any]:
    """The stored shape of one record: retrieval fields only, never the schema."""
    tool_id = _text(record.id)
    return {
        "pk": tool_id,
        "tool_id": tool_id,
        "owner_user_id": _text(record.owner_user_id),
        "tool_name": _text(record.name)[:255],
        "call_name": _text(record.call_name)[:255],
        "description": str(record.description or "")[:65535],
        "tags": _json_list(record.tags)[:2048],
        "parameters": _json_list(record.parameters)[:2048],
        "content": str(record.content or "")[:65535],
        "content_hash": _content_hash(record),
        "embedding": list(vector),
    }


def _owner_expression(owner_user_id: str | None) -> str:
    """The owner filter every search carries; an unscoped read is a bug.

    The owner is quoted as a JSON string literal, so a user id can never append
    a second predicate to the expression.
    """
    owner = _text(owner_user_id)
    if not owner:
        raise ValueError(
            "owner_user_id is required: tool retrieval is always owner-scoped"
        )
    return f"{_OWNER_FIELD} == {json.dumps(owner, ensure_ascii=False)}"


def _carries_tool_id(record: Any) -> bool:
    """Does this even look like a tool record?

    The sync contract is the store's ``ContextForgeToolRecord``, whose ``id``
    is the collection key.  A caller handing the old catalog entries has no
    ``id`` attribute at all, which is how ``sync`` tells "the catalog is empty"
    apart from "these are the wrong objects".
    """
    return bool(_text(getattr(record, "id", "")))


def _is_indexable(record: Any) -> bool:
    """Only owner-scoped, callable tools are retrievable.

    ``callable`` is the store's derived runtime gate, so it already folds in
    enabled/reachable/owner/name rules; the explicit checks here keep the index
    safe when it is handed a record that never went through the store.  A row
    that stops qualifying is removed through ``delete_by_ids`` (or a full sync),
    and the database join behind tool_search discards anything Milvus still
    remembers.
    """
    return bool(
        _text(getattr(record, "id", ""))
        and _text(getattr(record, "call_name", ""))
        and _text(getattr(record, "owner_user_id", ""))
        and getattr(record, "callable", False)
        and getattr(record, "enabled", True)
        and getattr(record, "reachable", True)
    )


def _content_hash(record: Any) -> str:
    """The diff key: sha256(content + owner + call_name), recomputed if absent.

    The owner and call_name are inside the hash, so moving a tool between users
    re-embeds it even when its text is unchanged.
    """
    stored = _text(getattr(record, "content_hash", ""))
    if stored:
        return stored
    return content_hash_for(
        str(getattr(record, "content", "") or ""),
        _text(getattr(record, "owner_user_id", "")) or None,
        str(getattr(record, "call_name", "") or ""),
    )


def _json_list(values: Sequence[str] | None) -> str:
    return json.dumps(list(values or ()), ensure_ascii=False)


def _json_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        result = result.get("data") or []
    if not result:
        return []
    first = result[0]
    return list(first) if isinstance(first, list) else list(result)


__all__ = [
    "COLLECTION_NAME",
    "Embedder",
    "OUTPUT_FIELDS",
    "ToolIndexSyncStats",
    "ToolIndexUnavailable",
    "ToolVectorHit",
    "ToolVectorIndex",
]
