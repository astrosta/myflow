"""Task 10: the active Milvus projection for public and private skills."""

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
}


class FakeMilvus:
    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.deleted: list[dict] = []
        self.searches: list[dict] = []
        self.queries: list[dict] = []
        self.flushes = 0
        self.closed = False
        self.extra_fields: list[dict] = []

    def has_collection(self, name: str) -> bool:
        return True

    def load_collection(self, name: str) -> None:
        return None

    def upsert(self, collection_name: str, data: list[dict]) -> dict:
        self.upserts.append({"collection_name": collection_name, "data": data})
        return {"upsert_count": len(data)}

    def delete(self, collection_name: str, filter: str) -> dict:
        self.deleted.append({"collection_name": collection_name, "filter": filter})
        return {"delete_count": 1}

    def flush(self, collection_name: str) -> None:
        self.flushes += 1

    def search(self, collection_name: str, **kwargs):
        self.searches.append({"collection_name": collection_name, **kwargs})
        return [[
            {
                "id": "public:s3",
                "distance": 0.91,
                "entity": {
                    "pk": "public:s3",
                    "scope": "public",
                    "owner_user_id": "",
                    "skill_id": "s3",
                    "version_id": "v3",
                    "skill_version": "1.0.0",
                    "enabled": True,
                    "name": "public skill",
                    "description": "public description",
                    "package_sha256": "p3",
                    "skill_md_sha256": "m3",
                },
            },
            {
                "id": "private:u1:s1",
                "distance": 0.87,
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
                },
            },
        ]]

    def query(self, collection_name: str, **kwargs):
        self.queries.append({"collection_name": collection_name, **kwargs})
        return [{"pk": "public:s3"}, {"pk": "private:u1:s1"}]

    def describe_collection(self, name: str) -> dict:
        return {
            "fields": [
                {"name": "embedding", "params": {"dim": "4"}},
                {"name": "scope"},
                {"name": "owner_user_id"},
                {"name": "enabled"},
                *self.extra_fields,
            ]
        }

    def close(self) -> None:
        self.closed = True


class FakeSchema:
    def __init__(self) -> None:
        self.fields: dict[str, dict] = {}

    def add_field(self, name: str, dtype, **kwargs) -> None:
        self.fields[name] = {"dtype": dtype, **kwargs}


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


def _config() -> MilvusConfig:
    # Collection is deliberately unset: the projection owns its active default.
    return MilvusConfig(uri="http://milvus:19530", consistency_level="Strong")


def _store(fake: FakeMilvus, *, dimensions: int = 4) -> MilvusSkillProjection:
    return MilvusSkillProjection(_config(), dimensions=dimensions, client=fake)


def _public_row() -> ProjectionRow:
    return ProjectionRow(
        projection_key="public:s3",
        scope="public",
        owner_user_id=None,
        skill_id="s3",
        version_id="v3",
        skill_version="1.0.0",
        enabled=True,
        name="public skill",
        description="public description",
        package_sha256="p3",
        skill_md_sha256="m3",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )


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
    )


@pytest.mark.asyncio
async def test_upsert_uses_projection_key_as_pk_and_keeps_projection_metadata() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    await store.upsert_active(_public_row())
    await store.upsert_active(_private_row())

    public = fake.upserts[0]["data"][0]
    private = fake.upserts[1]["data"][0]
    assert fake.upserts[0]["collection_name"] == "myflow_skills_active"
    assert public == {
        "pk": "public:s3",
        "scope": "public",
        "owner_user_id": "",
        "skill_id": "s3",
        "version_id": "v3",
        "skill_version": "1.0.0",
        "enabled": True,
        "name": "public skill",
        "description": "public description",
        "package_sha256": "p3",
        "skill_md_sha256": "m3",
        "embedding": [1.0, 0.0, 0.0, 0.0],
    }
    assert private["pk"] == "private:u1:s1"
    assert private["owner_user_id"] == "u1"
    assert private["scope"] == "private"
    assert private["version_id"] == "v1"
    assert set(public) == METADATA_FIELDS | {"embedding"}
    assert "body" not in public
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
async def test_search_sends_one_or_query_with_public_exclusion_and_private_owner() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    hits = await store.search(
        user_id="u1",
        private_skill_ids=["s1", "s2"],
        vector=[1.0, 0.0, 0.0, 0.0],
        limit=5,
    )

    assert len(fake.searches) == 1
    call = fake.searches[0]
    assert call["collection_name"] == "myflow_skills_active"
    assert call["filter"] == (
        '(scope == "public" and enabled == true and skill_id not in ["s1", "s2"]) '
        'or (scope == "private" and owner_user_id == "u1")'
    )
    assert "version_id" not in call["filter"]
    assert call["anns_field"] == "embedding"
    assert call["data"] == [[1.0, 0.0, 0.0, 0.0]]
    assert call["limit"] == 5
    assert call["consistency_level"] == "Strong"
    assert set(call["output_fields"]) == METADATA_FIELDS
    assert hits == [
        ProjectionHit(
            projection_key="public:s3",
            scope="public",
            owner_user_id=None,
            skill_id="s3",
            version_id="v3",
            score=0.91,
        ),
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
async def test_search_without_private_ids_uses_only_the_public_branch() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    await store.search(
        user_id="u1", private_skill_ids=[], vector=[1.0, 0.0, 0.0, 0.0], limit=3
    )

    assert len(fake.searches) == 1
    assert fake.searches[0]["filter"] == 'scope == "public" and enabled == true'
    await store.aclose()


@pytest.mark.asyncio
async def test_search_json_escapes_ids_in_the_filter() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    await store.search(
        user_id='u"1',
        private_skill_ids=['s"1'],
        vector=[1.0, 0.0, 0.0, 0.0],
        limit=3,
    )

    public = f'scope == "public" and enabled == true and skill_id not in {json.dumps(['s"1'])}'
    owner = json.dumps('u"1')
    assert fake.searches[0]["filter"] == (
        f'({public}) or (scope == "private" and owner_user_id == {owner})'
    )
    await store.aclose()


@pytest.mark.asyncio
async def test_collection_creation_adds_scalar_indexes_for_projection_filters() -> None:
    pytest.importorskip("pymilvus")
    from pymilvus import DataType

    fake = FakeEmptyMilvus()
    store = _store(fake)

    await store.ensure_collection(4)

    assert set(fake.schema.fields) == METADATA_FIELDS | {"embedding"}
    assert fake.schema.fields["pk"]["is_primary"] is True
    assert fake.schema.fields["pk"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["scope"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["owner_user_id"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["version_id"]["dtype"] == DataType.VARCHAR
    assert fake.schema.fields["enabled"]["dtype"] == DataType.BOOL
    assert fake.schema.fields["embedding"]["dtype"] == DataType.FLOAT_VECTOR
    assert fake.schema.fields["embedding"]["dim"] == 4
    assert "body" not in fake.schema.fields

    indexes = {item["field_name"]: item for item in fake.index_params.indexes}
    for field in ("scope", "owner_user_id", "enabled"):
        assert indexes[field]["index_type"] == "INVERTED"
    assert indexes["embedding"]["metric_type"] == "COSINE"
    assert indexes["embedding"]["index_type"] == "AUTOINDEX"
    assert fake.created[0]["collection_name"] == "myflow_skills_active"
    assert fake.created[0]["consistency_level"] == "Strong"
    await store.aclose()


@pytest.mark.asyncio
async def test_list_projection_keys_reads_only_the_primary_key() -> None:
    fake = FakeMilvus()
    store = _store(fake)

    keys = await store.list_projection_keys()

    assert keys == ["public:s3", "private:u1:s1"]
    assert fake.queries == [{
        "collection_name": "myflow_skills_active",
        "filter": "",
        "output_fields": ["pk"],
        "limit": 16384,
    }]
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
