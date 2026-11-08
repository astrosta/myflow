"""mcp_servers: owner-private MCP server rows, uniqueness and lifecycle stamps.

A server is user-private, so every read and write is owner-scoped: the only way to
reach a row is with the owner that created it, and `list_all` is the admin surface.
`name` is a stable slug that is unique per owner and immutable after creation -
that is what keeps the derived `mcp__<server>__<tool>` call names stable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.mcp.models import MAX_ERROR_CHARS, McpServerRecord
from agentkit.mcp.store import McpServerRow, McpServerStore
from agentkit.persistence.base import Base

TABLE = "mcp_servers"

EXPECTED_COLUMNS = (
    "id",
    "owner_user_id",
    "name",
    "display_name",
    "description",
    "transport",
    "url",
    "headers_encrypted",
    "verify_tls",
    "enabled",
    "status",
    "reachable",
    "last_error",
    "last_connected_at",
    "last_tools_sync_at",
    "created_at",
    "updated_at",
)

CONNECTED_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def server_record(
    *, owner: str = "u1", name: str = "anysearch", **overrides: Any
) -> McpServerRecord:
    fields: dict[str, Any] = {
        "owner_user_id": owner,
        "name": name,
        "display_name": "AnySearch",
        "description": "实时搜索",
        "transport": "streamable_http",
        "url": "https://api.anysearch.com/mcp",
        "headers_encrypted": "gAAAAA-ciphertext",
    }
    fields.update(overrides)
    return McpServerRecord(**fields)


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "mcp.db").as_posix())
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Base.metadata.tables[TABLE]]
            )
        )
    yield McpServerStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


# --------------------------------------------------------------------------- #
# Table shape
# --------------------------------------------------------------------------- #


def test_table_shape_matches_the_design() -> None:
    table = Base.metadata.tables[TABLE]

    assert tuple(table.columns.keys()) == EXPECTED_COLUMNS
    assert table.columns["owner_user_id"].nullable is False
    assert table.columns["name"].nullable is False
    assert table.columns["url"].nullable is False
    assert table.columns["transport"].nullable is False
    assert table.columns["enabled"].nullable is False
    assert table.columns["status"].nullable is False
    assert table.columns["reachable"].nullable is False
    assert table.columns["verify_tls"].nullable is False
    assert table.columns["display_name"].nullable is True
    assert table.columns["headers_encrypted"].nullable is True
    assert table.columns["last_error"].nullable is True


def test_name_is_unique_per_owner_and_indexed_for_the_catalog_queries() -> None:
    table = Base.metadata.tables[TABLE]

    unique = next(c for c in table.constraints if isinstance(c, UniqueConstraint))
    assert unique.name == "uq_mcp_servers_owner_name"
    assert [column.name for column in unique.columns] == ["owner_user_id", "name"]
    # The catalog queries filter one owner's enabled servers by status.
    index = next(i for i in table.indexes if i.name == "ix_mcp_servers_owner_enabled_status")
    assert [column.name for column in index.columns] == ["owner_user_id", "enabled", "status"]


def test_schema_module_registers_the_table() -> None:
    import agentkit.persistence.schema as schema_module

    assert TABLE in Base.metadata.tables
    assert schema_module.McpServerRow is McpServerRow
    assert "McpServerRow" in schema_module.__all__


# --------------------------------------------------------------------------- #
# Reads and writes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_owner_isolation_and_duplicate_name(store: McpServerStore) -> None:
    mine = await store.create(server_record(owner="u1", name="anysearch"))
    assert await store.get(mine.id, "u1") == mine
    assert await store.get(mine.id, "u2") is None
    with pytest.raises(IntegrityError):
        await store.create(server_record(owner="u1", name="anysearch"))
    other = await store.create(server_record(owner="u2", name="anysearch"))
    assert other.id != mine.id


@pytest.mark.asyncio
async def test_create_stamps_a_new_pending_server(store: McpServerStore) -> None:
    created = await store.create(server_record())

    assert created.id
    assert created.status == "pending"
    assert created.reachable is False
    assert created.enabled is True
    assert created.verify_tls is True
    assert created.last_error is None
    assert created.last_connected_at is None
    assert created.created_at is not None
    assert created.updated_at is not None


@pytest.mark.asyncio
async def test_create_keeps_an_explicit_id(store: McpServerStore) -> None:
    created = await store.create(server_record(id="server-1"))

    assert created.id == "server-1"
    assert (await store.get("server-1", "u1")) == created


@pytest.mark.asyncio
async def test_a_disabled_server_is_created_as_disabled(store: McpServerStore) -> None:
    created = await store.create(server_record(enabled=False))

    assert created.enabled is False
    assert created.status == "disabled"


@pytest.mark.asyncio
async def test_create_does_not_trust_the_caller_derivation_fields(
    store: McpServerStore,
) -> None:
    """status/reachable are what a connect produced, not what a caller claims.

    A freshly created server has never been contacted, so `active`/`reachable=True` on
    the incoming record are output flags the store overwrites: otherwise any write path
    could publish a server that never answered an initialize.
    """
    created = await store.create(server_record(status="active", reachable=True))

    assert created.status == "pending"
    assert created.reachable is False

    disabled = await store.create(
        server_record(name="other", enabled=False, status="active", reachable=True)
    )
    assert disabled.status == "disabled"
    assert disabled.reachable is False


@pytest.mark.asyncio
async def test_get_by_name_is_owner_scoped(store: McpServerStore) -> None:
    mine = await store.create(server_record(owner="u1", name="anysearch"))

    assert await store.get_by_name("u1", "anysearch") == mine
    assert await store.get_by_name("u2", "anysearch") is None
    assert await store.get_by_name("u1", "missing") is None


@pytest.mark.asyncio
async def test_lists_are_owner_scoped_and_ordered(store: McpServerStore) -> None:
    await store.create(server_record(owner="u1", name="zeta"))
    await store.create(server_record(owner="u1", name="alpha"))
    await store.create(server_record(owner="u2", name="beta"))

    assert [row.name for row in await store.list_for_owner("u1")] == ["alpha", "zeta"]
    assert [row.name for row in await store.list_for_owner("u2")] == ["beta"]
    assert [row.name for row in await store.list_all()] == ["alpha", "zeta", "beta"]


@pytest.mark.asyncio
async def test_owner_is_required_for_every_scoped_read(store: McpServerStore) -> None:
    created = await store.create(server_record())

    for call in (
        lambda: store.get(created.id, ""),
        lambda: store.get_by_name("", "anysearch"),
        lambda: store.list_for_owner(""),
    ):
        with pytest.raises(ValueError, match="owner_user_id"):
            await call()


@pytest.mark.asyncio
async def test_name_must_be_a_lowercase_slug(store: McpServerStore) -> None:
    for name in ("AnySearch", "any search", "any_search", "any.search", ""):
        with pytest.raises(ValueError, match="name"):
            await store.create(server_record(name=name))


@pytest.mark.asyncio
async def test_transport_and_status_are_validated(store: McpServerStore) -> None:
    with pytest.raises(ValueError, match="transport"):
        await store.create(server_record(transport="stdio"))
    with pytest.raises(ValueError, match="status"):
        await store.create(server_record(status="broken"))


@pytest.mark.asyncio
async def test_update_edits_the_mutable_columns(store: McpServerStore) -> None:
    created = await store.create(server_record())

    updated = await store.update(
        created.id,
        "u1",
        {
            "display_name": "AnySearch 2",
            "description": "realtime search",
            "url": "https://api.anysearch.com/v2/mcp",
            "transport": "sse",
            "headers_encrypted": "gAAAAA-new",
            "verify_tls": False,
        },
    )

    assert updated is not None
    assert updated.name == created.name
    assert updated.display_name == "AnySearch 2"
    assert updated.url == "https://api.anysearch.com/v2/mcp"
    assert updated.transport == "sse"
    assert updated.headers_encrypted == "gAAAAA-new"
    assert updated.verify_tls is False
    assert await store.get(created.id, "u1") == updated


@pytest.mark.asyncio
async def test_update_refuses_to_rename_or_move_a_server(store: McpServerStore) -> None:
    created = await store.create(server_record())

    with pytest.raises(ValueError, match="name"):
        await store.update(created.id, "u1", {"name": "renamed"})
    with pytest.raises(ValueError, match="owner_user_id"):
        await store.update(created.id, "u1", {"owner_user_id": "u2"})
    with pytest.raises(ValueError, match="column"):
        await store.update(created.id, "u1", {"id": "other"})

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.name == "anysearch"
    assert stored.owner_user_id == "u1"


@pytest.mark.asyncio
async def test_update_refuses_the_state_columns(store: McpServerStore) -> None:
    """enabled/status/reachable are derived state, not edit-form fields."""
    created = await store.create(server_record())

    for column in ("enabled", "status", "reachable", "last_error"):
        with pytest.raises(ValueError, match="column"):
            await store.update(created.id, "u1", {column: True})


@pytest.mark.asyncio
async def test_update_returns_none_for_another_owner(store: McpServerStore) -> None:
    created = await store.create(server_record(owner="u1"))

    assert await store.update(created.id, "u2", {"display_name": "hijacked"}) is None
    stored = await store.get(created.id, "u1")
    assert stored is not None and stored.display_name == "AnySearch"


@pytest.mark.asyncio
async def test_update_validates_the_new_transport(store: McpServerStore) -> None:
    created = await store.create(server_record())

    with pytest.raises(ValueError, match="transport"):
        await store.update(created.id, "u1", {"transport": "stdio"})


@pytest.mark.asyncio
async def test_set_enabled_derives_the_status(store: McpServerStore) -> None:
    created = await store.create(server_record())

    disabled = await store.set_enabled(created.id, "u1", False)
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.status == "disabled"

    enabled = await store.set_enabled(created.id, "u1", True)
    assert enabled is not None
    assert enabled.enabled is True
    # Re-enabling forces a fresh tools/list before it counts as active again.
    assert enabled.status == "pending"

    assert await store.set_enabled(created.id, "u2", True) is None


@pytest.mark.asyncio
async def test_set_status_records_a_successful_connect(store: McpServerStore) -> None:
    created = await store.create(server_record())

    updated = await store.set_status(
        created.id,
        "u1",
        status="active",
        reachable=True,
        last_connected_at=CONNECTED_AT,
        last_tools_sync_at=CONNECTED_AT,
    )

    assert updated is not None
    assert updated.status == "active"
    assert updated.reachable is True
    assert updated.last_error is None
    assert updated.last_connected_at == CONNECTED_AT
    assert updated.last_tools_sync_at == CONNECTED_AT


@pytest.mark.asyncio
async def test_set_status_records_a_failure_and_keeps_the_last_success(
    store: McpServerStore,
) -> None:
    created = await store.create(server_record())
    await store.set_status(created.id, "u1", status="active", reachable=True, last_connected_at=CONNECTED_AT)

    failed = await store.set_status(
        created.id, "u1", status="error", reachable=False, last_error="connection refused"
    )

    assert failed is not None
    assert failed.status == "error"
    assert failed.reachable is False
    assert failed.last_error == "connection refused"
    assert failed.last_connected_at == CONNECTED_AT


@pytest.mark.asyncio
async def test_set_status_truncates_a_runaway_error(store: McpServerStore) -> None:
    created = await store.create(server_record())

    failed = await store.set_status(
        created.id, "u1", status="error", reachable=False, last_error="x" * (MAX_ERROR_CHARS + 500)
    )

    assert failed is not None
    assert failed.last_error is not None
    assert len(failed.last_error) == MAX_ERROR_CHARS


@pytest.mark.asyncio
async def test_a_successful_connect_clears_the_previous_error(store: McpServerStore) -> None:
    created = await store.create(server_record())
    await store.set_status(created.id, "u1", status="error", reachable=False, last_error="boom")

    recovered = await store.set_status(
        created.id, "u1", status="active", reachable=True, last_connected_at=CONNECTED_AT
    )

    assert recovered is not None
    assert recovered.last_error is None
    assert recovered.last_connected_at == CONNECTED_AT


@pytest.mark.asyncio
async def test_set_status_validates_the_status(store: McpServerStore) -> None:
    created = await store.create(server_record())

    with pytest.raises(ValueError, match="status"):
        await store.set_status(created.id, "u1", status="broken", reachable=True)


@pytest.mark.asyncio
async def test_set_status_returns_none_for_another_owner(store: McpServerStore) -> None:
    created = await store.create(server_record(owner="u1"))

    assert (
        await store.set_status(created.id, "u2", status="active", reachable=True)
    ) is None


@pytest.mark.asyncio
async def test_delete_is_owner_scoped(store: McpServerStore) -> None:
    created = await store.create(server_record(owner="u1"))

    assert await store.delete(created.id, "u2") is False
    assert await store.get(created.id, "u1") is not None

    assert await store.delete(created.id, "u1") is True
    assert await store.get(created.id, "u1") is None
    assert await store.delete(created.id, "u1") is False
