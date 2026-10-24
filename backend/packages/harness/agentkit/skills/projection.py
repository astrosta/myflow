"""Milvus active projection for public and private skills.

The collection is a materialized view, not a version index: one row per active
public skill and one row per active private skill.  PostgreSQL remains the
authority; Milvus only narrows the candidate set and every hit is confirmed
against the active pointers before it becomes visible.

pymilvus is imported lazily so importing the catalog does not require the
optional Milvus client until the active projection is actually used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.knowledge.config import MilvusConfig

logger = logging.getLogger(__name__)

ACTIVE_COLLECTION = "myflow_skills_active"

OUTPUT_FIELDS = [
    "pk",
    "scope",
    "owner_user_id",
    "skill_id",
    "version_id",
    "skill_version",
    "enabled",
    "name",
    "description",
    "package_sha256",
    "skill_md_sha256",
]

_QUERY_LIMIT = 16384
_SCOPES = frozenset({"public", "private"})
_SCALAR_INDEX_FIELDS = ("scope", "owner_user_id", "enabled")


class ProjectionUnavailable(RuntimeError):
    """The active Milvus projection cannot be read or written."""


@dataclass(frozen=True, slots=True)
class ProjectionRow:
    """One active public or private skill row."""

    projection_key: str
    scope: str
    owner_user_id: str | None
    skill_id: str
    version_id: str
    skill_version: str
    enabled: bool
    name: str
    description: str
    package_sha256: str
    skill_md_sha256: str
    embedding: Sequence[float]


@dataclass(frozen=True, slots=True)
class ProjectionHit:
    """A vector hit, before PostgreSQL confirms the active pointer."""

    projection_key: str
    scope: str
    owner_user_id: str | None
    skill_id: str
    version_id: str
    score: float


class SkillProjectionStore(Protocol):
    async def ensure_collection(self, dimensions: int) -> None: ...

    async def upsert_active(self, row: ProjectionRow) -> None: ...

    async def delete_active(self, projection_key: str) -> None: ...

    async def search(
        self,
        *,
        user_id: str,
        private_skill_ids: Sequence[str],
        vector: Sequence[float],
        limit: int,
    ) -> list[ProjectionHit]: ...

    async def list_projection_keys(self) -> list[str]: ...

    async def aclose(self) -> None: ...


class MilvusSkillProjection:
    """Milvus implementation of the active skill projection."""

    def __init__(
        self,
        config: MilvusConfig,
        *,
        dimensions: int,
        client: Any | None = None,
    ) -> None:
        self._dimensions = int(dimensions)
        if self._dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self._config = config
        # MilvusConfig's own default belongs to the knowledge collection.  The
        # projection uses its active collection unless the caller configured one.
        self._collection = (
            config.collection
            if "collection" in config.model_fields_set
            else ACTIVE_COLLECTION
        )
        # Strong consistency is the projection contract; an explicit deployment
        # setting still wins.
        self._consistency_level = (
            config.consistency_level
            if "consistency_level" in config.model_fields_set
            else "Strong"
        )
        self._client = client

    def _client_instance(self):
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise ProjectionUnavailable(
                    "pymilvus is not installed; install the skills extra"
                ) from exc
            kwargs: dict[str, Any] = {"uri": self._config.uri}
            if self._config.token:
                kwargs["token"] = self._config.token
            if self._config.db_name:
                kwargs["db_name"] = self._config.db_name
            self._client = MilvusClient(**kwargs)
        return self._client

    async def ensure_collection(self, dimensions: int) -> None:
        value = int(dimensions)
        if value <= 0:
            raise ValueError("dimensions must be positive")
        self._dimensions = value
        await asyncio.to_thread(self._ensure_collection_sync)

    async def upsert_active(self, row: ProjectionRow) -> None:
        record = self._record(row)
        await asyncio.to_thread(self._upsert_sync, record)

    async def delete_active(self, projection_key: str) -> None:
        if not projection_key:
            raise ValueError("projection_key must not be blank")
        expression = f"pk == {json.dumps(projection_key)}"
        await asyncio.to_thread(self._delete_sync, expression)

    async def search(
        self,
        *,
        user_id: str,
        private_skill_ids: Sequence[str],
        vector: Sequence[float],
        limit: int,
    ) -> list[ProjectionHit]:
        values = list(vector)
        if len(values) != self._dimensions:
            raise ValueError(
                f"query vector dimension mismatch: expected {self._dimensions}, "
                f"got {len(values)}"
            )
        if int(limit) <= 0:
            raise ValueError("limit must be positive")
        expression = self._filter(user_id, private_skill_ids)
        rows = await asyncio.to_thread(
            self._search_sync, expression, values, int(limit)
        )
        return self._to_hits(rows)

    async def list_projection_keys(self) -> list[str]:
        rows = await asyncio.to_thread(self._list_keys_sync)
        return [
            str(row["pk"])
            for row in rows
            if isinstance(row, dict) and row.get("pk")
        ]

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

    def _upsert_sync(self, record: dict[str, Any]) -> None:
        client = self._client_instance()
        try:
            self._ensure_collection_sync(client)
            client.upsert(collection_name=self._collection, data=[record])
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"Milvus projection upsert failed: {type(exc).__name__}"
            ) from exc

    def _delete_sync(self, expression: str) -> None:
        client = self._client_instance()
        try:
            if not client.has_collection(self._collection):
                return
            client.delete(collection_name=self._collection, filter=expression)
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"Milvus projection delete failed: {type(exc).__name__}"
            ) from exc

    def _search_sync(
        self, expression: str, vector: list[float], limit: int
    ) -> list[dict]:
        client = self._client_instance()
        try:
            self._ensure_collection_sync(client)
            result = client.search(
                collection_name=self._collection,
                data=[vector],
                anns_field="embedding",
                search_params={
                    "metric_type": self._config.metric_type,
                    "params": {},
                },
                limit=limit,
                filter=expression,
                output_fields=list(OUTPUT_FIELDS),
                consistency_level=self._consistency_level,
            )
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"Milvus projection search failed: {type(exc).__name__}"
            ) from exc
        return self._first_rows(result)

    def _list_keys_sync(self) -> list[dict]:
        client = self._client_instance()
        try:
            self._ensure_collection_sync(client)
            result = client.query(
                collection_name=self._collection,
                filter="",
                output_fields=["pk"],
                limit=_QUERY_LIMIT,
            )
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"Milvus projection query failed: {type(exc).__name__}"
            ) from exc
        if isinstance(result, dict):
            result = result.get("data") or []
        return list(result or [])

    def _ensure_collection_sync(self, client: Any | None = None) -> None:
        client = client or self._client_instance()
        if client.has_collection(self._collection):
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._collection)
            self._inspect_collection(client)
            return
        self._create_collection(client)

    def _inspect_collection(self, client: Any) -> None:
        describe = getattr(client, "describe_collection", None)
        if not callable(describe):
            return
        try:
            description = describe(self._collection)
        except Exception:
            logger.debug("Could not inspect Milvus skill projection schema", exc_info=True)
            return
        fields = description.get("fields", []) if isinstance(description, dict) else []
        for field in fields:
            if not isinstance(field, dict) or field.get("name") != "embedding":
                continue
            params = field.get("params") or {}
            dim = params.get("dim") if isinstance(params, dict) else None
            if dim and int(dim) != self._dimensions:
                raise ProjectionUnavailable(
                    "Milvus skill projection dimension mismatch: "
                    f"expected {self._dimensions}, got {dim}"
                )

    def _create_collection(self, client: Any) -> None:
        try:
            from pymilvus import DataType
        except ImportError as exc:
            raise ProjectionUnavailable(
                "pymilvus is not installed; install the skills extra"
            ) from exc
        try:
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=255)
            schema.add_field("scope", DataType.VARCHAR, max_length=32)
            schema.add_field("owner_user_id", DataType.VARCHAR, max_length=255)
            schema.add_field("skill_id", DataType.VARCHAR, max_length=255)
            schema.add_field("version_id", DataType.VARCHAR, max_length=255)
            schema.add_field("skill_version", DataType.VARCHAR, max_length=255)
            schema.add_field("enabled", DataType.BOOL)
            schema.add_field("name", DataType.VARCHAR, max_length=1024)
            schema.add_field("description", DataType.VARCHAR, max_length=65535)
            schema.add_field("package_sha256", DataType.VARCHAR, max_length=128)
            schema.add_field("skill_md_sha256", DataType.VARCHAR, max_length=128)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self._dimensions)

            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type=self._config.index_type,
                metric_type=self._config.metric_type,
                params={},
            )
            for field_name in _SCALAR_INDEX_FIELDS:
                index_params.add_index(
                    field_name=field_name,
                    index_type="INVERTED",
                )
            client.create_collection(
                collection_name=self._collection,
                schema=schema,
                index_params=index_params,
                consistency_level=self._consistency_level,
            )
            load = getattr(client, "load_collection", None)
            if callable(load):
                load(self._collection)
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"Milvus skill projection creation failed: {type(exc).__name__}"
            ) from exc

    # ------------------------------------------------------------------
    # records, filters, and hits
    # ------------------------------------------------------------------

    def _record(self, row: ProjectionRow) -> dict[str, Any]:
        if row.scope not in _SCOPES:
            raise ValueError(f"unsupported projection scope: {row.scope!r}")
        if not row.projection_key or not row.skill_id or not row.version_id:
            raise ValueError("projection key, skill id, and version id must not be blank")
        values = list(row.embedding)
        if len(values) != self._dimensions:
            raise ValueError(
                f"embedding dimension mismatch: expected {self._dimensions}, "
                f"got {len(values)}"
            )
        return {
            "pk": row.projection_key,
            "scope": row.scope,
            "owner_user_id": row.owner_user_id or "",
            "skill_id": row.skill_id,
            "version_id": row.version_id,
            "skill_version": row.skill_version,
            "enabled": bool(row.enabled),
            "name": row.name,
            "description": row.description,
            "package_sha256": row.package_sha256,
            "skill_md_sha256": row.skill_md_sha256,
            "embedding": values,
        }

    @staticmethod
    def _filter(user_id: str, private_skill_ids: Sequence[str]) -> str:
        public = 'scope == "public" and enabled == true'
        ids = list(private_skill_ids)
        if not ids:
            return public
        public += f" and skill_id not in {json.dumps(ids)}"
        private = f'scope == "private" and owner_user_id == {json.dumps(user_id)}'
        return f"({public}) or ({private})"

    @staticmethod
    def _first_rows(result: Any) -> list[dict]:
        if isinstance(result, dict):
            result = result.get("data") or []
        if not result:
            return []
        first = result[0]
        return list(first) if isinstance(first, list) else list(result)

    @staticmethod
    def _to_hits(rows: list[dict]) -> list[ProjectionHit]:
        hits: list[ProjectionHit] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            entity = row.get("entity")
            if not isinstance(entity, dict):
                entity = row
            projection_key = str(
                entity.get("pk") or entity.get("projection_key") or row.get("id") or ""
            )
            if not projection_key:
                continue
            owner_user_id = str(entity.get("owner_user_id") or "") or None
            distance = row.get("distance", row.get("score", 0.0))
            try:
                score = float(distance) if distance is not None else 0.0
            except (TypeError, ValueError):
                score = 0.0
            hits.append(
                ProjectionHit(
                    projection_key=projection_key,
                    scope=str(entity.get("scope") or ""),
                    owner_user_id=owner_user_id,
                    skill_id=str(entity.get("skill_id") or ""),
                    version_id=str(entity.get("version_id") or ""),
                    score=score,
                )
            )
        return hits
