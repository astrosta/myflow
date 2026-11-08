"""The active Milvus projection for user-private skills."""

from __future__ import annotations

import builtins
import json

import pytest
from agentkit.knowledge.config import MilvusConfig
from agentkit.skills.projection import (
    MilvusSkillProjection,
    ProjectionHit,
    ProjectionRow,
    ProjectionUnavailable,
)


METADATA_FIELDS = {
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
    "content",
}


class MissingIndexError(Exception):
    """Milvus 700: the collection cannot be loaded while it has no index."""

    code = 700


class FakeMilvus:
    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.deleted: list[dict] = []
        self.searches: list[dict] = []
        self.hybrid_searches: list[dict] = []
        self.queries: list[dict] = []
        self.flushes = 0
        self.closed = False
        self.extra_fields: list[dict] = []
        #: 默认是一张健康集合：dense + BM25 索引都在，load 会成功。
        self.index_names: list[str] = ["embedding", "sparse"]
        self.keys: list[str] = ["private:u1:s1"]
        self.iterator_calls: list[dict] = []
        self.iterator: "FakeIterator | None" = None
        self.created_indexes: list[dict] = []
        self.released: list[str] = []
        self.loads: list[str] = []
        self.dropped: list[str] = []

    def has_collection(self, name: str) -> bool:
        return True

    def load_collection(self, name: str) -> None:
        self.loads.append(name)
        if "embedding" not in self.index_names:
            raise MissingIndexError(
                "index not found[collection=myflow_skills_active]"
            )

    def list_indexes(self, name: str, field_name: str = "") -> list[str]:
        return list(self.index_names)

    def query_iterator(
        self,
        collection_name: str,
        batch_size: int = 1000,
        limit: int = -1,
        filter: str = "",
        output_fields=None,
    ):
        self.iterator_calls.append({
            "collection_name": collection_name,
            "batch_size": batch_size,
            "filter": filter,
            "output_fields": list(output_fields or []),
        })
        self.iterator = FakeIterator(self.keys, batch_size)
        return self.iterator

    def prepare_index_params(self) -> "FakeIndexParams":
        return FakeIndexParams()

    def create_index(self, collection_name: str, index_params) -> None:
        self.created_indexes.append({
            "collection_name": collection_name,
            "indexes": list(index_params.indexes),
        })
        for item in index_params.indexes:
            if item["field_name"] not in self.index_names:
                self.index_names.append(item["field_name"])

    def release_collection(self, name: str) -> None:
        self.released.append(name)

    def drop_collection(self, name: str) -> None:
        self.dropped.append(name)

    def upsert(self, collection_name: str, data: list[dict]) -> dict:
        self.upserts.append({"collection_name": collection_name, "data": data})
        return {"upsert_count": len(data)}

    def delete(self, collection_name: str, filter: str) -> dict:
        self.deleted.append({"collection_name": collection_name, "filter": filter})
        return {"delete_count": 1}

    def flush(self, collection_name: str) -> None:
        self.flushes += 1

    def _row(self, *, distance: float = 0.87) -> dict:
        return {
            "id": "private:u1:s1",
            "distance": distance,
            "entity": {
                "pk": "private:u1:s1",
                "scope": "private",
                "owner_user_id": "u1",
                "skill_id": "s1",
                "version_id": "v1",
                "skill_version": "2.0.0",
                "enabled": True,
                "name": "private skill",
                "description": "private description",
                "package_sha256": "p1",
                "skill_md_sha256": "m1",
                "content": "private skill\nprivate description",
            },
        }

    def search(self, collection_name: str, **kwargs):
        self.searches.append({"collection_name": collection_name, **kwargs})
        return [[self._row()]]

    def hybrid_search(self, collection_name: str, **kwargs):
        self.hybrid_searches.append({"collection_name": collection_name, **kwargs})
        return [[self._row(distance=0.032)]]

    def query(self, collection_name: str, **kwargs):
        self.queries.append({"collection_name": collection_name, **kwargs})
        return [{"pk": "private:u1:s1"}]

    def describe_collection(self, name: str) -> dict:
        return {
            "fields": [
                {"name": "embedding", "params": {"dim": "4"}},
                {"name": "content"},
                {"name": "sparse"},
                {"name": "scope"},
                {"name": "owner_user_id"},
                {"name": "enabled"},
                *self.extra_fields,
            ]
        }

    def close(self) -> None:
        self.closed = True


class FakeIterator:
    """Stands in for pymilvus' QueryIterator: next() per page, close() at the end."""

    def __init__(self, keys: list[str], batch_size: int) -> None:
        self._keys = list(keys)
        self._batch = max(1, int(batch_size))
        self.closed = False

    def next(self) -> list[dict]:
        page, self._keys = self._keys[: self._batch], self._keys[self._batch :]
        return [{"pk": key} for key in page]

    def close(self) -> None:
        self.closed = True


class FakeSchema:
    def __init__(self) -> None:
        self.fields: dict[str, dict] = {}
        self.functions: list[dict] = []

    def add_field(self, name: str, dtype, **kwargs) -> None:
        self.fields[name] = {"dtype": dtype, **kwargs}

    def add_function(self, function) -> None:
        self.functions.append(
            {
                "name": getattr(function, "name", ""),
                "input_field_names": list(getattr(function, "input_field_names", [])),
                "output_field_names": list(getattr(function, "output_field_names", [])),
                "function_type": getattr(function, "type", None),
            }
        )


class FakeIndexParams:
    def __init__(self) -> None:
        self.indexes: list[dict] = []

    def add_index(self, **kwargs) -> None:
        self.indexes.append(kwargs)


class FakeEmptyMilvus(FakeMilvus):
    def __init__(self) -> None:
        super().__init__()
        self.index_names = []
        self.exists = False
        self.schema = FakeSchema()
        self.index_params = FakeIndexParams()
        self.created: list[dict] = []

    def has_collection(self, name: str) -> bool:
        return self.exists

    def create_schema(self, **kwargs) -> FakeSchema:
        return self.schema

    def prepare_index_params(self) -> FakeIndexParams:
        return self.index_params

    def create_collection(self, **kwargs) -> None:
        self.created.append(kwargs)
        self.exists = True


class BrokenIndexMilvus(FakeEmptyMilvus):
    """建集合成功但建索引失败：最容易留下"没有索引的幽灵集合"的路径。"""

    def create_index(self, collection_name: str, index_params) -> None:
        raise RuntimeError("index build failed")


class FlakyLoadMilvus(FakeMilvus):
    """自检说有索引，但 load 仍然报 700（索引刚建好还不可见 / 被手工动过）。"""

    def __init__(self) -> None:
        super().__init__()
        self.failures_left = 1

    def load_collection(self, name: str) -> None:
        self.loads.append(name)
        if self.failures_left > 0:
            self.failures_left -= 1
            raise MissingIndexError(
                "index not found[collection=myflow_skills_active]"
            )


def _config() -> MilvusConfig:
    # Collection is deliberately unset: the projection owns its active default.
    return MilvusConfig(uri="http://milvus:19530", consistency_level="Strong")


def _store(fake: FakeMilvus, *, dimensions: int = 4) -> MilvusSkillProjection:
    return MilvusSkillProjection(_config(), dimensions=dimensions, client=fake)


def _private_row() -> ProjectionRow:
    return ProjectionRow(
        projection_key="private:u1:s1",
        scope="private",
        owner_user_id="u1",
        skill_id="s1",
        version_id="v1",
        skill_version="2.0.0",
        enabled=True,
        name="private skill",
        description="private description",
        package_sha256="p1",
        skill_md_sha256="m1",
        embedding=[0.0, 1.0, 0.0, 0.0],
        content="private skill\nprivate description\nbody",
    )


@pytest.mark.asyncio
async def test_upsert_uses_projection_key_as_pk_and_keeps_projection_metadata() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    await store.upsert_active(_private_row())

    private = fake.upserts[0]["data"][0]
    assert fake.upserts[0]["collection_name"] == "myflow_skills_active"
    assert private["pk"] == "private:u1:s1"
    assert private["owner_user_id"] == "u1"
    assert private["scope"] == "private"
    assert private["version_id"] == "v1"
    assert set(private) == METADATA_FIELDS | {"embedding"}
    assert "body" not in private
    await store.aclose()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_delete_active_filters_by_the_projection_primary_key() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    await store.delete_active("private:u1:s1")

    assert fake.deleted == [{
        "collection_name": "myflow_skills_active",
        "filter": 'pk == "private:u1:s1"',
    }]
    await store.aclose()


@pytest.mark.asyncio
async def test_search_filters_by_the_caller_owner_only() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    hits = await store.search(
        user_id="u1",
        vector=[1.0, 0.0, 0.0, 0.0],
        limit=5,
    )

    assert len(fake.searches) == 1
    call = fake.searches[0]
    assert call["collection_name"] == "myflow_skills_active"
    assert call["filter"] == (
        'scope == "private" and enabled == true and owner_user_id == "u1"'
    )
    assert "skill_id not in" not in call["filter"]
    assert "version_id" not in call["filter"]
    assert call["anns_field"] == "embedding"
    assert call["data"] == [[1.0, 0.0, 0.0, 0.0]]
    assert call["limit"] == 5
    assert call["search_params"]["params"] == {}
    assert call["consistency_level"] == "Strong"
    assert set(call["output_fields"]) == METADATA_FIELDS - {"content"}
    assert hits == [
        ProjectionHit(
            projection_key="private:u1:s1",
            scope="private",
            owner_user_id="u1",
            skill_id="s1",
            version_id="v1",
            score=0.87,
        ),
    ]
    await store.aclose()


@pytest.mark.asyncio
async def test_hybrid_search_uses_milvus_rrf_with_the_owner_filter_on_both_channels() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    hits = await store.hybrid_search(
        user_id="u1",
        vector=[1.0, 0.0, 0.0, 0.0],
        keyword_query="pgvector 多租户",
        limit=5,
        rrf_k=37,
    )

    assert hits[0].score == 0.032
    call = fake.hybrid_searches[0]
    assert "filter" not in call
    dense, sparse = call["reqs"]
    expected = 'scope == "private" and enabled == true and owner_user_id == "u1"'
    assert dense.expr == expected
    assert sparse.expr == expected
    assert dense.anns_field == "embedding"
    assert sparse.anns_field == "sparse"
    assert sparse.data == ["pgvector 多租户"]
    assert sparse.param["metric_type"] == "BM25"
    assert call["ranker"].dict() == {"strategy": "rrf", "params": {"k": 37}}
    await store.aclose()


@pytest.mark.asyncio
async def test_keyword_search_uses_milvus_bm25_on_the_sparse_field() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    hits = await store.keyword_search(user_id="u1", query="pgvector", limit=3)

    assert hits[0].score == 0.87
    call = fake.searches[0]
    assert call["anns_field"] == "sparse"
    assert call["data"] == ["pgvector"]
    assert call["search_params"]["metric_type"] == "BM25"
    await store.aclose()


@pytest.mark.asyncio
async def test_an_old_collection_without_a_sparse_field_is_rejected() -> None:
    fake = FakeMilvus()
    original = fake.describe_collection

    def describe_without_sparse(name: str) -> dict:
        description = original(name)
        description["fields"] = [
            field for field in description["fields"] if field["name"] != "sparse"
        ]
        return description

    fake.describe_collection = describe_without_sparse  # type: ignore[method-assign]
    store = _store(fake)

    with pytest.raises(ProjectionUnavailable, match="BM25 sparse"):
        await store.ensure_collection(4)
    await store.aclose()


@pytest.mark.asyncio
async def test_search_json_escapes_the_owner_id_in_the_filter() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    await store.search(user_id='u"1', vector=[1.0, 0.0, 0.0, 0.0], limit=3)

    owner = json.dumps('u"1')
    assert fake.searches[0]["filter"] == (
        f'scope == "private" and enabled == true and owner_user_id == {owner}'
    )
    await store.aclose()


@pytest.mark.asyncio
async def test_collection_creation_adds_scalar_indexes_for_projection_filters() -> None:
    pytest.importorskip("pymilvus")
    from pymilvus import DataType, FunctionType

    fake = FakeEmptyMilvus()
    store = _store(fake)

    await store.ensure_collection(4)

    assert set(fake.schema.fields) == METADATA_FIELDS | {"embedding", "sparse"}
    assert fake.schema.fields["pk"]["is_primary"] is True
    assert fake.schema.fields["pk"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["scope"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["owner_user_id"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["version_id"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["enabled"]["dtype"] == DataType.BOOL
    assert fake.schema.fields["content"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["content"]["enable_analyzer"] is True
    assert fake.schema.fields["sparse"]["dtype"] == DataType.SPARSE_FLOAT_VECTOR
    assert fake.schema.fields["embedding"]["dtype"] == DataType.FLOAT_VECTOR
    assert fake.schema.fields["embedding"]["dim"] == 4
    assert any(
        item.get("function_type") == FunctionType.BM25
        for item in fake.schema.functions
    )

    indexes = {item["field_name"]: item for item in fake.index_params.indexes}
    for field in ("scope", "owner_user_id", "enabled"):
        assert indexes[field]["index_type"] == "INVERTED"
    assert indexes["embedding"]["metric_type"] == "COSINE"
    assert indexes["embedding"]["index_type"] == "AUTOINDEX"
    assert indexes["embedding"]["params"] == {}
    assert indexes["sparse"]["index_type"] == "SPARSE_INVERTED_INDEX"
    assert indexes["sparse"]["metric_type"] == "BM25"
    assert indexes["sparse"]["params"] == {"bm25_k1": 1.2, "bm25_b": 0.75}
    assert fake.created[0]["collection_name"] == "myflow_skills_active"
    assert fake.created[0]["consistency_level"] == "Strong"
    # 索引显式创建，不塞进 create_collection：那一步失败会留下无法 load 的集合。
    assert "index_params" not in fake.created[0]
    assert [item["indexes"] for item in fake.created_indexes] == [fake.index_params.indexes]
    assert fake.loads == ["myflow_skills_active"]
    await store.aclose()


@pytest.mark.asyncio
async def test_an_existing_collection_without_an_index_gets_one_before_loading() -> None:
    """复现事故：集合存在但没有向量索引，load 报 700；必须补建索引而不是反复 load。"""
    pytest.importorskip("pymilvus")
    fake = FakeMilvus()
    fake.index_names = []  # 只有集合，没有索引
    store = _store(fake)

    await store.upsert_active(_private_row())

    assert len(fake.created_indexes) == 1
    created = {item["field_name"]: item for item in fake.created_indexes[0]["indexes"]}
    assert created["embedding"]["index_type"] == "AUTOINDEX"
    assert created["sparse"]["metric_type"] == "BM25"
    for field in ("scope", "owner_user_id", "enabled"):
        assert created[field]["index_type"] == "INVERTED"
    assert fake.released == ["myflow_skills_active"]  # 已 load 的集合不能建索引
    assert fake.loads == ["myflow_skills_active"]  # 索引补好之后才 load
    assert fake.upserts[0]["data"][0]["pk"] == "private:u1:s1"
    assert fake.dropped == []
    await store.aclose()


@pytest.mark.asyncio
async def test_a_load_that_reports_a_missing_index_repairs_it_and_retries_once() -> None:
    pytest.importorskip("pymilvus")
    fake = FlakyLoadMilvus()
    store = _store(fake)

    await store.search(user_id="u1", vector=[1.0, 0.0, 0.0, 0.0], limit=5)

    assert len(fake.created_indexes) == 1
    assert fake.loads == ["myflow_skills_active", "myflow_skills_active"]
    assert len(fake.searches) == 1
    await store.aclose()


@pytest.mark.asyncio
async def test_a_healthy_collection_is_loaded_without_touching_its_indexes() -> None:
    pytest.importorskip("pymilvus")
    fake = FakeMilvus()
    store = _store(fake)

    await store.ensure_collection(4)
    await store.ensure_collection(4)

    assert fake.created_indexes == []
    assert fake.released == []
    assert fake.loads == ["myflow_skills_active", "myflow_skills_active"]
    await store.aclose()


class NeverIndexedMilvus(FakeMilvus):
    """索引怎么都建不成功：自愈只重试一次，不能变成无限循环。"""

    def __init__(self) -> None:
        super().__init__()
        self.index_names = []

    def create_index(self, collection_name: str, index_params) -> None:
        self.created_indexes.append({
            "collection_name": collection_name,
            "indexes": list(index_params.indexes),
        })
        # 故意不把 embedding 记进 index_names：load 依旧报 700。


@pytest.mark.asyncio
async def test_a_missing_index_is_retried_once_and_then_fails_cleanly() -> None:
    pytest.importorskip("pymilvus")
    fake = NeverIndexedMilvus()
    store = _store(fake)

    with pytest.raises(ProjectionUnavailable, match="load failed"):
        await store.upsert_active(_private_row())

    assert len(fake.loads) == 2  # 原先 + 补索引后的一次重试
    assert len(fake.created_indexes) == 2  # 自检补一次，load 失败后再补一次
    assert fake.upserts == []  # 没有假装写成功
    assert fake.dropped == []  # 集合不是这次调用建的，不自作主张删
    await store.aclose()

@pytest.mark.asyncio
async def test_a_half_created_collection_is_dropped_when_the_index_fails() -> None:
    """建集合成功、建索引失败：不能把"没有索引的集合"留给下一次重试。"""
    pytest.importorskip("pymilvus")
    fake = BrokenIndexMilvus()
    store = _store(fake)

    with pytest.raises(ProjectionUnavailable, match="index creation failed"):
        await store.ensure_collection(4)

    assert fake.dropped == ["myflow_skills_active"]
    await store.aclose()


@pytest.mark.asyncio
async def test_scan_projection_keys_streams_every_primary_key() -> None:
    fake = FakeMilvus()
    fake.keys = [f"private:u1:skill-{index}" for index in range(25)]
    store = _store(fake)

    keys, truncated = await store.scan_projection_keys(limit=100)

    assert keys == fake.keys
    assert truncated is False
    assert fake.iterator_calls == [{
        "collection_name": "myflow_skills_active",
        "batch_size": 100,
        "filter": "",
        "output_fields": ["pk"],
    }]
    assert fake.iterator is not None and fake.iterator.closed is True
    await store.aclose()


@pytest.mark.asyncio
async def test_scan_projection_keys_reports_a_truncated_scan() -> None:
    fake = FakeMilvus()
    fake.keys = [f"private:u1:skill-{index}" for index in range(25)]
    store = _store(fake)

    keys, truncated = await store.scan_projection_keys(limit=10)

    # 一批一批读，不靠单次 query 的 16384 上限；到了预算就停并如实报告。
    assert len(keys) == 10
    assert truncated is True
    await store.aclose()


@pytest.mark.asyncio
async def test_missing_pymilvus_raises_a_clear_error(monkeypatch) -> None:
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pymilvus":
            raise ImportError("simulated missing dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    store = MilvusSkillProjection(_config(), dimensions=4)

    with pytest.raises(ProjectionUnavailable, match="pymilvus"):
        await store.ensure_collection(4)
