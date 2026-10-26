"""Milvus active projection and the PostgreSQL outbox projector.

Milvus is derived data.  Every event is rechecked against PostgreSQL before it
is applied; a stale revision, a deleted row, or a different clear epoch is
settled without resurrecting anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from agentkit.agents.memory.canonical.domain import MemoryRecordView
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.config.memory_config import MemoryConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryProjectionRow:
    pk: str
    user_id: str
    memory_id: str
    kind: str
    status: str
    topic_key: str
    scope_condition: str
    valid_from: int
    valid_to: int
    occurred_at: int
    confidence: float
    salience: float
    last_active_at: int
    explicit_remember: bool
    indexed_revision: int
    embedding: list[float]
    embedding_model: str
    embedding_dimensions: int
    updated_at: int


class MemoryMilvusProjection:
    def __init__(
        self,
        config: Any,
        *,
        dimensions: int,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._dimensions = int(dimensions)
        self._client = client
        self._owns_client = client is None

    def _client_instance(self) -> Any:
        if self._client is None:
            from pymilvus import MilvusClient

            kwargs: dict[str, Any] = {"uri": self._config.uri}
            if getattr(self._config, "token", ""):
                kwargs["token"] = self._config.token
            self._client = MilvusClient(**kwargs)
        return self._client

    async def ensure_collection(self) -> None:
        await asyncio.to_thread(self._ensure_sync)

    def _ensure_sync(self) -> None:
        client = self._client_instance()
        if client.has_collection(self._config.collection):
            return
        raise RuntimeError(
            "memory Milvus collection is missing; create it with the configured "
            "schema and dimensions before enabling memory"
        )

    async def upsert(self, row: MemoryProjectionRow) -> None:
        await asyncio.to_thread(self._upsert_sync, row)

    def _upsert_sync(self, row: MemoryProjectionRow) -> None:
        client = self._client_instance()
        client.upsert(collection_name=self._config.collection, data=[asdict(row)])

    async def delete(self, pk: str) -> None:
        await asyncio.to_thread(
            self._client_instance().delete,
            collection_name=self._config.collection,
            filter=f"pk == {json.dumps(pk)}",
        )

    async def search(
        self,
        *,
        user_id: str,
        kinds: list[str],
        vector: list[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        if len(vector) != self._dimensions:
            raise ValueError("query vector dimension mismatch")
        kind_list = ", ".join(json.dumps(kind) for kind in kinds)
        expression = (
            f"user_id == {json.dumps(user_id)} and kind in [{kind_list}] "
            'and status == "active" and valid_to == 0'
        )
        return await asyncio.to_thread(
            self._client_instance().search,
            collection_name=self._config.collection,
            data=[list(vector)],
            anns_field="embedding",
            search_params={"metric_type": self._config.metric_type, "params": {}},
            limit=int(limit),
            filter=expression,
            output_fields=[
                "pk",
                "user_id",
                "memory_id",
                "kind",
                "status",
                "topic_key",
                "valid_to",
                "indexed_revision",
            ],
            consistency_level=self._config.consistency_level,
        )

    async def list_pks(self) -> list[str]:
        result = await asyncio.to_thread(
            self._client_instance().query,
            collection_name=self._config.collection,
            filter='status == "active"',
            output_fields=["pk"],
            limit=16384,
        )
        return [str(row.get("pk")) for row in result or [] if row.get("pk")]

    async def close(self) -> None:
        if self._owns_client and self._client is not None and hasattr(self._client, "close"):
            await asyncio.to_thread(self._client.close)
            self._client = None


class MemoryProjector:
    def __init__(
        self,
        repo: MemoryRepository,
        projection: MemoryMilvusProjection,
        embedder: Any,
        config: MemoryConfig,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self._repo = repo
        self.projection = projection
        self._embedder = embedder
        self._config = config
        self._dimensions = int(
            getattr(embedder, "dimensions", config.embedding.dimensions)
        )
        self._poll_seconds = float(poll_seconds)
        self._task: asyncio.Task[None] | None = None

    async def run_once(self, limit: int = 50) -> int:
        events = await self._repo.claim_outbox(limit)
        for event in events:
            try:
                await self._process_event(event)
                await self._repo.finish_outbox(event.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider failures are retryable
                if event.attempts >= 5:
                    await self._repo.finish_outbox(event.id)
                    logger.error(
                        "memory outbox event %s exhausted retries (%s)",
                        event.id,
                        type(exc).__name__,
                    )
                else:
                    await self._repo.retry_outbox(
                        event.id,
                        error=type(exc).__name__,
                        delay_seconds=min(300.0, 2**max(0, event.attempts - 1)),
                    )
        return len(events)

    async def _process_event(self, event: Any) -> None:
        record = await self._repo.get_record(event.user_id, event.memory_id)
        current_epoch = await self._repo.get_epoch(event.user_id)
        if event.epoch != current_epoch:
            if record is not None and record.status == "active":
                await self.projection.delete(event.pk)
            return
        if event.op == "delete":
            await self.projection.delete(event.pk)
            return
        if (
            record is None
            or record.status != "active"
            or record.revision != event.revision
        ):
            if record is not None and record.status != "active":
                await self.projection.delete(event.pk)
            return
        vector = (await self._embedder.embed_documents([record.content]))[0]
        if len(vector) != self._dimensions:
            raise ValueError("embedding dimension mismatch")
        await self.projection.upsert(self._row(record, vector))

    async def reindex(self, user_id: str | None = None) -> int:
        records = await self._repo.list_active_records(user_id, limit=100_000)
        if not records:
            return 0
        vectors = await self._embedder.embed_documents([record.content for record in records])
        for record, vector in zip(records, vectors, strict=True):
            if len(vector) != self._dimensions:
                raise ValueError("embedding dimension mismatch")
            await self.projection.upsert(self._row(record, vector))
        return len(records)

    def _row(self, record: MemoryRecordView, vector: list[float]) -> MemoryProjectionRow:
        return MemoryProjectionRow(
            pk=f"{record.user_id}:{record.id}",
            user_id=record.user_id,
            memory_id=record.id,
            kind=record.kind,
            status=record.status,
            topic_key=record.topic_key or "",
            scope_condition=record.scope_condition or "global",
            valid_from=_epoch(record.valid_from),
            valid_to=_epoch(record.valid_to),
            occurred_at=_epoch(record.occurred_at),
            confidence=record.confidence,
            salience=record.salience,
            last_active_at=_epoch(record.last_active_at),
            explicit_remember=record.explicit_remember,
            indexed_revision=record.revision,
            embedding=list(vector),
            embedding_model=self._config.embedding.model,
            embedding_dimensions=self._dimensions,
            updated_at=int(time.time()),
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="memory-index-outbox")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the poll loop must survive
                logger.warning("memory outbox poll failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._poll_seconds)


def _epoch(value: datetime | None) -> int:
    return int(value.timestamp()) if value is not None else 0
