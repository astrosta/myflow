"""contextforge_tools: authoritative tool catalog table, migration, and store.

The table is the runtime source of truth for ContextForge tools; Milvus is a
derived index on top of it.  These tests pin the column set, the uniqueness and
lookup indexes the sync/search/execute paths depend on, the verbatim schema and
hash bookkeeping, and the full-snapshot upsert semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (JSON,
    Boolean,
    DateTime,
    String,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import agentkit.persistence.schema as schema_module
from agentkit.contextforge.store import (
    CALL_NAME_PREFIX,
    ContextForgeSnapshotStats,
    ContextForgeToolRecord,
    ContextForgeToolRow,
    ContextForgeToolStore,
    call_name_for,
    content_hash_for,
    schema_hash_for,
)
from agentkit.persistence.bootstrap import _head_revision
from agentkit.persistence.migrations import __file__ as migrations_file
from agentkit.persistence.schema import Base

MIGRATIONS_DIR = Path(migrations_file).resolve().parent

TABLE = "contextforge_tools"

#: Spec §4.1, in declaration order.  created_at/updated_at trail last_seen_at.
EXPECTED_COLUMNS = (
    "id",
    "name",
    "call_name",
    "display_name",
    "description",
    "input_schema",
    "annotations",
    "tags",
    "parameters",
    "owner_user_id",
    "enabled",
    "reachable",
    "callable",
    "content",
    "content_hash",
    "schema_hash",
    "last_seen_at",
    "created_at",
    "updated_at",
)

#: A schema with every shape a Pydantic round-trip would rewrite.
VERBATIM_SCHEMA = {
    "$defs": {
        "Repo": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "owner": {"type": "string"}},
            "required": ["name"],
        }
    },
    "type": "object",
    "properties": {
        "query": {"type": "string", "default": ""},
        "repo": {"$ref": "#/$defs/Repo"},
        "filters": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
    },
    "additionalProperties": False,
}

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
T = TypeVar("T")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _record(name: str = "github_search", **overrides: object) -> ContextForgeToolRecord:
    """A complete, callable record owned by user-1."""
    values: dict[str, object] = {
        "id": "tool-" + name,
        "name": name,
        "call_name": call_name_for(name),
        "display_name": name.replace("_", " ").title(),
        "description": name + " tool",
        "input_schema": VERBATIM_SCHEMA,
        "annotations": {"myflow_owner": "user-1", "readOnlyHint": True},
        "tags": ("github", "code"),
        "parameters": ("query", "repo"),
        "owner_user_id": "user-1",
        "enabled": True,
        "reachable": True,
        "callable": True,
        "content": name + "\n" + name + " tool",
    }
    values.update(overrides)
    return ContextForgeToolRecord(**values)  # type: ignore[arg-type]


def _row_values(name: str = "github_search", **overrides: object) -> dict[str, object]:
    """Raw column values, for core inserts that bypass the store."""
    values: dict[str, object] = {
        "id": "tool-" + name,
        "name": name,
        "call_name": call_name_for(name),
        "display_name": None,
        "description": "",
        "input_schema": {},
        "annotations": {},
        "tags": [],
        "parameters": [],
        "owner_user_id": "user-1",
        "enabled": True,
        "reachable": True,
        "callable": True,
        "content": "",
        "content_hash": "",
        "schema_hash": "",
        "last_seen_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


@pytest.fixture(name="engine")
def engine_fixture():
    """Synchronous DDL surface, for inspector-level assertions."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


class Harness:
    def __init__(
        self,
        store: ContextForgeToolStore,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.store = store
        self.session_factory = session_factory

    def run(self, scenario: Callable[[], Awaitable[T]]) -> T:
        return asyncio.run(scenario())


@pytest.fixture(name="harness")
def harness_fixture(tmp_path: Path):
    async def _build() -> tuple[Harness, object]:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "contextforge.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return Harness(ContextForgeToolStore(factory), factory), engine

    built, engine = asyncio.run(_build())
    try:
        yield built
    finally:
        asyncio.run(engine.dispose())


# --------------------------------------------------------------------------- #
# Table shape
# --------------------------------------------------------------------------- #


def test_table_has_exactly_the_documented_columns() -> None:
    table = Base.metadata.tables[TABLE]

    assert tuple(table.columns.keys()) == EXPECTED_COLUMNS


def test_column_types_and_nullability_match_the_spec() -> None:
    table = Base.metadata.tables[TABLE]
    columns = table.columns

    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert isinstance(columns["id"].type, String)
    assert columns["id"].type.length == 64
    assert isinstance(columns["name"].type, String)
    assert isinstance(columns["call_name"].type, String)
    assert isinstance(columns["description"].type, Text)
    assert isinstance(columns["content"].type, Text)
    assert isinstance(columns["input_schema"].type, JSON)
    assert isinstance(columns["annotations"].type, JSON)
    assert isinstance(columns["tags"].type, JSON)
    assert isinstance(columns["parameters"].type, JSON)
    assert isinstance(columns["enabled"].type, Boolean)
    assert isinstance(columns["reachable"].type, Boolean)
    assert isinstance(columns["callable"].type, Boolean)
    assert isinstance(columns["last_seen_at"].type, DateTime)
    assert columns["last_seen_at"].type.timezone is True
    assert isinstance(columns["created_at"].type, DateTime)
    assert isinstance(columns["updated_at"].type, DateTime)
    assert columns["content_hash"].type.length == 64
    assert columns["schema_hash"].type.length == 64

    for name in (
        "id",
        "name",
        "call_name",
        "description",
        "input_schema",
        "annotations",
        "tags",
        "parameters",
        "enabled",
        "reachable",
        "callable",
        "content",
        "content_hash",
        "schema_hash",
        "last_seen_at",
        "created_at",
        "updated_at",
    ):
        assert columns[name].nullable is False, name
    # Optional by design: ContextForge tools without an owner stay non-callable.
    assert columns["display_name"].nullable is True
    assert columns["owner_user_id"].nullable is True


def test_unique_constraints_and_lookup_indexes_exist(engine) -> None:
    inspector = inspect(engine)
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(TABLE)
    }
    index_names = {index["name"] for index in inspector.get_indexes(TABLE)}
    indexed_columns = {
        tuple(index["column_names"]) for index in inspector.get_indexes(TABLE)
    }

    assert ("name",) in unique_columns
    assert ("call_name",) in unique_columns
    assert ("owner_user_id", "callable") in indexed_columns
    assert ("callable",) in indexed_columns
    assert ("last_seen_at",) in indexed_columns
    assert "ix_contextforge_tools_owner_callable" in index_names
    assert "ix_contextforge_tools_callable" in index_names
    assert "ix_contextforge_tools_last_seen_at" in index_names


def test_model_is_registered_in_the_persistence_schema() -> None:
    assert TABLE in Base.metadata.tables
    assert schema_module.ContextForgeToolRow is ContextForgeToolRow
    assert "ContextForgeToolRow" in schema_module.__all__


def test_duplicate_name_and_call_name_are_rejected(engine) -> None:
    table = Base.metadata.tables[TABLE]
    with engine.begin() as conn:
        conn.execute(table.insert().values(**_row_values("github_search")))

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(**_row_values("github_search", id="tool-other"))
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **_row_values("github_issue", id="tool-third", call_name="cf__github_search")
                )
            )


# --------------------------------------------------------------------------- #
# Hash and naming contract
# --------------------------------------------------------------------------- #


def test_call_name_uses_the_cf_prefix() -> None:
    assert CALL_NAME_PREFIX == "cf__"
    assert call_name_for("github_search") == "cf__github_search"


def test_content_hash_covers_content_owner_and_call_name() -> None:
    expected = hashlib.sha256(
        ("github_search tool" + "user-1" + "cf__github_search").encode("utf-8")
    ).hexdigest()

    assert content_hash_for("github_search tool", "user-1", "cf__github_search") == expected
    # owner and call_name participate, so a move between users re-indexes.
    assert content_hash_for("github_search tool", "user-2", "cf__github_search") != expected
    assert content_hash_for("github_search tool", "user-1", "cf__other") != expected
    # an unowned tool hashes the same as an empty owner, never "None".
    assert content_hash_for("x", None, "cf__x") == content_hash_for("x", "", "cf__x")


def test_schema_hash_is_canonical_across_key_order() -> None:
    one = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
    two = {"properties": {"b": {"type": "integer"}, "a": {"type": "string"}}, "type": "object"}

    assert schema_hash_for(one) == schema_hash_for(two)
    assert schema_hash_for(one) == hashlib.sha256(
        json.dumps(one, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert schema_hash_for(one) != schema_hash_for({"type": "object"})


# --------------------------------------------------------------------------- #
# upsert_snapshot
# --------------------------------------------------------------------------- #


def test_upsert_snapshot_stores_verbatim_schema_and_hashes(harness: Harness) -> None:
    async def scenario() -> ContextForgeToolRecord:
        await harness.store.upsert_snapshot([_record()], ["github_search"])
        found = await harness.store.get_by_call_name("cf__github_search", "user-1")
        assert found is not None
        return found

    found = harness.run(scenario)

    assert found.id == "tool-github_search"
    assert found.name == "github_search"
    assert found.call_name == "cf__github_search"
    assert found.callable is True
    # $ref / anyOf / default survive untouched: the schema is executed verbatim.
    assert found.input_schema == VERBATIM_SCHEMA
    assert found.annotations == {"myflow_owner": "user-1", "readOnlyHint": True}
    assert found.tags == ("github", "code")
    assert found.parameters == ("query", "repo")
    assert found.content_hash == content_hash_for(
        "github_search\ngithub_search tool", "user-1", "cf__github_search"
    )
    assert found.schema_hash == schema_hash_for(VERBATIM_SCHEMA)
    assert found.last_seen_at is not None
    assert _utc(found.last_seen_at).tzinfo is not None


async def _row_timestamps(harness: Harness) -> tuple[datetime, datetime]:
    """The store's record has no timestamps; read the row to prove it is reused."""
    async with harness.session_factory() as session:
        row = (
            await session.scalars(
                select(ContextForgeToolRow).where(
                    ContextForgeToolRow.name == "github_search"
                )
            )
        ).one()
        created_at, updated_at = _utc(row.created_at), _utc(row.updated_at)
    assert created_at is not None and updated_at is not None
    return created_at, updated_at


def test_upsert_snapshot_replaces_changed_fields(harness: Harness) -> None:
    async def scenario() -> tuple[ContextForgeToolRecord, datetime, datetime, datetime]:
        first = await harness.store.upsert_snapshot([_record()], ["github_search"])
        assert first.inserted == 1
        created_at, updated_before = await _row_timestamps(harness)
        await harness.store.upsert_snapshot(
            [
                _record(
                    description="new description",
                    tags=("git",),
                    parameters=("query",),
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                    content="github_search\nnew description",
                )
            ],
            ["github_search"],
        )
        found = await harness.store.get_by_call_name("cf__github_search", "user-1")
        assert found is not None
        created_after, updated_after = await _row_timestamps(harness)
        return found, created_at, created_after, max(updated_before, updated_after)

    found, created_at, created_after, updated_after = harness.run(scenario)

    assert found.id == "tool-github_search"
    assert found.description == "new description"
    assert found.tags == ("git",)
    assert found.parameters == ("query",)
    assert found.input_schema == {"type": "object", "properties": {"query": {"type": "string"}}}
    assert found.schema_hash == schema_hash_for(found.input_schema)
    assert found.content_hash == content_hash_for(
        "github_search\nnew description", "user-1", "cf__github_search"
    )
    # The row is reused: created_at survives, updated_at moves forward.
    assert created_after == created_at
    assert updated_after >= created_at


def test_upsert_snapshot_marks_missing_tools_not_callable(harness: Harness) -> None:
    async def scenario() -> tuple[
        list[ContextForgeToolRecord], list[str], ContextForgeSnapshotStats
    ]:
        await harness.store.upsert_snapshot(
            [_record("github_search"), _record("jira_search")],
            ["github_search", "jira_search"],
        )
        before = await harness.store.get_by_call_name("cf__jira_search", "user-1")
        assert before is not None
        stats = await harness.store.upsert_snapshot([_record("github_search")], ["github_search"])
        rows = await harness.store.list_all("user-1")
        callable_names = [row.name for row in await harness.store.list_callable("user-1")]
        return rows, callable_names, stats

    rows, callable_names, stats = harness.run(scenario)
    by_name = {row.name: row for row in rows}

    # The definition is kept; only runtime availability flips.
    assert set(by_name) == {"github_search", "jira_search"}
    assert by_name["jira_search"].callable is False
    assert by_name["jira_search"].description == "jira_search tool"
    assert by_name["github_search"].callable is True
    assert callable_names == ["github_search"]
    assert stats.disabled == 1
    assert stats.seen == 1


def test_upsert_snapshot_revives_a_tool_that_comes_back(harness: Harness) -> None:
    async def scenario() -> tuple[ContextForgeToolRecord, ContextForgeToolRecord]:
        await harness.store.upsert_snapshot([_record("github_search")], ["github_search"])
        gone = await harness.store.upsert_snapshot([], [])
        assert gone.disabled == 1
        dropped = await harness.store.list_all("user-1")
        assert dropped[0].callable is False
        await harness.store.upsert_snapshot(
            [_record("github_search", input_schema={"type": "object", "properties": {}})],
            ["github_search"],
        )
        rows = await harness.store.list_all("user-1")
        assert len(rows) == 1
        return dropped[0], rows[0]

    dropped, revived = harness.run(scenario)

    assert revived.id == dropped.id
    assert revived.callable is True
    assert revived.input_schema == {"type": "object", "properties": {}}
    assert revived.schema_hash == schema_hash_for({"type": "object", "properties": {}})


def test_upsert_snapshot_keeps_unowned_disabled_and_unsafe_tools_uncallable(
    harness: Harness,
) -> None:
    async def scenario() -> dict[str, ContextForgeToolRecord]:
        await harness.store.upsert_snapshot(
            [
                _record("unowned", owner_user_id=None),
                _record("disabled", enabled=False),
                _record("down", reachable=False),
                _record("bad call_name", call_name="cf__renamed"),
                _record("not_seen"),
            ],
            ["unowned", "disabled", "down", "bad call_name"],
        )
        rows = await harness.store.list_all(None)
        return {row.name: row for row in rows}

    rows = harness.run(scenario)

    # Every row is persisted (the definition is not lost) but none is runtime-visible.
    assert set(rows) == {"unowned", "disabled", "down", "bad call_name", "not_seen"}
    assert rows["unowned"].callable is False
    assert rows["disabled"].callable is False
    assert rows["down"].callable is False
    # call_name must be exactly cf__<name>; anything else is unsafe.
    assert rows["bad call_name"].callable is False
    assert rows["bad call_name"].call_name == "cf__renamed"
    # A record outside seen_names was not actually observed upstream.
    assert rows["not_seen"].callable is False


def test_upsert_snapshot_explicit_empty_seen_names_disables_everything(
    harness: Harness,
) -> None:
    async def scenario() -> list[ContextForgeToolRecord]:
        await harness.store.upsert_snapshot([_record("github_search")], [])
        return await harness.store.list_all(None)

    rows = harness.run(scenario)

    # None defaults to the record names, but an explicit empty collection means
    # the snapshot saw nothing, so no row may become callable.
    assert len(rows) == 1
    assert rows[0].name == "github_search"
    assert rows[0].callable is False


def test_upsert_snapshot_reports_insert_update_and_unchanged(harness: Harness) -> None:
    async def scenario() -> tuple[ContextForgeSnapshotStats, ContextForgeSnapshotStats]:
        first = await harness.store.upsert_snapshot(
            [_record("github_search"), _record("jira_search")],
            ["github_search", "jira_search"],
        )
        second = await harness.store.upsert_snapshot(
            [_record("github_search"), _record("jira_search", description="changed")],
            ["github_search", "jira_search"],
        )
        return first, second

    first, second = harness.run(scenario)

    assert (first.seen, first.inserted, first.updated, first.unchanged, first.disabled) == (
        2,
        2,
        0,
        0,
        0,
    )
    # A new row must reach the vector index too, so it counts as changed.
    assert first.changed_ids == ("tool-github_search", "tool-jira_search")
    assert (second.seen, second.inserted, second.updated, second.unchanged) == (2, 0, 1, 1)
    assert second.changed_ids == ("tool-jira_search",)


def test_upsert_snapshot_honours_last_seen_at_from_the_record(harness: Harness) -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)

    async def scenario() -> tuple[ContextForgeToolRecord, ContextForgeToolRecord]:
        await harness.store.upsert_snapshot(
            [_record(last_seen_at=older)], ["github_search"]
        )
        stamped = await harness.store.get_by_call_name("cf__github_search", "user-1")
        assert stamped is not None
        await harness.store.upsert_snapshot([_record()], ["github_search"])
        refreshed = await harness.store.get_by_call_name("cf__github_search", "user-1")
        assert refreshed is not None
        return stamped, refreshed

    stamped, refreshed = harness.run(scenario)

    assert _utc(stamped.last_seen_at) == older
    assert _utc(refreshed.last_seen_at) > older


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def _seed_two_owners() -> list[ContextForgeToolRecord]:
    return [
        _record("github_search"),
        _record("jira_search"),
        _record("owned_by_other", owner_user_id="user-2"),
        _record("unowned", owner_user_id=None),
    ]


def test_get_by_ids_preserves_order_and_filters_owner_and_callable(
    harness: Harness,
) -> None:
    async def scenario() -> tuple[list[str], list[str], list[str]]:
        records = _seed_two_owners()
        await harness.store.upsert_snapshot(
            records, [record.name for record in records]
        )
        ids = ["tool-jira_search", "tool-github_search", "tool-owned_by_other", "missing"]
        mine = await harness.store.get_by_ids(ids, "user-1")
        unscoped = await harness.store.get_by_ids(ids, None)
        empty = await harness.store.get_by_ids([], "user-1")
        return (
            [record.call_name for record in mine],
            [record.call_name for record in unscoped],
            [record.call_name for record in empty],
        )

    mine, unscoped, empty = harness.run(scenario)

    # Milvus rank order is preserved; foreign / unknown / non-callable ids drop out.
    assert mine == ["cf__jira_search", "cf__github_search"]
    assert unscoped == ["cf__jira_search", "cf__github_search", "cf__owned_by_other"]
    assert empty == []


def test_get_by_call_name_requires_owner_and_callable(harness: Harness) -> None:
    async def scenario() -> tuple[ContextForgeToolRecord | None, ...]:
        empty_table = await harness.store.get_by_call_name("cf__github_search", "user-1")
        records = _seed_two_owners()
        await harness.store.upsert_snapshot(records, [record.name for record in records])
        return (
            empty_table,
            await harness.store.get_by_call_name("cf__unknown", "user-1"),
            await harness.store.get_by_call_name("cf__github_search", "user-1"),
            await harness.store.get_by_call_name("cf__github_search", "user-2"),
            await harness.store.get_by_call_name("cf__unowned", "user-1"),
        )

    on_empty_table, unknown, mine, other_owner, unowned = harness.run(scenario)

    assert on_empty_table is None
    assert unknown is None
    assert mine is not None
    assert mine.id == "tool-github_search"
    # Another user's tool is invisible even though the row exists.
    assert other_owner is None
    # A tool without an owner never reaches the runtime directory.
    assert unowned is None


def test_get_by_call_name_without_owner_skips_the_owner_filter(harness: Harness) -> None:
    async def scenario() -> object:
        records = _seed_two_owners()
        await harness.store.upsert_snapshot(records, [record.name for record in records])
        return await harness.store.get_by_call_name("cf__github_search", None)

    found = harness.run(scenario)

    assert found is not None
    assert found.call_name == "cf__github_search"


def test_list_callable_and_list_all(harness: Harness) -> None:
    async def scenario() -> tuple[list[str], list[str], list[str]]:
        records = _seed_two_owners() + [_record("disabled", enabled=False)]
        await harness.store.upsert_snapshot(
            records, [record.name for record in records]
        )
        callable_names = [
            record.name for record in await harness.store.list_callable("user-1")
        ]
        all_names = [record.name for record in await harness.store.list_all("user-1")]
        everything = [record.name for record in await harness.store.list_all(None)]
        return callable_names, all_names, everything

    callable_names, all_names, everything = harness.run(scenario)

    # call_name is the stable sort key: cf__github_search < cf__jira_search.
    assert callable_names == ["github_search", "jira_search"]
    # The management view also sees rows the runtime refuses, but an owner filter
    # still keeps other people's tools out of the list.
    assert all_names == ["disabled", "github_search", "jira_search"]
    assert everything == [
        "disabled",
        "github_search",
        "jira_search",
        "owned_by_other",
        "unowned",
    ]


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_migration_0016_creates_the_table_behind_the_skill_head(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    cfg = _config(db_path)

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert TABLE in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _head_revision()

    # Drop the table through the migration itself, then let 0016 rebuild it.
    command.downgrade(cfg, "0015_skill_index_repair")
    with engine.connect() as conn:
        assert TABLE not in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0015_skill_index_repair"
        )

    command.upgrade(cfg, "0016_contextforge_tools")
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    indexes = {index["name"] for index in inspector.get_indexes(TABLE)}
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0016_contextforge_tools"
        )

    assert set(EXPECTED_COLUMNS) <= columns
    assert {"ix_contextforge_tools_callable", "ix_contextforge_tools_owner_callable"} <= indexes
    engine.dispose()


def test_migration_0016_is_reentrant_on_a_created_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0015_skill_index_repair')")
        )

    # create_all already built the table; the upgrade must not blow up on it.
    command.upgrade(_config(db_path), "head")

    with engine.connect() as conn:
        assert TABLE in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _head_revision()
    engine.dispose()
