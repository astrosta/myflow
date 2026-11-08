"""mcp_tools: the owner-private tool snapshot, its call-name contract and its writer.

`mcp_tools` is what the runtime reads: the Milvus `myflow_mcp_tools` collection is a
derived retrieval index on top of it, and `tool_execute` resolves a
`mcp__<server>__<tool>` call name back to the stored `input_schema` here. These
tests pin the column set, the uniqueness the sync path depends on, and the four
invariants the store owns:

* **identity** - `id = uuid5(NAMESPACE_URL, "<server_id>:<tool_name>")` and
  `call_name = "mcp__<server.name>__<tool_name>"`; a server slug is immutable, so
  both are stable across syncs and neither comes from the caller;
* **the callable gate** - seen in this snapshot + enabled + reachable + owned +
  safe call name; derived by the store, never copied from the record, so a sync
  service cannot publish a tool the server state forbids;
* **last_seen_at** - moves only for a tool this snapshot actually saw;
* **a full-snapshot write** - a stored definition the snapshot did not see is kept
  (the definition is data) but drops out of tool_search/tool_execute.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import agentkit.persistence.schema as schema_module
from agentkit.mcp.store import (
    CALL_NAME_PREFIX,
    McpToolRecord,
    McpToolRow,
    McpToolSnapshotStats,
    McpToolStore,
    call_name_for,
    content_hash_for,
    is_safe_call_name,
    schema_hash_for,
    tool_id_for,
)
from agentkit.persistence.base import Base

TABLE = "mcp_tools"

#: Spec 4.2 in declaration order. `server_name` is the denormalised immutable slug the
#: search cards render (Task 6); created_at/updated_at trail last_seen_at.
EXPECTED_COLUMNS = (
    "id",
    "server_id",
    "server_name",
    "owner_user_id",
    "name",
    "call_name",
    "display_name",
    "description",
    "input_schema",
    "annotations",
    "parameters",
    "content",
    "content_hash",
    "schema_hash",
    "enabled",
    "reachable",
    "callable",
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
        "filters": {
            "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]
        },
    },
    "additionalProperties": False,
}

#: uuid5(NAMESPACE_URL, "s1:search"), pinned: the id is the Milvus primary key, so the
#: derivation is a contract, not an implementation detail that may drift.
TOOL_ID = "ea487357-0bb2-5650-bd25-025361559891"
LIST_ID = "aa65f61a-96c7-5a0c-b549-b03ac38bf526"

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def tool_record(
    *,
    server_id: str = "s1",
    server_name: str = "anysearch",
    name: str = "search",
    owner: str = "u1",
    **overrides: Any,
) -> McpToolRecord:
    """A complete record for one tool of one server, owned by one user.

    `callable` defaults to False on purpose: the store owns that flag, so a record
    that arrives without it must still be published correctly.
    """
    fields: dict[str, Any] = {
        "id": tool_id_for(server_id, name),
        "server_id": server_id,
        "server_name": server_name,
        "owner_user_id": owner,
        "name": name,
        "call_name": call_name_for(server_name, name),
        "display_name": name.replace("_", " ").title(),
        "description": name + " tool",
        "input_schema": VERBATIM_SCHEMA,
        "annotations": {"readOnlyHint": True},
        "parameters": ("query", "repo"),
        "enabled": True,
        "reachable": True,
        "callable": False,
        "content": name + " " + name + " tool",
    }
    fields.update(overrides)
    return McpToolRecord(**fields)


def row_values(**overrides: Any) -> dict[str, Any]:
    """Raw column values, for core inserts that bypass the store."""
    values: dict[str, Any] = {
        "id": TOOL_ID,
        "server_id": "s1",
        "server_name": "anysearch",
        "owner_user_id": "u1",
        "name": "search",
        "call_name": call_name_for("anysearch", "search"),
        "display_name": None,
        "description": "",
        "input_schema": {},
        "annotations": {},
        "parameters": [],
        "content": "",
        "content_hash": "",
        "schema_hash": "",
        "enabled": True,
        "reachable": True,
        "callable": True,
        "last_seen_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


@pytest.fixture(name="engine")
def engine_fixture():
    """Synchronous DDL surface, for constraint assertions that bypass the store."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[TABLE]])
    try:
        yield engine
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp_tools.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Base.metadata.tables[TABLE]]
            )
        )
    yield McpToolStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


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
    assert isinstance(columns["server_id"].type, String)
    assert columns["server_id"].type.length == 64
    assert isinstance(columns["server_name"].type, String)
    assert columns["server_name"].type.length == 128
    assert isinstance(columns["owner_user_id"].type, String)
    assert columns["owner_user_id"].type.length == 64
    assert isinstance(columns["name"].type, String)
    assert columns["name"].type.length == 255
    assert isinstance(columns["call_name"].type, String)
    assert columns["call_name"].type.length == 255
    assert isinstance(columns["description"].type, Text)
    assert isinstance(columns["content"].type, Text)
    assert isinstance(columns["input_schema"].type, JSON)
    assert isinstance(columns["annotations"].type, JSON)
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

    # Only the display label is optional: a tool always belongs to one server and one
    # owner, unlike the retired catalog where an unowned tool was representable.
    for name in EXPECTED_COLUMNS:
        if name == "display_name":
            continue
        assert columns[name].nullable is False, name
    assert columns["display_name"].nullable is True


def test_unique_constraints_and_lookup_indexes_exist() -> None:
    table = Base.metadata.tables[TABLE]

    uniques = {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert uniques == {
        ("uq_mcp_tools_server_name", ("server_id", "name")),
        ("uq_mcp_tools_owner_call_name", ("owner_user_id", "call_name")),
    }
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    }
    # The two runtime reads: one owner's callable tools, and one server's tools.
    assert indexes["ix_mcp_tools_owner_callable"] == ("owner_user_id", "callable")
    assert indexes["ix_mcp_tools_server_callable"] == ("server_id", "callable")


def test_model_is_registered_in_the_persistence_schema() -> None:
    assert TABLE in Base.metadata.tables
    assert schema_module.McpToolRow is McpToolRow
    assert "McpToolRow" in schema_module.__all__


def test_duplicate_server_tool_and_owner_call_name_are_rejected(engine) -> None:
    table = Base.metadata.tables[TABLE]
    with engine.begin() as conn:
        conn.execute(table.insert().values(**row_values()))

    # Two rows for the same tool of the same server.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **row_values(id="other-id", call_name="mcp__anysearch__other")
                )
            )

    # One owner with two tools that claim the same call name.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **row_values(id="third-id", server_id="s2", name="other")
                )
            )


def test_two_servers_or_two_owners_may_share_a_tool_name(engine) -> None:
    table = Base.metadata.tables[TABLE]
    with engine.begin() as conn:
        conn.execute(table.insert().values(**row_values()))
        # Same tool name, different server: distinct row and distinct call name.
        conn.execute(
            table.insert().values(
                **row_values(
                    id="beta-id",
                    server_id="s2",
                    server_name="beta",
                    call_name="mcp__beta__search",
                )
            )
        )
        # Another owner may own an identically named server and tool.
        conn.execute(
            table.insert().values(
                **row_values(id="theirs-id", server_id="s9", owner_user_id="u2")
            )
        )


# --------------------------------------------------------------------------- #
# Naming and hash contract
# --------------------------------------------------------------------------- #


def test_call_name_namespaces_server_and_tool() -> None:
    assert CALL_NAME_PREFIX == "mcp__"
    assert call_name_for("anysearch", "search") == "mcp__anysearch__search"
    # The server slug cannot contain "__", so the call name is unambiguous even when
    # the upstream tool name does.
    assert call_name_for("any", "a__b") == "mcp__any__a__b"


def test_tool_id_is_the_uuid5_of_server_and_tool_name() -> None:
    assert tool_id_for("s1", "search") == TOOL_ID
    assert tool_id_for("s1", "search") == tool_id_for("s1", "search")
    # Same tool name on two servers, or two tools on one server, never collide.
    assert tool_id_for("s2", "search") != TOOL_ID
    assert tool_id_for("s1", "list") == LIST_ID


def test_is_safe_call_name_accepts_derived_names_and_rejects_the_unsafe() -> None:
    assert is_safe_call_name("mcp__anysearch__search") is True
    assert is_safe_call_name("mcp__any-search__search_code-2") is True
    assert is_safe_call_name("mcp__a__b") is True

    for unsafe in (
        "",
        "search",
        "mcp_anysearch__search",  # wrong prefix
        "mcp__anysearch",  # no tool segment
        "mcp__anysearch__",  # empty tool segment
        " mcp__anysearch__search",  # leading space
        "mcp__anysearch__search ",  # trailing space
        "mcp__AnySearch__search",  # server slugs are lowercase
        "mcp__any_search__search",  # ... and cannot contain "_"
        "mcp__anysearch__has space",
        "mcp__anysearch__dots.here",  # no provider accepts a dot in a function name
        "mcp__anysearch__" + "x" * 200,  # over the 128-char call-name budget
    ):
        assert is_safe_call_name(unsafe) is False, unsafe


def test_content_hash_covers_content_owner_and_call_name() -> None:
    expected = hashlib.sha256(
        ("search tool" + "u1" + "mcp__anysearch__search").encode("utf-8")
    ).hexdigest()

    assert content_hash_for("search tool", "u1", "mcp__anysearch__search") == expected
    # owner and call_name participate, so a server or owner move re-indexes the tool.
    assert content_hash_for("search tool", "u2", "mcp__anysearch__search") != expected
    assert content_hash_for("search tool", "u1", "mcp__other__search") != expected
    # a missing owner hashes like the empty string, never like "None".
    assert content_hash_for("x", None, "mcp__a__x") == content_hash_for("x", "", "mcp__a__x")


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


@pytest.mark.asyncio
async def test_snapshot_stores_verbatim_schema_and_recomputes_hashes(
    store: McpToolStore,
) -> None:
    record = tool_record(content="search a repository")
    await store.upsert_snapshot([record], seen_ids={record.id})
    found = await store.get_by_call_name(record.call_name, "u1")

    assert found is not None
    assert found.id == TOOL_ID
    assert found.server_id == "s1"
    assert found.server_name == "anysearch"
    assert found.name == "search"
    assert found.call_name == "mcp__anysearch__search"
    # $ref / anyOf / default survive untouched: tool_execute validates against this.
    assert found.input_schema == VERBATIM_SCHEMA
    assert found.annotations == {"readOnlyHint": True}
    assert found.parameters == ("query", "repo")
    assert found.content == "search a repository"
    assert found.content_hash == content_hash_for(
        "search a repository", "u1", "mcp__anysearch__search"
    )
    assert found.schema_hash == schema_hash_for(VERBATIM_SCHEMA)
    assert found.last_seen_at is not None
    assert _utc(found.last_seen_at).tzinfo is not None


@pytest.mark.asyncio
async def test_the_store_recomputes_the_hashes_instead_of_trusting_them(
    store: McpToolStore,
) -> None:
    record = tool_record(content_hash="stale", schema_hash="stale")
    await store.upsert_snapshot([record], seen_ids={record.id})
    found = await store.get_by_call_name(record.call_name, "u1")

    assert found is not None
    assert found.content_hash == content_hash_for(record.content, "u1", record.call_name)
    assert found.schema_hash == schema_hash_for(VERBATIM_SCHEMA)


@pytest.mark.asyncio
async def test_call_name_and_callable_gate(store: McpToolStore) -> None:
    record = tool_record(server_id="s1", server_name="anysearch", name="search", owner="u1")
    assert call_name_for("anysearch", "search") == "mcp__anysearch__search"

    await store.upsert_snapshot([record], seen_ids={record.id})

    mine = await store.get_by_call_name(record.call_name, "u1")
    assert mine is not None
    assert mine.callable is True
    # Another user can never resolve this tool by guessing its call name.
    assert await store.get_by_call_name(record.call_name, "u2") is None


@pytest.mark.asyncio
async def test_store_derives_the_id_from_server_and_tool_name(store: McpToolStore) -> None:
    record = tool_record(id="")
    stats = await store.upsert_snapshot([record], seen_ids={tool_id_for("s1", "search")})
    stored = await store.get_by_call_name(record.call_name, "u1")

    assert stored is not None
    assert stored.id == TOOL_ID
    # The Milvus primary key is the derived id, so the upsert set must carry it too.
    assert stats.upsert_ids == (TOOL_ID,)


@pytest.mark.asyncio
async def test_store_derives_callable_instead_of_trusting_the_record(
    store: McpToolStore,
) -> None:
    derived = tool_record(name="derived", callable=False)
    blocked = tool_record(name="blocked", enabled=False, callable=True)
    down = tool_record(name="down", reachable=False, callable=True)
    renamed = tool_record(name="renamed", call_name="mcp__elsewhere__renamed", callable=True)
    unsafe = tool_record(name="has space", callable=True)

    await store.upsert_snapshot(
        [derived, blocked, down, renamed, unsafe],
        seen_ids={derived.id, blocked.id, down.id, renamed.id, unsafe.id},
    )
    rows = {row.name: row for row in await store.list_all("u1")}

    # A valid tool becomes callable even though the record left the flag at False.
    assert rows["derived"].callable is True
    # ... and callable=True cannot promote a tool the server state forbids.
    assert rows["blocked"].callable is False
    assert rows["down"].callable is False
    # call_name must be exactly mcp__<server>__<tool>.
    assert rows["renamed"].call_name == "mcp__elsewhere__renamed"
    assert rows["renamed"].callable is False
    # An upstream name the model could not call back stays a stored definition only.
    assert rows["has space"].callable is False


@pytest.mark.asyncio
async def test_unseen_and_disabled_are_not_callable(store: McpToolStore) -> None:
    record = tool_record(enabled=False)
    await store.upsert_snapshot([record], seen_ids={record.id})

    rows = await store.list_all()
    assert len(rows) == 1
    assert rows[0].callable is False


@pytest.mark.asyncio
async def test_a_record_that_was_not_seen_is_not_callable(store: McpToolStore) -> None:
    record = tool_record()
    await store.upsert_snapshot([record], seen_ids=set())

    rows = await store.list_all(None)
    assert len(rows) == 1
    assert rows[0].callable is False


@pytest.mark.asyncio
async def test_upsert_requires_seen_ids(store: McpToolStore) -> None:
    record = tool_record()

    with pytest.raises(TypeError):
        await store.upsert_snapshot([record])  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="seen_ids"):
        await store.upsert_snapshot([record], None)  # type: ignore[arg-type]

    # A caller that forgot the observation must not have published anything.
    assert await store.list_all(None) == []


@pytest.mark.asyncio
async def test_server_id_and_owner_are_required(store: McpToolStore) -> None:
    for record in (
        tool_record(server_id=""),
        tool_record(owner=""),
        tool_record(server_name="", call_name=""),
    ):
        with pytest.raises(ValueError):
            await store.upsert_snapshot([record], seen_ids={record.id})

    assert await store.list_all(None) == []


@pytest.mark.asyncio
async def test_names_and_labels_are_bounded_at_the_storage_boundary(
    store: McpToolStore,
) -> None:
    """PostgreSQL enforces the column bounds and SQLite does not, so the store does."""
    unnamable = tool_record(name="x" * 300)
    long_title = tool_record(name="search", display_name="T" * 900)

    stats = await store.upsert_snapshot(
        [unnamable, long_title], seen_ids={unnamable.id, long_title.id}
    )
    rows = await store.list_all("u1")

    # A name longer than the column could hold is not a tool anything can call back.
    assert [row.name for row in rows] == ["search"]
    assert stats.inserted == 1
    assert stats.skipped == 1
    # A runaway title is cosmetic, so it is truncated instead of rejected.
    assert rows[0].display_name == "T" * 512


@pytest.mark.asyncio
async def test_call_name_provider_budget_boundary_is_exact(store: McpToolStore) -> None:
    """128 characters may be called, 129 may only be stored.

    `mcp__anysearch__` is 16 characters, so the tool name carries the difference.
    """
    at_budget = tool_record(name="a" * 112)
    over_budget = tool_record(name="a" * 113)
    assert len(call_name_for("anysearch", at_budget.name)) == 128
    assert len(call_name_for("anysearch", over_budget.name)) == 129

    await store.upsert_snapshot(
        [at_budget, over_budget], seen_ids={at_budget.id, over_budget.id}
    )
    rows = {row.name: row for row in await store.list_all("u1")}

    assert rows[at_budget.name].callable is True
    # Stored (the definition is data) but outside every provider's function-name
    # budget, so it is never offered to the model.
    assert rows[over_budget.name].callable is False
    assert len(rows[over_budget.name].call_name) == 129


@pytest.mark.asyncio
async def test_call_name_column_boundary_is_exact(store: McpToolStore) -> None:
    """A derived call name that fits `String(255)` is stored; one char more is dropped."""
    fitted = tool_record(name="a" * 239)
    oversized = tool_record(name="a" * 240)
    assert len(call_name_for("anysearch", fitted.name)) == 255
    assert len(call_name_for("anysearch", oversized.name)) == 256

    stats = await store.upsert_snapshot(
        [fitted, oversized], seen_ids={fitted.id, oversized.id}
    )
    rows = {row.name: row for row in await store.list_all("u1")}

    assert set(rows) == {fitted.name}
    assert rows[fitted.name].call_name == call_name_for("anysearch", fitted.name)
    assert stats.inserted == 1
    assert stats.skipped == 1


@pytest.mark.asyncio
async def test_the_name_column_boundary_is_exact(store: McpToolStore) -> None:
    """The tool name itself is bounded at 255, independent of the call-name bound."""
    fitted = tool_record(name="a" * 255, call_name="mcp__a__x")
    oversized = tool_record(name="a" * 256, call_name="mcp__a__x")

    stats = await store.upsert_snapshot(
        [fitted, oversized], seen_ids={fitted.id, oversized.id}
    )
    rows = {row.name for row in await store.list_all("u1")}

    assert rows == {fitted.name}
    assert stats.skipped == 1


@pytest.mark.asyncio
async def test_the_server_name_column_boundary_is_exact(store: McpToolStore) -> None:
    """`mcp_tools.server_name` is `String(128)`, the bound a server slug is created with."""
    fitted = tool_record(server_name="a" * 128, name="search")
    oversized = tool_record(server_name="a" * 129, name="search")

    stats = await store.upsert_snapshot(
        [fitted, oversized], seen_ids={fitted.id, oversized.id}
    )
    stored = await store.list_all("u1")

    assert [row.server_name for row in stored] == ["a" * 128]
    assert stats.inserted == 1
    assert stats.skipped == 1


@pytest.mark.asyncio
async def test_one_unstorable_record_does_not_abort_the_snapshot(
    store: McpToolStore,
) -> None:
    """Fail closed, one record at a time: a malformed tool never sinks the sync."""
    good = tool_record(name="search")
    await store.upsert_snapshot([good], seen_ids={good.id})
    assert (await store.get_by_call_name(good.call_name, "u1")).callable is True

    # The same tool now arrives with a server name that cannot be stored, next to a
    # healthy server's tool.
    broken = tool_record(name="search", server_name="a" * 129)
    other = tool_record(server_id="s2", server_name="beta", name="list")
    stats = await store.upsert_snapshot([broken, other], seen_ids={broken.id, other.id})

    rows = {row.name: row for row in await store.list_all("u1")}
    # The healthy record is published...
    assert rows["list"].callable is True
    # ... the unstorable one was dropped (nothing is written for it) ...
    assert stats.skipped == 1
    # ... and the row it would have rewritten is retired, not left callable.
    assert rows["search"].callable is False
    assert stats.delete_ids == (good.id,)


@pytest.mark.asyncio
async def test_server_name_is_recovered_from_the_call_name(store: McpToolStore) -> None:
    """The sync may pass server_name directly or only the derived call name."""
    record = tool_record(server_name="", call_name="mcp__anysearch__search")
    await store.upsert_snapshot([record], seen_ids={record.id})

    stored = await store.get_by_call_name("mcp__anysearch__search", "u1")
    assert stored is not None
    assert stored.server_name == "anysearch"
    assert stored.callable is True


@pytest.mark.asyncio
async def test_last_seen_moves_only_for_tools_the_snapshot_saw(
    store: McpToolStore,
) -> None:
    first_seen = datetime(2026, 1, 1, tzinfo=UTC)
    later = datetime(2027, 1, 1, tzinfo=UTC)

    record = tool_record(last_seen_at=first_seen)
    await store.upsert_snapshot([record], seen_ids={record.id})
    before = (await store.list_all("u1"))[0]

    # The sync carries the stored row for an unreachable server, but that server was
    # not reloaded, so its sighting stamp must not move.
    carried = tool_record(last_seen_at=later, reachable=False)
    await store.upsert_snapshot([carried], seen_ids=set())
    after = (await store.list_all("u1"))[0]

    assert _utc(before.last_seen_at) == first_seen
    assert _utc(after.last_seen_at) == first_seen
    assert after.callable is False

    # A tool the snapshot really saw is stamped with the new sighting.
    await store.upsert_snapshot([record], seen_ids={record.id})
    refreshed = (await store.list_all("u1"))[0]
    assert _utc(refreshed.last_seen_at) == first_seen
    await store.upsert_snapshot(
        [tool_record(last_seen_at=later)], seen_ids={record.id}
    )
    moved = (await store.list_all("u1"))[0]
    assert _utc(moved.last_seen_at) == later


@pytest.mark.asyncio
async def test_a_full_snapshot_retires_tools_it_did_not_cover(store: McpToolStore) -> None:
    removed = tool_record(name="removed")
    await store.upsert_snapshot([removed], seen_ids={removed.id})

    # The sync only sends what tools/list returned: a tool that disappeared upstream
    # is absent from both records and seen_ids.
    kept = tool_record(name="search")
    stats = await store.upsert_snapshot([kept], seen_ids={kept.id})
    rows = {row.name: row for row in await store.list_all("u1")}

    assert set(rows) == {"removed", "search"}
    assert rows["removed"].callable is False
    # The definition is data and is kept; only runtime availability flips.
    assert rows["removed"].description == "removed tool"
    assert rows["search"].callable is True
    assert stats.disabled == 1
    assert stats.delete_ids == (removed.id,)

    # A tool that comes back reuses its row and its id.
    revived = await store.upsert_snapshot([removed], seen_ids={removed.id})
    rows = {row.name: row for row in await store.list_all("u1")}
    assert rows["removed"].callable is True
    assert rows["removed"].id == removed.id
    assert revived.upsert_ids == (removed.id,)


@pytest.mark.asyncio
async def test_an_empty_snapshot_retires_everything(store: McpToolStore) -> None:
    record = tool_record()
    await store.upsert_snapshot([record], seen_ids={record.id})

    stats = await store.upsert_snapshot([], set())

    assert stats.seen == 0
    assert stats.disabled == 1
    assert stats.delete_ids == (TOOL_ID,)
    rows = await store.list_all(None)
    assert len(rows) == 1
    assert rows[0].callable is False


@pytest.mark.asyncio
async def test_stats_split_the_index_upsert_and_delete_sets(store: McpToolStore) -> None:
    search = tool_record(name="search")
    listing = tool_record(name="list")
    seen_ids = {search.id, listing.id}

    first = await store.upsert_snapshot([search, listing], seen_ids)
    assert (
        first.seen,
        first.inserted,
        first.updated,
        first.unchanged,
        first.disabled,
    ) == (2, 2, 0, 0, 0)
    assert first.upsert_ids == (search.id, listing.id)
    assert first.delete_ids == ()

    # One definition changed, one tool became disabled.
    second = await store.upsert_snapshot(
        [
            tool_record(name="search", description="changed"),
            tool_record(name="list", enabled=False),
        ],
        seen_ids,
    )
    assert (second.seen, second.inserted, second.updated, second.unchanged) == (2, 0, 2, 0)
    assert second.disabled == 1
    assert second.upsert_ids == (search.id,)
    assert second.delete_ids == (listing.id,)

    # The unchanged definition is not re-embedded; the revived one is.
    third = await store.upsert_snapshot(
        [tool_record(name="search", description="changed"), tool_record(name="list")],
        seen_ids,
    )
    assert (third.updated, third.unchanged) == (1, 1)
    assert third.upsert_ids == (listing.id,)
    assert third.delete_ids == ()


@pytest.mark.asyncio
async def test_the_same_tool_name_on_two_servers_stays_distinct(store: McpToolStore) -> None:
    alpha = tool_record(server_id="s1", server_name="alpha")
    beta = tool_record(server_id="s2", server_name="beta")
    await store.upsert_snapshot([alpha, beta], seen_ids={alpha.id, beta.id})

    one = await store.get_by_call_name("mcp__alpha__search", "u1")
    two = await store.get_by_call_name("mcp__beta__search", "u1")
    assert one is not None and one.server_id == "s1"
    assert two is not None and two.server_id == "s2"
    assert one.id != two.id


@pytest.mark.asyncio
async def test_two_owners_may_share_a_call_name(store: McpToolStore) -> None:
    mine = tool_record(server_id="s1", owner="u1")
    theirs = tool_record(server_id="s9", owner="u2")
    await store.upsert_snapshot([mine, theirs], seen_ids={mine.id, theirs.id})

    assert (await store.get_by_call_name("mcp__anysearch__search", "u1")).server_id == "s1"
    assert (await store.get_by_call_name("mcp__anysearch__search", "u2")).server_id == "s9"
    assert await store.get_by_call_name("mcp__anysearch__search", "u3") is None


@pytest.mark.asyncio
async def test_the_row_is_reused_across_snapshots(store: McpToolStore) -> None:
    record = tool_record()
    await store.upsert_snapshot([record], seen_ids={record.id})
    created = (await store.list_all("u1"))[0]

    updated = await store.upsert_snapshot(
        [tool_record(description="new description")], seen_ids={record.id}
    )
    after = (await store.list_all("u1"))[0]

    assert updated.updated == 1
    assert after.id == created.id
    assert after.description == "new description"
    assert len(await store.list_all("u1")) == 1


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_by_ids_returns_only_callable_owned_rows_in_rank_order(
    store: McpToolStore,
) -> None:
    public = tool_record(name="search")
    broken = tool_record(name="list", enabled=False)
    foreign = tool_record(server_id="s9", owner="u2")
    await store.upsert_snapshot(
        [public, broken, foreign], seen_ids={public.id, broken.id, foreign.id}
    )

    ranked = await store.get_by_ids([broken.id, foreign.id, public.id], "u1")
    assert [row.id for row in ranked] == [public.id]
    assert await store.get_by_ids([], "u1") == []
    assert [row.id for row in await store.get_by_ids([public.id, foreign.id], "u2")] == [
        foreign.id
    ]
    # A duplicate Milvus hit must not duplicate the row.
    assert [row.id for row in await store.get_by_ids([public.id, public.id], "u1")] == [
        public.id
    ]


@pytest.mark.asyncio
async def test_list_for_server_is_owner_scoped_and_keeps_non_callable_rows(
    store: McpToolStore,
) -> None:
    search = tool_record(name="search")
    listing = tool_record(name="list", enabled=False)
    other = tool_record(server_id="s2", server_name="beta")
    await store.upsert_snapshot(
        [search, listing, other], seen_ids={search.id, listing.id, other.id}
    )

    # The management view shows every tool of the server, callable or not.
    assert [row.name for row in await store.list_for_server("s1", "u1")] == ["list", "search"]
    assert await store.list_for_server("s1", "u2") == []
    assert await store.list_for_server("missing", "u1") == []


@pytest.mark.asyncio
async def test_list_all_is_the_admin_view(store: McpToolStore) -> None:
    mine = tool_record(name="search")
    theirs = tool_record(server_id="s9", owner="u2")
    await store.upsert_snapshot([mine, theirs], seen_ids={mine.id, theirs.id})

    assert {row.owner_user_id for row in await store.list_all()} == {"u1", "u2"}
    assert [row.owner_user_id for row in await store.list_all("u2")] == ["u2"]


@pytest.mark.asyncio
async def test_owner_is_required_for_every_scoped_read(store: McpToolStore) -> None:
    for call in (
        lambda: store.get_by_call_name("mcp__anysearch__search", ""),
        lambda: store.get_by_ids([], ""),
        lambda: store.list_for_server("s1", ""),
        # None is the deliberate admin view; "" is never a user id.
        lambda: store.list_all(""),
    ):
        with pytest.raises(ValueError, match="owner_user_id"):
            await call()


@pytest.mark.asyncio
async def test_delete_by_server_removes_every_tool_of_that_server(
    store: McpToolStore,
) -> None:
    search = tool_record(name="search")
    listing = tool_record(name="list", enabled=False)
    other = tool_record(server_id="s2", server_name="beta")
    await store.upsert_snapshot(
        [search, listing, other], seen_ids={search.id, listing.id, other.id}
    )

    removed = await store.delete_by_server("s1")

    # Every id comes back - callable or not - so the index cannot keep a stale vector.
    assert set(removed) == {search.id, listing.id}
    assert await store.list_for_server("s1", "u1") == []
    assert [row.name for row in await store.list_all("u1")] == ["search"]
    assert await store.delete_by_server("s1") == []
    assert await store.delete_by_server("") == []


@pytest.mark.asyncio
async def test_a_snapshot_reports_its_stats_type(store: McpToolStore) -> None:
    stats = await store.upsert_snapshot([], set())

    assert isinstance(stats, McpToolSnapshotStats)
    assert (stats.seen, stats.inserted, stats.updated, stats.unchanged) == (0, 0, 0, 0)
