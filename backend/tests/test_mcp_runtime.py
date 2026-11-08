"""MCP tool runtime: startup pass, single-flight refresh, per-target results, calls.

The runtime owns what the catalog sync deliberately does not: when a pass runs (one
per process, under one lock), what `mcp_servers` says after it, and the single
place a `tools/call` leaves this process.  These tests pin the decisions that are
easy to get subtly wrong:

* one startup pass, and two triggers during a running pass are exactly one
  follow-up pass - never two concurrent snapshots;
* `refresh_server` reports the outcome for the server it was asked about, so one
  broken server cannot veto another server's retry and a catalog-wide success
  cannot claim a target that failed;
* a server stamp follows `failed_servers`: `error`/unreachable with the
  sanitized note, `active`/reachable with a fresh `last_tools_sync_at`, and
  nothing at all for a disabled server (nothing contacted it);
* `call_tool` re-checks owner/enabled/reachable, decrypts headers with the
  process cipher, and calls the *upstream* tool name on the owning server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mcp import types as mcp_types
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.config.mcp_servers_config import McpServersConfig
from agentkit.config.tool_search_config import ToolSearchConfig
from agentkit.mcp.client import McpServerConnection
from agentkit.mcp.crypto import HeaderCipher
from agentkit.mcp.errors import (
    McpServerConfigurationError,
    McpServerNotFound,
    McpServerTimeout,
    McpServerUnavailable,
    McpServerValidationError,
    McpToolError,
)
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.runtime import (
    McpServerSyncResult,
    McpToolRuntime,
    build_mcp_runtime,
    seconds_until_local_midnight,
)
from agentkit.mcp.runtime_protocol import (
    get_mcp_runtime,
    reset_mcp_runtime,
    set_mcp_runtime,
)
from agentkit.mcp.service import McpServerReindexStats
from agentkit.mcp.store import (
    McpServerStore,
    McpToolRecord,
    McpToolStore,
    call_name_for,
    tool_id_for,
)
from agentkit.mcp.sync import McpCatalogSync, McpServerFailure, McpSyncStats
from agentkit.persistence.base import Base
from agentkit.tools.search.service import (
    McpToolSearchService,
    get_tool_search_runtime,
    reset_tool_search_runtime,
    set_tool_search_runtime,
)

OWNER = "u1"
OTHER_OWNER = "u2"
URL = "https://mcp.example.test/mcp"
VECTOR_STORE = {"uri": "http://milvus.test:19530"}
EMBEDDING = {"base_url": "http://embed.test", "model": "bge-m3", "dimensions": 8}


@pytest.fixture(autouse=True)
def _unpublished_runtime() -> Any:
    reset_mcp_runtime()
    reset_tool_search_runtime()
    yield
    reset_mcp_runtime()
    reset_tool_search_runtime()


@pytest_asyncio.fixture
async def stores(tmp_path: Path) -> Any:
    """The two real stores over one SQLite database, exactly as the build wires them."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp_runtime.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield McpServerStore(factory), McpToolStore(factory)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeClient:
    """The client boundary: canned tools/list per server, recording each call."""

    def __init__(self) -> None:
        self.results: dict[str, list[mcp_types.Tool]] = {}
        self.errors: dict[str, BaseException] = {}
        self.connections: list[McpServerConnection] = []
        self.calls: list[tuple[McpServerConnection, str, dict[str, Any]]] = []
        self.call_error: BaseException | None = None
        self.closed = False
        #: Held open to keep a catalog pass in flight, so a test can prove what a
        #: concurrent delete does while that snapshot is still unwritten.
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def list_tools(self, connection: McpServerConnection) -> list[mcp_types.Tool]:
        self.connections.append(connection)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        error = self.errors.get(connection.server_id)
        if error is not None:
            raise error
        return list(self.results.get(connection.server_id, []))

    async def call_tool(
        self,
        connection: McpServerConnection,
        name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> mcp_types.CallToolResult:
        self.calls.append((connection, name, dict(arguments)))
        if self.call_error is not None:
            raise self.call_error
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="called " + name)],
            isError=False,
        )

    async def aclose(self) -> None:
        self.closed = True


class FakeIndex:
    """The derived index: records the ensure/delete calls the runtime makes."""

    def __init__(
        self,
        error: BaseException | None = None,
        *,
        ensure_error: BaseException | None = None,
    ) -> None:
        self.error = error
        self.ensure_error = ensure_error
        self.ensured: list[int | None] = []
        self.deleted: list[list[str]] = []
        self.closed = False
        self.synced: list[bool] = []

    async def ensure_collection(self, dimensions: int | None = None) -> None:
        self.ensured.append(dimensions)
        if self.ensure_error is not None:
            raise self.ensure_error

    async def delete_by_ids(self, tool_ids: Any) -> int:
        self.deleted.append(list(tool_ids))
        if self.error is not None:
            raise self.error
        return len(list(tool_ids))

    async def sync(self, records: Any, *, embedder: Any = None, full: bool = False) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeSync:
    """Stands in for `McpCatalogSync`: records the passes, never raises by accident.

    `max_active` is the concurrency high-water mark, which is what proves the
    process lock serialises two triggers instead of letting them overlap.
    """

    def __init__(
        self,
        *,
        stats: McpSyncStats | None = None,
        delay: float = 0.0,
        error: BaseException | None = None,
    ) -> None:
        self.stats = stats or McpSyncStats(scanned=1, servers_scanned=1, duration_ms=3)
        self.delay = delay
        self.error = error
        self.reasons: list[str] = []
        self.fulls: list[bool] = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def sync(self, reason: str = "manual", full: bool = False) -> McpSyncStats:
        self.reasons.append(reason)
        self.fulls.append(full)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return self.stats
        finally:
            self.active -= 1


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _config(**overrides: Any) -> McpServersConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "encryption_key": HeaderCipher.generate_key(),
    }
    base.update(overrides)
    return McpServersConfig(**base)


def _search(**overrides: Any) -> ToolSearchConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "vector_store": dict(VECTOR_STORE),
        "embedding": dict(EMBEDDING),
    }
    base.update(overrides)
    return ToolSearchConfig(**base)


def _mcp_tool(name: str = "search") -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=name + " tool",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


async def _server(
    server_store: McpServerStore,
    *,
    server_id: str = "s1",
    name: str = "anysearch",
    owner: str = OWNER,
    enabled: bool = True,
    reachable: bool = True,
    headers_encrypted: str | None = None,
    url: str = URL,
) -> McpServerRecord:
    """One stored server, left in the state the test needs."""
    created = await server_store.create(
        McpServerRecord(
            id=server_id,
            owner_user_id=owner,
            name=name,
            url=url,
            headers_encrypted=headers_encrypted,
            enabled=enabled,
        )
    )
    if reachable and enabled:
        await server_store.set_status(
            created.id, owner, status="active", reachable=True, last_error=None
        )
    return (await server_store.get(created.id, owner)) or created


def _record(
    *,
    server_id: str = "s1",
    server_name: str = "anysearch",
    owner: str = OWNER,
    name: str = "search",
    callable_: bool = True,
) -> McpToolRecord:
    return McpToolRecord(
        id=tool_id_for(server_id, name),
        server_id=server_id,
        server_name=server_name,
        owner_user_id=owner,
        name=name,
        call_name=call_name_for(server_name, name),
        description=name + " tool",
        input_schema={"type": "object"},
        content=name,
        callable=callable_,
    )


def _runtime(
    server_store: McpServerStore,
    tool_store: McpToolStore,
    *,
    client: Any = None,
    sync: Any = None,
    index: Any = None,
    embedder: Any = None,
    reranker: Any = None,
    config: McpServersConfig | None = None,
    tool_search: ToolSearchConfig | None = None,
    daily_delay: Any = None,
) -> McpToolRuntime:
    resolved_config = config or _config()
    cipher = HeaderCipher(resolved_config.resolved_encryption_key())
    resolved_client = client if client is not None else FakeClient()
    resolved_sync = (
        sync
        if sync is not None
        else McpCatalogSync(server_store, tool_store, resolved_client, cipher=cipher)
    )
    return McpToolRuntime(
        resolved_config,
        resolved_sync,
        server_store,
        tool_store,
        resolved_client,
        cipher,
        index=index,
        embedder=object() if embedder is None else embedder,
        reranker=reranker,
        tool_search=tool_search,
        daily_delay=daily_delay,
    )


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was never reached")
        await asyncio.sleep(0.005)


# --------------------------------------------------------------------------- #
# Lifecycle and the startup pass
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_syncs_the_catalog_and_status_counts_servers(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    live = await _server(server_store, server_id="s1", name="anysearch")
    await _server(
        server_store, server_id="s2", name="legacy", enabled=False, reachable=False
    )
    client = FakeClient()
    client.results[live.id] = [_mcp_tool("search")]
    runtime = _runtime(server_store, tool_store, client=client, daily_delay=lambda: 3600.0)

    await runtime.start()
    try:
        status = await runtime.status()

        assert status["servers_total"] == 2
        assert status["servers_active"] == 1
        assert status["servers_disabled"] == 1
        assert status["tools_total"] == 1
        assert status["indexed_tools"] == 1
        assert status["last_sync_at"] is not None
        # The pass stamped the server it read: that is what the management page
        # and tool_execute's own server check both read.
        stored = await server_store.get(live.id, OWNER)
        assert stored is not None
        assert (stored.status, stored.reachable) == ("active", True)
        assert stored.last_tools_sync_at is not None
        # A disabled server was never contacted, so its row is untouched.
        disabled = await server_store.get("s2", OWNER)
        assert disabled is not None
        assert (disabled.status, disabled.reachable) == ("disabled", False)
        assert client.connections[0].server_id == live.id
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent(stores: Any) -> None:
    server_store, tool_store = stores
    sync = FakeSync()
    runtime = _runtime(server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0)

    await runtime.start()
    try:
        await runtime.start()
        assert sync.reasons == ["startup"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stop_cancels_the_tasks_and_releases_the_index_and_client(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    client = FakeClient()
    index = FakeIndex()
    sync = FakeSync()
    runtime = _runtime(
        server_store,
        tool_store,
        client=client,
        sync=sync,
        index=index,
        daily_delay=lambda: 0.02,
    )
    set_mcp_runtime(runtime)
    set_tool_search_runtime(object())  # type: ignore[arg-type]

    await runtime.start()
    await asyncio.sleep(0.05)  # let the daily task start once
    await runtime.stop()

    assert client.closed is True
    assert index.closed is True
    assert get_mcp_runtime() is None
    assert get_tool_search_runtime() is None
    assert sync.reasons[:1] == ["startup"], "the daily task ran nothing after stop"

    # Stopping twice must stay harmless: app shutdown paths can overlap.
    await runtime.stop()


@pytest.mark.asyncio
async def test_stopping_an_old_runtime_does_not_clear_the_newer_one(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    old = _runtime(server_store, tool_store, daily_delay=lambda: 3600.0)
    new = _runtime(server_store, tool_store, daily_delay=lambda: 3600.0)
    set_mcp_runtime(new)  # the replacement is already published

    await old.start()
    await old.stop()

    assert get_mcp_runtime() is new


@pytest.mark.asyncio
async def test_a_disabled_runtime_never_touches_the_catalog(stores: Any) -> None:
    server_store, tool_store = stores
    sync = FakeSync()
    runtime = _runtime(
        server_store, tool_store, sync=sync, config=_config(enabled=False)
    )

    await runtime.start()
    await runtime.request_refresh("admin")
    assert await runtime.refresh_now("admin") == McpSyncStats()
    assert await runtime.reindex(OWNER) == McpServerReindexStats()
    status = await runtime.status()

    assert status["enabled"] is False
    assert status["sync"] == "disabled"
    assert sync.reasons == []
    await runtime.stop()


@pytest.mark.asyncio
async def test_the_daily_task_runs_a_full_pass(stores: Any) -> None:
    server_store, tool_store = stores
    delays = iter([0.01, 3600.0, 3600.0, 3600.0])
    sync = FakeSync()
    runtime = _runtime(
        server_store, tool_store, sync=sync, daily_delay=lambda: next(delays)
    )

    await runtime.start()
    try:
        await _wait_until(
            lambda: any(reason == "daily" for reason in sync.reasons), timeout=3
        )
        assert ("daily", True) in list(zip(sync.reasons, sync.fulls))
    finally:
        await runtime.stop()


def test_the_daily_delay_targets_the_next_local_midnight() -> None:
    """The schedule is local time, so the fixture is built in local time.

    A fixed +08:00 fixture would only pass on a +08:00 host: the helper normalises
    whatever it is given to the host's zone (that is the documented "local
    midnight" semantics), so under TZ=UTC that same fixture is 15:00 local and the
    delay is 32400s.  Building from `datetime.now().astimezone()` keeps the
    assertion true in every zone.
    """
    from datetime import datetime

    now = datetime.now().astimezone().replace(
        hour=23, minute=0, second=0, microsecond=0
    )

    assert seconds_until_local_midnight(now) == pytest.approx(3600.0)


def test_the_daily_delay_stays_within_one_day() -> None:
    assert 0.0 < seconds_until_local_midnight() <= 86400.0


def test_a_non_local_offset_is_normalised_to_the_host_zone() -> None:
    """An aware argument is interpreted in *its* zone, then scheduled locally."""
    from datetime import datetime, timedelta, timezone

    given = datetime(2026, 11, 8, 23, 0, tzinfo=timezone(timedelta(hours=8)))
    local = given.astimezone()
    # Independent arithmetic: a day minus the seconds already spent today.
    since_local_midnight = local.hour * 3600 + local.minute * 60 + local.second

    assert seconds_until_local_midnight(given) == pytest.approx(
        86400 - since_local_midnight
    )


# --------------------------------------------------------------------------- #
# Triggers: one pass, one lock
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_queued_triggers_become_one_pass(stores: Any) -> None:
    server_store, tool_store = stores
    sync = FakeSync()
    runtime = _runtime(server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0)

    await runtime.request_refresh("admin")
    await runtime.request_refresh("admin", )  # the worker has not started yet
    await _wait_until(lambda: sync.reasons, timeout=2)
    await asyncio.sleep(0.05)

    assert sync.reasons == ["admin"]


@pytest.mark.asyncio
async def test_triggers_during_a_pass_cause_exactly_one_more_pass(stores: Any) -> None:
    server_store, tool_store = stores
    sync = FakeSync()
    sync.gate = asyncio.Event()
    runtime = _runtime(server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0)

    await runtime.request_refresh("admin")
    await asyncio.wait_for(sync.started.wait(), timeout=2)
    await runtime.request_refresh("admin")
    await runtime.request_refresh("admin")

    sync.gate.set()
    await _wait_until(lambda: len(sync.reasons) >= 2, timeout=5)
    await asyncio.sleep(0.05)

    assert sync.reasons == ["admin", "admin"]


@pytest.mark.asyncio
async def test_concurrent_refreshes_are_serialised_by_the_process_lock(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    sync = FakeSync(delay=0.02)
    runtime = _runtime(server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0)

    await asyncio.gather(
        runtime.refresh_now("manual"), runtime.refresh_now("manual")
    )

    assert sync.max_active == 1, "two passes must never overlap"
    assert sync.reasons == ["manual", "manual"]


# --------------------------------------------------------------------------- #
# Per-target refresh results
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refresh_server_reports_only_that_servers_failure(stores: Any) -> None:
    server_store, tool_store = stores
    healthy = await _server(server_store, server_id="s1", name="good")
    broken = await _server(server_store, server_id="s2", name="bad")
    client = FakeClient()
    client.results[healthy.id] = [_mcp_tool("search")]
    client.errors[broken.id] = McpServerUnavailable("MCP server 'bad' is unavailable")
    runtime = _runtime(server_store, tool_store, client=client, daily_delay=lambda: 3600.0)

    failed = await runtime.refresh_server(broken.id)
    assert failed.error is not None
    assert failed.index_failed is False
    # The broken server's row carries the note and is no longer advertised.
    stamped = await server_store.get(broken.id, OWNER)
    assert stamped is not None
    assert (stamped.status, stamped.reachable) == ("error", False)
    assert stamped.last_error

    healthy_result = await runtime.refresh_server(healthy.id)
    assert healthy_result.error is None, (
        "another server's failure must not report this target as unrefreshed"
    )
    assert healthy_result.server_id == healthy.id


@pytest.mark.asyncio
async def test_an_index_only_failure_still_counts_as_refreshed(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="good")
    client = FakeClient()
    client.results[server.id] = [_mcp_tool("search")]
    sync = FakeSync(
        stats=McpSyncStats(
            error="MCP tool index sync failed (ToolIndexUnavailable)",
            index_failed=True,
            servers_scanned=1,
        )
    )
    runtime = _runtime(
        server_store, tool_store, client=client, sync=sync, daily_delay=lambda: 3600.0
    )

    result = await runtime.refresh_server(server.id)

    assert result.error
    assert result.index_failed is True


@pytest.mark.asyncio
async def test_an_aborted_pass_is_not_a_refresh(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="good")
    sync = FakeSync(error=RuntimeError("database is gone"))
    runtime = _runtime(
        server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0
    )

    result = await runtime.refresh_server(server.id)

    assert result.error == "RuntimeError"
    assert result.index_failed is False
    status = await runtime.status()
    assert status["sync"] == "degraded"
    assert status["last_error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_sync_server_stamps_the_servers_it_walked(stores: Any) -> None:
    server_store, tool_store = stores
    good = await _server(server_store, server_id="s1", name="good")
    bad = await _server(server_store, server_id="s2", name="bad")
    client = FakeClient()
    client.results[good.id] = [_mcp_tool("search")]
    client.errors[bad.id] = McpServerUnavailable("MCP server 'bad' is unavailable")
    runtime = _runtime(server_store, tool_store, client=client, daily_delay=lambda: 3600.0)

    result = await runtime.sync_server(good.id)

    assert isinstance(result, McpServerSyncResult)
    assert result.error is None
    stamped_good = await server_store.get(good.id, OWNER)
    stamped_bad = await server_store.get(bad.id, OWNER)
    assert stamped_good is not None and stamped_bad is not None
    assert (stamped_good.status, stamped_good.reachable) == ("active", True)
    assert (stamped_bad.status, stamped_bad.reachable) == ("error", False)


# --------------------------------------------------------------------------- #
# reindex and catalog removal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reindex_maps_the_sync_stats_to_the_service_shape(stores: Any) -> None:
    server_store, tool_store = stores
    sync = FakeSync(
        stats=McpSyncStats(
            scanned=3,
            servers_scanned=2,
            servers_failed=1,
            changed=2,
            upserted=2,
            deleted=1,
            duration_ms=7,
            error="MCP server 'bad': MCP server 'bad' is unavailable",
            failed_servers=(
                McpServerFailure(server_id="s2", owner_user_id=OWNER, error="boom"),
            ),
        )
    )
    runtime = _runtime(server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0)

    stats = await runtime.reindex(OWNER)

    assert (stats.scanned, stats.changed, stats.upserted, stats.deleted) == (3, 2, 2, 1)
    assert stats.failed == 1
    assert stats.duration_ms == 7
    assert stats.error
    assert sync.reasons == ["manual"]
    assert sync.fulls == [True], "a manual reindex is a full index rebuild"


@pytest.mark.asyncio
async def test_reindex_rejects_an_empty_owner(stores: Any) -> None:
    server_store, tool_store = stores
    runtime = _runtime(server_store, tool_store, daily_delay=lambda: 3600.0)

    with pytest.raises(McpServerValidationError):
        await runtime.reindex("  ")


@pytest.mark.asyncio
async def test_remove_server_catalog_deletes_rows_and_index_documents(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    record = _record(server_id=server.id, server_name=server.name)
    await tool_store.upsert_snapshot([record], {tool_id_for(server.id, record.name)})
    index = FakeIndex()
    runtime = _runtime(server_store, tool_store, index=index, daily_delay=lambda: 3600.0)

    deleted = await runtime.remove_server_catalog(server.id)

    assert deleted == (tool_id_for(server.id, "search"),)
    assert await tool_store.list_for_server(server.id, OWNER) == []
    assert index.deleted == [[tool_id_for(server.id, "search")]]


@pytest.mark.asyncio
async def test_an_index_cleanup_failure_does_not_undo_the_row_delete(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    record = _record(server_id=server.id, server_name=server.name)
    await tool_store.upsert_snapshot([record], {tool_id_for(server.id, record.name)})
    runtime = _runtime(
        server_store,
        tool_store,
        index=FakeIndex(error=RuntimeError("milvus down")),
        daily_delay=lambda: 3600.0,
    )

    deleted = await runtime.remove_server_catalog(server.id)

    assert len(deleted) == 1
    assert await tool_store.list_for_server(server.id, OWNER) == []


# --------------------------------------------------------------------------- #
# call_tool
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_call_tool_routes_to_the_owning_server_with_decrypted_headers(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    config = _config()
    cipher = HeaderCipher(config.resolved_encryption_key())
    server = await _server(
        server_store,
        server_id="s1",
        name="anysearch",
        headers_encrypted=cipher.encrypt({"Authorization": "Bearer secret"}),
    )
    record = _record(server_id=server.id, server_name=server.name)
    client = FakeClient()
    runtime = _runtime(server_store, tool_store, client=client, config=config)

    result = await runtime.call_tool(record, {"query": "x"})

    assert result.isError is False
    connection, name, arguments = client.calls[0]
    assert connection.server_id == server.id
    assert connection.url == server.url
    assert dict(connection.headers) == {"Authorization": "Bearer secret"}
    assert connection.request_timeout_seconds == config.request_timeout_seconds
    assert connection.connect_timeout_seconds == config.connect_timeout_seconds
    # The upstream server gets the bare tool name, never the mcp__ call name.
    assert name == "search"
    assert arguments == {"query": "x"}


@pytest.mark.asyncio
async def test_call_tool_refuses_another_owners_server(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    record = _record(
        server_id=server.id, server_name=server.name, owner=OTHER_OWNER
    )
    client = FakeClient()
    runtime = _runtime(server_store, tool_store, client=client)

    with pytest.raises(McpServerNotFound):
        await runtime.call_tool(record, {})

    assert client.calls == []


@pytest.mark.asyncio
async def test_call_tool_refuses_a_missing_server_row(stores: Any) -> None:
    server_store, tool_store = stores
    runtime = _runtime(server_store, tool_store)

    with pytest.raises(McpServerNotFound):
        await runtime.call_tool(_record(server_id="gone"), {})


@pytest.mark.asyncio
async def test_call_tool_refuses_a_disabled_server(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    await server_store.set_enabled(server.id, OWNER, False)
    client = FakeClient()
    runtime = _runtime(server_store, tool_store, client=client)

    with pytest.raises(McpServerUnavailable) as excinfo:
        await runtime.call_tool(_record(server_id=server.id), {})

    assert "disabled" in str(excinfo.value)
    assert client.calls == []


@pytest.mark.asyncio
async def test_call_tool_refuses_an_unreachable_server(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    await server_store.set_status(
        server.id, OWNER, status="error", reachable=False, last_error="connect timed out"
    )
    client = FakeClient()
    runtime = _runtime(server_store, tool_store, client=client)

    with pytest.raises(McpServerUnavailable) as excinfo:
        await runtime.call_tool(_record(server_id=server.id), {})

    assert "unreachable" in str(excinfo.value)
    assert client.calls == []


@pytest.mark.asyncio
async def test_call_tool_refuses_a_row_the_store_no_longer_calls_callable(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    client = FakeClient()
    runtime = _runtime(server_store, tool_store, client=client)

    with pytest.raises(McpToolError):
        await runtime.call_tool(_record(server_id=server.id, callable_=False), {})

    assert client.calls == []


@pytest.mark.asyncio
async def test_undecryptable_headers_are_a_configuration_error(stores: Any) -> None:
    server_store, tool_store = stores
    other = HeaderCipher(HeaderCipher.generate_key())
    server = await _server(
        server_store,
        server_id="s1",
        name="anysearch",
        headers_encrypted=other.encrypt({"Authorization": "Bearer secret"}),
    )
    client = FakeClient()
    runtime = _runtime(server_store, tool_store, client=client)

    with pytest.raises(McpServerConfigurationError) as excinfo:
        await runtime.call_tool(_record(server_id=server.id), {})

    assert "secret" not in str(excinfo.value)
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Status scoping and degradation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_counts_are_scoped_to_the_caller(stores: Any) -> None:
    server_store, tool_store = stores
    mine = await _server(server_store, server_id="s1", name="mine", owner=OWNER)
    theirs = await _server(
        server_store, server_id="s2", name="theirs", owner=OTHER_OWNER
    )
    await tool_store.upsert_snapshot(
        [
            _record(server_id=mine.id, server_name=mine.name, owner=OWNER),
            _record(
                server_id=theirs.id,
                server_name=theirs.name,
                owner=OTHER_OWNER,
                name="search",
            ),
        ],
        {
            tool_id_for(mine.id, "search"),
            tool_id_for(theirs.id, "search"),
        },
    )
    runtime = _runtime(server_store, tool_store, daily_delay=lambda: 3600.0)

    scoped = await runtime.status(OWNER)
    catalog_wide = await runtime.status()

    assert (scoped["servers_total"], scoped["indexed_tools"]) == (1, 1)
    assert (catalog_wide["servers_total"], catalog_wide["indexed_tools"]) == (2, 2)


@pytest.mark.asyncio
async def test_a_scoped_status_omits_the_process_global_retry(stores: Any) -> None:
    server_store, tool_store = stores
    runtime = _runtime(server_store, tool_store, daily_delay=lambda: 3600.0)
    runtime.record_retry(tool_name="mcp__anysearch__search", result="success")

    scoped = await runtime.status(OWNER)
    catalog_wide = await runtime.status()

    assert "last_retry_tool" not in scoped
    assert catalog_wide["last_retry_tool"] == "mcp__anysearch__search"
    assert catalog_wide["last_retry_result"] == "success"


@pytest.mark.asyncio
async def test_status_degrades_instead_of_raising_when_the_stores_fail(
    stores: Any,
) -> None:
    class BrokenStore:
        async def list_all(self, owner_user_id: Any = None) -> Any:
            raise RuntimeError("db down")

        async def list_for_owner(self, owner_user_id: str) -> Any:
            raise RuntimeError("db down")

    runtime = McpToolRuntime(
        _config(),
        FakeSync(),
        BrokenStore(),  # type: ignore[arg-type]
        BrokenStore(),  # type: ignore[arg-type]
        FakeClient(),
        HeaderCipher(HeaderCipher.generate_key()),
    )

    status = await runtime.status(OWNER)

    assert (status["servers_total"], status["tools_total"]) == (0, 0)
    assert status["index"] == "degraded"


@pytest.mark.asyncio
async def test_the_retrieval_policy_comes_from_the_tool_search_block(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    loose = _runtime(
        server_store, tool_store, tool_search=ToolSearchConfig(require_retrieved=False)
    )
    strict = _runtime(server_store, tool_store)

    assert loose.require_retrieved is False
    assert strict.require_retrieved is True, "an unreadable policy is not permission"
    assert loose.retry_on_tool_error is True
    assert loose.store is loose.tool_store


# --------------------------------------------------------------------------- #
# Building the process runtime
# --------------------------------------------------------------------------- #


def test_building_a_disabled_runtime_publishes_nothing() -> None:
    set_mcp_runtime(object())  # type: ignore[arg-type]
    set_tool_search_runtime(object())  # type: ignore[arg-type]

    assert build_mcp_runtime(McpServersConfig()) is None
    assert get_mcp_runtime() is None
    assert get_tool_search_runtime() is None


def test_an_enabled_build_without_a_key_fails_before_anything_is_published() -> None:
    with pytest.raises(ValueError, match="encryption_key"):
        build_mcp_runtime(McpServersConfig(enabled=True))

    assert get_mcp_runtime() is None
    assert get_tool_search_runtime() is None


def test_an_enabled_build_with_an_invalid_key_fails_too() -> None:
    with pytest.raises(ValueError, match="Fernet"):
        build_mcp_runtime(McpServersConfig(enabled=True, encryption_key="not-a-key"))


@pytest.mark.asyncio
async def test_building_an_enabled_runtime_publishes_it_and_the_search_service(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp_build.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = build_mcp_runtime(_config(), factory, tool_search=_search())
    assert runtime is not None
    try:
        assert get_mcp_runtime() is runtime
        service = get_tool_search_runtime()
        assert isinstance(service, McpToolSearchService)
        status = await runtime.status()
        assert (status["servers_total"], status["indexed_tools"]) == (0, 0)
    finally:
        reset_mcp_runtime()
        reset_tool_search_runtime()
        if runtime is not None:
            await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_disabled_tool_search_block_publishes_no_service(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp_nosearch.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = build_mcp_runtime(
        _config(), factory, tool_search=ToolSearchConfig(enabled=False)
    )
    assert runtime is not None
    try:
        assert get_mcp_runtime() is runtime
        assert get_tool_search_runtime() is None
        assert runtime.index is None
    finally:
        reset_mcp_runtime()
        reset_tool_search_runtime()
        if runtime is not None:
            await runtime.stop()
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Review round 1: scoped errors, the delete race, degradation, shutdown
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_scoped_status_never_reports_another_users_error(stores: Any) -> None:
    """The catalog-wide error names another user's server, so a scoped read drops it.

    /api/mcp-servers/status is a per-user surface: if it echoed the process-wide
    last error, every caller would learn that some other user's server exists and
    why it failed.
    """
    server_store, tool_store = stores
    mine = await _server(server_store, server_id="s1", name="mine", owner=OWNER)
    theirs = await _server(
        server_store, server_id="s2", name="theirs", owner=OTHER_OWNER
    )
    sync = FakeSync(
        stats=McpSyncStats(
            error="MCP server 'theirs': MCP server 'theirs' is unavailable",
            servers_scanned=2,
            servers_failed=1,
        )
    )
    runtime = _runtime(server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0)
    await runtime.refresh_now("manual")  # the pass records the process-wide error

    scoped = await runtime.status(OWNER)
    other = await runtime.status(OTHER_OWNER)
    catalog_wide = await runtime.status()

    assert scoped["last_error"] is None, (
        "a scoped caller must not inherit the catalog-wide error"
    )
    assert scoped["servers_total"] == 1
    assert catalog_wide["last_error"] == (
        "MCP server 'theirs': MCP server 'theirs' is unavailable"
    )
    # A caller whose own row carries a note still sees that note.
    await server_store.set_status(
        theirs.id, OTHER_OWNER, status="error", reachable=False, last_error="theirs: boom"
    )
    own_error = await runtime.status(OTHER_OWNER)
    assert own_error["last_error"] == "theirs: boom"
    assert (await runtime.status(OWNER))["last_error"] is None
    assert mine.id != theirs.id


@pytest.mark.asyncio
async def test_a_delete_waits_for_the_in_flight_pass_so_rows_stay_deleted(
    stores: Any,
) -> None:
    """The delete and the catalog pass share one lock: no resurrected rows.

    A pass holds the server's rows in the snapshot it is about to write, so a
    delete that ran while it was in flight would be undone at commit time.  With
    the lock the pass commits first and the delete removes what it wrote.
    """
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    record = _record(server_id=server.id, server_name=server.name)
    await tool_store.upsert_snapshot([record], {tool_id_for(record.server_id, "search")})
    client = FakeClient()
    client.results[server.id] = [_mcp_tool("search")]
    client.gate = asyncio.Event()
    index = FakeIndex()
    runtime = _runtime(
        server_store, tool_store, client=client, index=index, daily_delay=lambda: 3600.0
    )

    pass_task = asyncio.create_task(runtime.refresh_now("manual"))
    await asyncio.wait_for(client.entered.wait(), timeout=2)
    delete_task = asyncio.create_task(runtime.remove_server_catalog(server.id))
    await asyncio.sleep(0.02)
    assert not delete_task.done(), "the delete must wait for the running pass"

    client.gate.set()
    await asyncio.wait_for(asyncio.gather(pass_task, delete_task), timeout=5)

    assert await tool_store.list_for_server(server.id, OWNER) == [], (
        "the delete ran after the pass, so nothing was resurrected"
    )
    assert index.deleted and index.deleted[-1] == [tool_id_for(server.id, "search")]


@pytest.mark.asyncio
async def test_a_transport_failure_degrades_the_owning_server(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    client = FakeClient()
    client.call_error = McpServerTimeout(
        "MCP server 'anysearch' timed out during tools/call"
    )
    runtime = _runtime(server_store, tool_store, client=client)

    with pytest.raises(McpServerTimeout):
        await runtime.call_tool(_record(server_id=server.id), {"query": "x"})

    stored = await server_store.get(server.id, OWNER)
    assert stored is not None
    assert (stored.status, stored.reachable) == ("error", False)
    assert stored.last_error
    assert "timed out" in stored.last_error


@pytest.mark.asyncio
async def test_a_configuration_failure_degrades_the_owning_server(stores: Any) -> None:
    server_store, tool_store = stores
    other_key = HeaderCipher(HeaderCipher.generate_key())
    server = await _server(
        server_store,
        server_id="s1",
        name="anysearch",
        headers_encrypted=other_key.encrypt({"Authorization": "Bearer secret"}),
    )
    runtime = _runtime(server_store, tool_store)

    with pytest.raises(McpServerConfigurationError):
        await runtime.call_tool(_record(server_id=server.id), {})

    stored = await server_store.get(server.id, OWNER)
    assert stored is not None
    assert (stored.status, stored.reachable) == ("error", False)
    assert stored.last_error is not None
    assert "secret" not in stored.last_error


@pytest.mark.asyncio
async def test_a_tool_error_does_not_degrade_the_server(stores: Any) -> None:
    """The server answered; only that call failed, so the row stays usable."""
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    client = FakeClient()
    client.call_error = McpToolError(
        "MCP server 'anysearch' rejected the tool call (JSON-RPC error -32000)"
    )
    runtime = _runtime(server_store, tool_store, client=client)

    with pytest.raises(McpToolError):
        await runtime.call_tool(_record(server_id=server.id), {})

    stored = await server_store.get(server.id, OWNER)
    assert stored is not None
    assert (stored.status, stored.reachable) == ("active", True)
    assert stored.last_error is None


class _CloseableResource:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_stop_closes_the_whole_retrieval_stack(stores: Any) -> None:
    server_store, tool_store = stores
    client = FakeClient()
    index = FakeIndex()
    embedder = _CloseableResource()
    reranker = _CloseableResource()
    runtime = _runtime(
        server_store,
        tool_store,
        client=client,
        index=index,
        embedder=embedder,
        reranker=reranker,
        daily_delay=lambda: 3600.0,
    )

    await runtime.start()
    await runtime.stop()

    assert index.closed is True
    assert embedder.closed is True
    assert reranker.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_a_stopped_runtime_refuses_new_passes(stores: Any) -> None:
    server_store, tool_store = stores
    server = await _server(server_store, server_id="s1", name="anysearch")
    sync = FakeSync()
    runtime = _runtime(
        server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0
    )

    await runtime.start()
    await runtime.stop()

    await runtime.request_refresh("admin")
    assert await runtime.refresh_now("admin") == McpSyncStats()
    result = await runtime.refresh_server(server.id)

    assert sync.reasons == ["startup"], "no pass may start after stop()"
    assert result.error == "mcp_stopped"
    assert result.index_failed is False


@pytest.mark.asyncio
async def test_a_server_created_during_a_pass_is_not_stamped_by_it(stores: Any) -> None:
    """A row that appeared mid-pass was never contacted, so it keeps its own state."""
    server_store, tool_store = stores
    first = await _server(server_store, server_id="s1", name="first")
    sync = FakeSync()
    sync.gate = asyncio.Event()
    runtime = _runtime(
        server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0
    )

    pass_task = asyncio.create_task(runtime.refresh_now("manual"))
    await asyncio.wait_for(sync.started.wait(), timeout=2)
    # reachable=False keeps the helper from stamping it active itself: a freshly
    # created row is pending until some pass actually contacts it.
    late = await _server(server_store, server_id="s2", name="late", reachable=False)
    sync.gate.set()
    await asyncio.wait_for(pass_task, timeout=5)

    stamped = await server_store.get(first.id, OWNER)
    late_row = await server_store.get(late.id, OWNER)
    assert stamped is not None and late_row is not None
    assert (stamped.status, stamped.reachable) == ("active", True)
    assert (late_row.status, late_row.reachable) == ("pending", False), (
        "a server that appeared after the snapshot must not be claimed active"
    )


@pytest.mark.asyncio
async def test_a_failed_retrieval_build_reports_degraded_not_disabled(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import agentkit.knowledge.embedding as embedding_module

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no embedder today")

    monkeypatch.setattr(embedding_module, "OpenAICompatibleEmbedder", _boom)
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp_degraded.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = build_mcp_runtime(_config(), factory, tool_search=_search())
    assert runtime is not None
    try:
        assert runtime.index is None
        status = await runtime.status()
        assert status["index"] == "degraded", (
            "the search block is on, so a missing index is breakage, not a switch"
        )
    finally:
        reset_mcp_runtime()
        reset_tool_search_runtime()
        await runtime.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_startup_index_failure_is_not_cleared_by_a_later_pass(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    index = FakeIndex(ensure_error=RuntimeError("milvus is down"))
    sync = FakeSync()
    runtime = _runtime(
        server_store, tool_store, sync=sync, index=index, daily_delay=lambda: 3600.0
    )

    await runtime.start()
    try:
        assert (await runtime.status())["index"] == "degraded"

        await runtime.refresh_now("manual")  # a clean pass, but no recovered check
        status = await runtime.status()
        assert status["index"] == "degraded", (
            "a pass reporting no error is not evidence the index recovered"
        )
        assert status["sync"] == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_later_pass_that_fixes_the_index_clears_the_degradation(
    stores: Any,
) -> None:
    server_store, tool_store = stores
    index = FakeIndex(ensure_error=RuntimeError("milvus is down"))
    sync = FakeSync()
    runtime = _runtime(
        server_store, tool_store, sync=sync, index=index, daily_delay=lambda: 3600.0
    )

    await runtime.start()
    try:
        assert (await runtime.status())["index"] == "degraded"
        index.ensure_error = None  # Milvus came back
        await runtime.refresh_now("manual")
        assert (await runtime.status())["index"] == "ready"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_server_disabled_during_a_pass_stays_disabled(stores: Any) -> None:
    """A concurrent disable wins over the pass's snapshot.

    The pass read enabled=True before the sync; the user switched the server off
    while it was in flight.  Stamping the pre-pass copy would flip the row back to
    active/reachable and silently undo a management write, so the stamp reads the
    rows *after* the pass and leaves a disabled row exactly as the write left it.
    """
    server_store, tool_store = stores
    # Starts enabled but not yet reachable, so a wrong stamp would show up as
    # active/reachable=True instead of being hidden by the fixture itself.
    server = await _server(
        server_store, server_id="s1", name="anysearch", reachable=False
    )
    sync = FakeSync()
    sync.gate = asyncio.Event()
    runtime = _runtime(
        server_store, tool_store, sync=sync, daily_delay=lambda: 3600.0
    )

    pass_task = asyncio.create_task(runtime.refresh_now("manual"))
    await asyncio.wait_for(sync.started.wait(), timeout=2)
    disabled = await server_store.set_enabled(server.id, OWNER, False)
    assert disabled is not None and disabled.status == "disabled"
    sync.gate.set()
    await asyncio.wait_for(pass_task, timeout=5)

    stored = await server_store.get(server.id, OWNER)
    assert stored is not None
    assert (stored.status, stored.reachable) == ("disabled", False), (
        "the disable happened during the pass and must survive it"
    )
    assert stored.last_tools_sync_at is None
