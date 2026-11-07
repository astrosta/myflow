"""ContextForge sync service: merge MCP + Admin, write the DB, then Milvus.

The service is the only place that turns the two ContextForge views into
``contextforge_tools`` rows plus the derived Milvus index.  These tests pin
the design's merge rules (name is the join key, the MCP inputSchema wins, Admin
owns id/owner/tags/enabled/reachable), the safety rules (a tool is only indexed
when it is callable, an MCP-only tool never gets a guessed id, an ambiguous
record is dropped instead of being persisted) and the ordering guarantee (the
database commits before Milvus is touched).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import types as mcp_types
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.index import ToolIndexSyncStats
from agentkit.contextforge.models import ContextForgeTool, ContextForgeUnavailable
from agentkit.contextforge.store import (
    ContextForgeToolRecord,
    ContextForgeToolStore,
    content_hash_for,
    schema_hash_for,
)
from agentkit.contextforge.sync import ContextForgeSyncService, ContextForgeSyncStats
from agentkit.persistence.schema import Base

#: A schema with every shape a Pydantic round-trip would rewrite: it must be
#: stored exactly as ContextForge sent it.
VERBATIM_SCHEMA: dict[str, Any] = {
    "$defs": {
        "Filter": {
            "type": "object",
            "properties": {"q": {"type": "string", "default": "all"}},
        }
    },
    "type": "object",
    "properties": {
        "query": {"$ref": "#/$defs/Filter"},
        "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 10},
    },
    "required": ["query"],
}

#: What the Admin REST view carries as inputSchema; the merge must ignore it.
ADMIN_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"admin_only": {}}}


def _config(**overrides: Any) -> ContextForgeConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://cf",
        "admin_token": "adm",
        "tool_loading": "eager",
        "search": {"enabled": False},
    }
    base.update(overrides)
    return ContextForgeConfig(**base)


def _admin_tool(
    name: str = "github_search",
    tool_id: str = "tool-1",
    *,
    owner: str | None = "u1",
    display_name: str | None = "GitHub Search",
    tags: Sequence[str] = ("code", "git"),
    enabled: bool = True,
    reachable: bool = True,
    description: str = "",
    annotations: dict[str, Any] | None = None,
) -> ContextForgeTool:
    """One Admin /tools row.  The owner rides in annotations, as it does on the wire."""
    merged = dict(annotations or {})
    if owner is not None:
        merged.setdefault("myflow_owner", owner)
    return ContextForgeTool(
        id=tool_id,
        name=name,
        display_name=display_name,
        description=description,
        enabled=enabled,
        reachable=reachable,
        input_schema=dict(ADMIN_SCHEMA),
        annotations=merged,
        tags=tuple(tags),
    )


def _mcp_tool(
    name: str = "github_search",
    schema: dict[str, Any] | None = None,
    description: str = "Search GitHub code",
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema=dict(VERBATIM_SCHEMA) if schema is None else schema,
    )


class ScriptedMcpTool:
    """An MCP tool whose inputSchema is not a dict, to exercise the bad-schema path."""

    def __init__(self, name: str, schema: Any, description: str = "") -> None:
        self.name = name
        self.description = description
        self.inputSchema = schema


class FakeAdmin:
    """Admin REST /tools double: skip/limit pages, optional failure, concurrency probe."""

    def __init__(self, tools: Sequence[ContextForgeTool] = (), *, error: Exception | None = None):
        self.tools = list(tools)
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def list_tools(
        self, *, limit: int = 100, skip: int = 0, include_inactive: bool = True
    ) -> list[ContextForgeTool]:
        self.calls.append({"limit": limit, "skip": skip, "include_inactive": include_inactive})
        if self.error is not None:
            raise self.error
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return self.tools[skip : skip + limit]
        finally:
            self.in_flight -= 1


class FakeMcp:
    """MCP tools/list double."""

    def __init__(self, tools: Sequence[Any] = (), *, error: Exception | None = None) -> None:
        self.tools = list(tools)
        self.error = error
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def list_tools(self) -> list[Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return list(self.tools)
        finally:
            self.in_flight -= 1


class FakeEmbedder:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.5, 0.5] for _ in texts]


def _hash_of(record: ContextForgeToolRecord) -> str:
    return content_hash_for(record.content, record.owner_user_id, record.call_name)


class FakeIndex:
    """Milvus tool index double with the real diff semantics.

    It keeps the content hashes it has "stored", so it upserts only the records
    whose hash changed and deletes what it no longer indexes - exactly what the
    real index does on top of `query_hashes`.  It also records the records handed
    over, the ``full`` flag, the embedder, and - when it is given the store - the
    catalog rows that were already committed when the index update started.  That
    last observation is what proves the DB-before-Milvus ordering.
    """

    def __init__(
        self,
        *,
        store: ContextForgeToolStore | None = None,
        log: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self.store = store
        self.log = log if log is not None else []
        self.error = error
        self.stored: dict[str, str] = {}
        self.records: list[ContextForgeToolRecord] = []
        self.full_flags: list[bool] = []
        self.embedders: list[Any] = []
        self.committed: list[ContextForgeToolRecord] = []
        self.calls = 0

    async def sync(
        self,
        records: Sequence[ContextForgeToolRecord],
        *,
        embedder: Any,
        full: bool = False,
    ) -> ToolIndexSyncStats:
        self.calls += 1
        self.records = list(records)
        self.full_flags.append(full)
        self.embedders.append(embedder)
        if self.store is not None:
            self.committed = await self.store.list_all(None)
        self.log.append("index")
        if self.error is not None:
            return ToolIndexSyncStats(scanned=len(self.records), error=self.error)
        indexable = [record for record in self.records if record.callable]
        keep = {record.id for record in indexable}
        changed = [
            record for record in indexable if self.stored.get(record.id) != _hash_of(record)
        ]
        for record in changed:
            self.stored[record.id] = _hash_of(record)
        # An incremental sync only knows the ids it was handed; a full one prunes
        # everything the catalog no longer indexes.
        if full:
            stale = [tool_id for tool_id in self.stored if tool_id not in keep]
        else:
            queried = {record.id for record in self.records if record.id}
            stale = [
                tool_id
                for tool_id in self.stored
                if tool_id in queried and tool_id not in keep
            ]
        for tool_id in stale:
            del self.stored[tool_id]
        return ToolIndexSyncStats(
            scanned=len(self.records),
            changed=len(changed),
            upserted=len(changed),
            deleted=len(stale),
        )


class SpyStore(ContextForgeToolStore):
    """The real store, plus an ordering marker for the DB-before-Milvus assertion."""

    def __init__(self, session_factory: Any, log: list[str]) -> None:
        super().__init__(session_factory)
        self.log = log

    async def upsert_snapshot(  # type: ignore[override]
        self,
        records: Sequence[ContextForgeToolRecord],
        seen_names: Sequence[str],
    ) -> Any:
        self.log.append("db")
        return await super().upsert_snapshot(records, list(seen_names))


@dataclass
class Harness:
    store: ContextForgeToolStore
    session_factory: async_sessionmaker[AsyncSession]
    engine: Any


@asynccontextmanager
async def harness(
    tmp_path: Path, *, log: list[str] | None = None
) -> AsyncIterator[Harness]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "contextforge.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = SpyStore(factory, log) if log is not None else ContextForgeToolStore(factory)
    try:
        yield Harness(store, factory, engine)
    finally:
        await engine.dispose()


def _service(
    built: Harness,
    *,
    admin: FakeAdmin,
    mcp: FakeMcp,
    index: FakeIndex | None = None,
    embedder: FakeEmbedder | None = None,
    page_size: int = 100,
) -> ContextForgeSyncService:
    return ContextForgeSyncService(
        _config(),
        admin=admin,
        mcp=mcp,
        store=built.store,
        index=index,
        embedder=embedder,
        page_size=page_size,
    )


async def _rows(built: Harness) -> dict[str, ContextForgeToolRecord]:
    return {record.name: record for record in await built.store.list_all(None)}


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_merge_joins_on_name_with_mcp_schema_and_admin_metadata(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        service = _service(built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([_mcp_tool()]))
        stats = await service.sync("startup")

        assert stats.error is None
        assert stats.scanned == 1
        assert stats.changed == 1
        stored = await _rows(built)

    record = stored["github_search"]
    assert record.id == "tool-1"
    assert record.call_name == "cf__github_search"
    # The MCP inputSchema is verbatim; the Admin copy never wins.
    assert record.input_schema == VERBATIM_SCHEMA
    assert record.input_schema != ADMIN_SCHEMA
    # id/display/tags/owner/enabled/reachable come from Admin.
    assert record.display_name == "GitHub Search"
    assert record.tags == ("code", "git")
    assert record.owner_user_id == "u1"
    assert record.enabled is True
    assert record.reachable is True
    assert record.callable is True


@pytest.mark.asyncio
async def test_parameters_come_from_the_mcp_schema(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        service = _service(built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([_mcp_tool()]))
        await service.sync("startup")
        stored = await _rows(built)

    assert stored["github_search"].parameters == ("query", "limit")


@pytest.mark.asyncio
async def test_owner_is_read_from_annotations(tmp_path: Path) -> None:
    admin_tool = _admin_tool(owner="owner-42", annotations={"myflow_owner": "owner-42"})
    async with harness(tmp_path) as built:
        service = _service(built, admin=FakeAdmin([admin_tool]), mcp=FakeMcp([_mcp_tool()]))
        await service.sync("startup")
        stored = await _rows(built)

    assert stored["github_search"].owner_user_id == "owner-42"
    assert stored["github_search"].callable is True


@pytest.mark.asyncio
async def test_admin_only_tool_is_stored_but_never_callable(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        service = _service(
            built,
            admin=FakeAdmin([_admin_tool(name="ghost", tool_id="tool-2")]),
            mcp=FakeMcp([]),
            index=FakeIndex(),
            embedder=FakeEmbedder(),
        )
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert stats.error is None
    assert stats.scanned == 1
    record = stored["ghost"]
    assert record.id == "tool-2"
    assert record.enabled is True
    assert record.callable is False


@pytest.mark.asyncio
async def test_tool_that_leaves_mcp_keeps_its_row_and_loses_callability(
    tmp_path: Path,
) -> None:
    admin = FakeAdmin([_admin_tool()])
    mcp = FakeMcp([_mcp_tool()])
    async with harness(tmp_path) as built:
        service = _service(built, admin=admin, mcp=mcp, index=FakeIndex(), embedder=FakeEmbedder())
        await service.sync("startup")
        assert (await _rows(built))["github_search"].callable is True

        # ContextForge still lists the tool in Admin but its MCP endpoint no longer
        # executes it, so the definition survives and the runtime gate closes.
        service = _service(built, admin=admin, mcp=FakeMcp([]))
        await service.sync("admin_events")
        stored = await _rows(built)

    assert stored["github_search"].callable is False
    assert stored["github_search"].input_schema == VERBATIM_SCHEMA


@pytest.mark.asyncio
async def test_mcp_only_tool_is_skipped_without_a_guessed_id(tmp_path: Path) -> None:
    admin = FakeAdmin([_admin_tool()])
    async with harness(tmp_path) as built:
        service = _service(built, admin=admin, mcp=FakeMcp([_mcp_tool()]))
        await service.sync("startup")

        # The Admin row is gone but MCP still lists the tool: there is no id to
        # upsert, so nothing is written for it and the stored row stops being callable.
        service = _service(built, admin=FakeAdmin([]), mcp=FakeMcp([_mcp_tool()]))
        stats = await service.sync("admin_events")
        stored = await _rows(built)

    assert stats.scanned == 0
    assert set(stored) == {"github_search"}
    assert stored["github_search"].id == "tool-1"
    assert stored["github_search"].callable is False


@pytest.mark.asyncio
async def test_ambiguous_mcp_tool_is_omitted(tmp_path: Path) -> None:
    # Two tools claim one name: which schema would be stored is a guess, so the
    # snapshot drops it instead of persisting an arbitrary non-callable row.
    first = _mcp_tool(schema={"type": "object", "properties": {"a": {}}})
    second = _mcp_tool(schema={"type": "object", "properties": {"b": {}}})
    async with harness(tmp_path) as built:
        service = _service(built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([first, second]))
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert stats.error is None
    assert stats.scanned == 0
    assert stored == {}


@pytest.mark.asyncio
async def test_unparsable_schema_is_omitted(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        service = _service(
            built,
            admin=FakeAdmin([_admin_tool()]),
            mcp=FakeMcp([ScriptedMcpTool("github_search", 5)]),
        )
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert stats.error is None
    assert stored == {}


@pytest.mark.asyncio
async def test_unsafe_tool_name_is_stored_but_never_callable(tmp_path: Path) -> None:
    # The store refuses a call_name the model could not call back safely; the sync
    # still keeps the definition instead of inventing a sanitized alias.
    async with harness(tmp_path) as built:
        admin_tool = _admin_tool(name="bad name!", tool_id="tool-9")
        service = _service(
            built, admin=FakeAdmin([admin_tool]), mcp=FakeMcp([_mcp_tool(name="bad name!")])
        )
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert stats.error is None
    record = stored["bad name!"]
    assert record.call_name == "cf__bad name!"
    assert record.enabled is True
    assert record.callable is False


@pytest.mark.parametrize(
    ("enabled", "reachable", "owner"),
    [(True, True, "u1"), (False, True, "u1"), (True, False, "u1"), (True, True, None)],
)
@pytest.mark.asyncio
async def test_callability_is_derived_from_admin_state(
    tmp_path: Path, enabled: bool, reachable: bool, owner: str | None
) -> None:
    admin_tool = _admin_tool(enabled=enabled, reachable=reachable, owner=owner)
    async with harness(tmp_path) as built:
        service = _service(built, admin=FakeAdmin([admin_tool]), mcp=FakeMcp([_mcp_tool()]))
        await service.sync("startup")
        stored = await _rows(built)

    record = stored["github_search"]
    assert record.enabled is enabled
    assert record.reachable is reachable
    assert record.owner_user_id == owner
    assert record.callable is (enabled and reachable and owner is not None)


# --------------------------------------------------------------------------- #
# Ordering and failure handling
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_database_is_written_before_milvus(tmp_path: Path) -> None:
    log: list[str] = []
    async with harness(tmp_path, log=log) as built:
        index = FakeIndex(store=built.store, log=log)
        service = _service(
            built,
            admin=FakeAdmin([_admin_tool()]),
            mcp=FakeMcp([_mcp_tool()]),
            index=index,
            embedder=FakeEmbedder(),
        )
        stats = await service.sync("startup")

    assert log == ["db", "index"]
    # What the index saw is the committed catalog, not the sync's own records.
    assert [record.name for record in index.committed] == ["github_search"]
    assert index.committed[0].callable is True
    assert stats.upserted == 1


@pytest.mark.asyncio
async def test_mcp_failure_keeps_the_last_good_catalog(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        index = FakeIndex()
        embedder = FakeEmbedder()
        service = _service(
            built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([_mcp_tool()]),
            index=index, embedder=embedder,
        )
        await service.sync("startup")

        failing = _service(
            built,
            admin=FakeAdmin([_admin_tool()]),
            mcp=FakeMcp([], error=ContextForgeUnavailable("mcp down")),
            index=index,
            embedder=embedder,
        )
        stats = await failing.sync("admin_events")
        stored = await _rows(built)

    assert stats.error == "mcp_failed"
    assert stats.scanned == 0
    assert stored["github_search"].callable is True
    assert index.calls == 1  # the failed sync never reached Milvus


@pytest.mark.asyncio
async def test_admin_failure_keeps_the_last_good_catalog(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        index = FakeIndex()
        service = _service(
            built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([_mcp_tool()]),
            index=index, embedder=FakeEmbedder(),
        )
        await service.sync("startup")

        # Without the Admin view there is no owner/id, so the owner must not be
        # cleared and the tool must stay callable.
        failing = _service(
            built,
            admin=FakeAdmin(error=ContextForgeUnavailable("admin down")),
            mcp=FakeMcp([_mcp_tool()]),
            index=index,
            embedder=FakeEmbedder(),
        )
        stats = await failing.sync("admin_events")
        stored = await _rows(built)

    assert stats.error == "admin_failed"
    assert stored["github_search"].callable is True
    assert stored["github_search"].owner_user_id == "u1"
    assert index.calls == 1


@pytest.mark.asyncio
async def test_index_failure_is_degraded_not_fatal(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        service = _service(
            built,
            admin=FakeAdmin([_admin_tool()]),
            mcp=FakeMcp([_mcp_tool()]),
            index=FakeIndex(error="ToolIndexUnavailable"),
            embedder=FakeEmbedder(),
        )
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert stats.error == "ToolIndexUnavailable"
    assert stored["github_search"].callable is True


@pytest.mark.asyncio
async def test_index_crash_is_reported_as_an_error_stat(tmp_path: Path) -> None:
    class ExplodingIndex:
        async def sync(self, records: Sequence[ContextForgeToolRecord], *, embedder, full=False):
            raise RuntimeError("milvus down")

    async with harness(tmp_path) as built:
        service = _service(
            built,
            admin=FakeAdmin([_admin_tool()]),
            mcp=FakeMcp([_mcp_tool()]),
            index=ExplodingIndex(),  # type: ignore[arg-type]
            embedder=FakeEmbedder(),
        )
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert stats.error == "RuntimeError"
    assert stored["github_search"].callable is True


# --------------------------------------------------------------------------- #
# Pagination, full sync, hashes, locking
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_pagination_reads_until_a_short_page(tmp_path: Path) -> None:
    admin_tools = [_admin_tool(name=f"tool{i}", tool_id=f"id{i}") for i in range(3)]
    mcp_tools = [_mcp_tool(name=f"tool{i}") for i in range(3)]
    admin = FakeAdmin(admin_tools)
    async with harness(tmp_path) as built:
        service = _service(built, admin=admin, mcp=FakeMcp(mcp_tools), page_size=2)
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert [(call["skip"], call["limit"]) for call in admin.calls] == [(0, 2), (2, 2)]
    assert all(call["include_inactive"] is True for call in admin.calls)
    assert stats.scanned == 3
    assert set(stored) == {"tool0", "tool1", "tool2"}
    assert all(record.callable for record in stored.values())


@pytest.mark.asyncio
async def test_full_sync_prunes_the_derived_index_and_keeps_the_rows(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        index = FakeIndex()
        # A Milvus row the catalog has already lost.
        index.stored["orphan-tool"] = "deadbeef"
        service = _service(
            built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([_mcp_tool()]),
            index=index, embedder=FakeEmbedder(),
        )
        incremental = await service.sync("admin_events")
        full = await service.sync("manual", full=True)
        stored = await _rows(built)

    assert index.full_flags == [False, True]
    assert incremental.upserted == 1
    assert incremental.deleted == 0  # an incremental pass only touches ids it knows
    assert full.deleted == 1  # the full pass prunes the orphan
    assert "orphan-tool" not in index.stored
    assert full.changed == 0  # the second pass rewrote nothing
    # A full sync prunes Milvus, never the authoritative rows.
    assert stored["github_search"].callable is True


@pytest.mark.asyncio
async def test_content_and_hashes_follow_the_merged_fields(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        service = _service(built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([_mcp_tool()]))
        await service.sync("startup")
        stored = (await _rows(built))["github_search"]

    assert stored.content.splitlines()[0] == "github_search"
    assert "GitHub Search" in stored.content
    assert "Search GitHub code" in stored.content
    assert "code, git" in stored.content
    assert "query" in stored.content
    assert stored.content_hash == content_hash_for(
        stored.content, "u1", "cf__github_search"
    )
    assert stored.schema_hash == schema_hash_for(VERBATIM_SCHEMA)


@pytest.mark.asyncio
async def test_owner_move_reembeds_but_a_schema_only_edit_does_not(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        index = FakeIndex()
        service = _service(
            built, admin=FakeAdmin([_admin_tool(owner="u1")]), mcp=FakeMcp([_mcp_tool()]),
            index=index, embedder=FakeEmbedder(),
        )
        await service.sync("startup")
        before = (await _rows(built))["github_search"]

        moved = _service(
            built, admin=FakeAdmin([_admin_tool(owner="u2")]), mcp=FakeMcp([_mcp_tool()]),
            index=index, embedder=FakeEmbedder(),
        )
        moved_stats = await moved.sync("admin_events")
        after = (await _rows(built))["github_search"]

        # Same text, new owner: the content hash follows owner + call_name.
        assert after.content == before.content
        assert after.content_hash != before.content_hash
        assert after.schema_hash == before.schema_hash
        assert moved_stats.upserted == 1

        schema_edit = dict(VERBATIM_SCHEMA)
        schema_edit["required"] = []
        edited = _service(
            built,
            admin=FakeAdmin([_admin_tool(owner="u2")]),
            mcp=FakeMcp([_mcp_tool(schema=schema_edit)]),
            index=index,
            embedder=FakeEmbedder(),
        )
        edited_stats = await edited.sync("admin_events")
        final = (await _rows(built))["github_search"]

    assert final.schema_hash != after.schema_hash
    assert final.input_schema == schema_edit
    assert final.content_hash == after.content_hash
    assert final.content == after.content
    assert edited_stats.upserted == 0  # the index diff keys on content_hash


@pytest.mark.asyncio
async def test_concurrent_syncs_are_serialized_by_one_lock(tmp_path: Path) -> None:
    admin = FakeAdmin([_admin_tool()])
    mcp = FakeMcp([_mcp_tool()])
    async with harness(tmp_path) as built:
        service = _service(built, admin=admin, mcp=mcp)
        stats = await asyncio.gather(
            service.sync("admin_events"),
            service.sync("call_error"),
        )

    assert admin.max_in_flight == 1
    assert mcp.max_in_flight == 1
    assert [item.error for item in stats] == [None, None]
    assert admin.calls  # both runs read ContextForge


@pytest.mark.asyncio
async def test_manual_reindex_is_a_full_sync(tmp_path: Path) -> None:
    async with harness(tmp_path) as built:
        index = FakeIndex()
        service = _service(
            built, admin=FakeAdmin([_admin_tool()]), mcp=FakeMcp([_mcp_tool()]),
            index=index, embedder=FakeEmbedder(),
        )
        await service.reindex()

    assert index.full_flags == [True]


@pytest.mark.asyncio
async def test_disabled_config_never_touches_contextforge(tmp_path: Path) -> None:
    admin = FakeAdmin([_admin_tool()])
    mcp = FakeMcp([_mcp_tool()])
    async with harness(tmp_path) as built:
        service = ContextForgeSyncService(
            ContextForgeConfig(), admin=admin, mcp=mcp, store=built.store
        )
        stats = await service.sync("startup")
        stored = await _rows(built)

    assert stats == ContextForgeSyncStats()
    assert admin.calls == []
    assert mcp.calls == 0
    assert stored == {}


# --------------------------------------------------------------------------- #
# Catalog helpers the merge relies on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (VERBATIM_SCHEMA, ("query", "limit")),
        ({"properties": {}}, ()),
        ({}, ()),
        (None, ()),
    ],
)
def test_parameter_names_read_the_json_schema(schema: Any, expected: tuple[str, ...]) -> None:
    from agentkit.contextforge.catalog import parameter_names

    assert parameter_names(schema) == expected


def test_build_tool_content_orders_and_truncates() -> None:
    from agentkit.contextforge.catalog import build_tool_content

    content = build_tool_content(
        name="demo",
        display_name="Demo Tool",
        description="does things",
        tags=("a", "b"),
        parameters=("x",),
        max_chars=4000,
    )
    assert content.splitlines() == [
        "demo",
        "Demo Tool",
        "does things",
        "tags: a, b",
        "parameters: x",
    ]
    assert len(build_tool_content(name="demo", display_name="", max_chars=3)) == 3
