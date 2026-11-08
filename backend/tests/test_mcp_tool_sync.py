"""MCP tool discovery sync: one full-catalog snapshot per sync, per-server isolation.

A sync is the only writer of `mcp_tools`, and Task 4's store makes it a *full* snapshot
writer: every row it does not rewrite loses callability.  That turns three otherwise
local details into global correctness rules, and these tests pin them:

* **one snapshot, every server.**  A sync reads every server (all owners) and calls
  `upsert_snapshot` exactly once.  A per-server snapshot would retire the other
  servers' tools, so the tests assert the single call and the complete record list;
* **failure is per server, not fatal.**  An unreachable server keeps its tools (the
  definition is data) but they stop being callable, while every other server still
  syncs and is published;
* **a disabled server is never contacted.**  Its rows are carried unseen, so they stay
  in the database and drop out of tool_search/tool_execute;
* **tools inherit their server's owner.**  A stored row that belongs to somebody else
  is never rewritten (`upsert_snapshot` would hand it to the wrong user) - it is
  skipped and reported as a conflict;
* **the database commits before the index.**  A Milvus failure sets `index_failed` and
  leaves the committed catalog alone;
* **failures are sanitized.**  A stored error names the server slug and the exception
  class; a credential, a bearer token or a URL with a query string never reaches it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mcp import types as mcp_types
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.mcp import net
from agentkit.mcp.catalog import (
    DEFAULT_MAX_DOCUMENT_CHARS,
    build_tool_content,
    build_tool_document,
    parameter_names,
)
from agentkit.mcp.client import McpServerConnection
from agentkit.mcp.crypto import HeaderCipher
from agentkit.mcp.errors import (
    McpProtocolError,
    McpServerUnavailable,
    sanitize_error_message,
)
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.store import (
    McpServerStore,
    McpToolRecord,
    McpToolStore,
    call_name_for,
    tool_id_for,
)
from agentkit.mcp.sync import McpCatalogSync, McpServerFailure, McpSyncStats
from agentkit.persistence.base import Base

TABLES = ("mcp_servers", "mcp_tools")
OWNER = "u1"
OTHER_OWNER = "u2"
URL = "https://mcp.example.test/mcp"


# --------------------------------------------------------------------------- #
# Fakes: the client, the vector index, and a recording tool store
# --------------------------------------------------------------------------- #


class FakeClient:
    """The client boundary: canned tools/list results or a failure, per server."""

    def __init__(self) -> None:
        self.results: dict[str, list[mcp_types.Tool]] = {}
        self.errors: dict[str, BaseException] = {}
        self.connections: list[McpServerConnection] = []

    @property
    def called(self) -> list[str]:
        """The server ids the sync actually opened a connection for."""
        return [connection.server_id for connection in self.connections]

    async def list_tools(
        self, connection: McpServerConnection
    ) -> list[mcp_types.Tool]:
        self.connections.append(connection)
        error = self.errors.get(connection.server_id)
        if error is not None:
            raise error
        return list(self.results.get(connection.server_id, []))


class FakeIndex:
    """The vector index: records what it was asked to sync, optionally fails."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def sync(
        self,
        records: Sequence[McpToolRecord],
        *,
        embedder: Any = None,
        full: bool = False,
    ) -> None:
        self.calls.append(
            {"records": list(records), "embedder": embedder, "full": full}
        )
        if self.error is not None:
            raise self.error


class RecordingToolStore:
    """A tool store that records every snapshot the sync asks it to write.

    The real store is still the writer - the wrapper only exposes *how* it was
    called, which is what proves the sync never writes a per-server snapshot and
    which rows a filtered pass handed over as preserved.
    """

    def __init__(self, store: McpToolStore) -> None:
        self.store = store
        self.snapshots: list[tuple[list[McpToolRecord], set[str]]] = []
        self.preserved: list[set[str]] = []

    async def list_all(self, owner_user_id: str | None = None):
        return await self.store.list_all(owner_user_id)

    async def list_for_server(self, server_id: str, owner_user_id: str):
        return await self.store.list_for_server(server_id, owner_user_id)

    async def upsert_snapshot(self, records, seen_ids, preserve_ids=None):
        self.snapshots.append((list(records), set(seen_ids)))
        self.preserved.append(set(preserve_ids or ()))
        return await self.store.upsert_snapshot(
            records, seen_ids, preserve_ids=preserve_ids
        )


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def server_record(
    name: str = "good",
    *,
    owner: str = OWNER,
    enabled: bool = True,
    url: str = URL,
    headers_encrypted: str | None = None,
) -> McpServerRecord:
    """The input shape `McpServerStore.create` accepts."""
    return McpServerRecord(
        owner_user_id=owner,
        name=name,
        url=url,
        headers_encrypted=headers_encrypted,
        enabled=enabled,
    )


def mcp_tool(
    name: str = "search",
    *,
    description: str | None = None,
    title: str | None = None,
    schema: dict[str, Any] | None = None,
    annotations: mcp_types.ToolAnnotations | None = None,
) -> mcp_types.Tool:
    """What `tools/list` actually returns for one tool."""
    return mcp_types.Tool(
        name=name,
        title=title,
        description=description if description is not None else name + " tool",
        inputSchema=(
            schema
            if schema is not None
            else {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        ),
        annotations=annotations,
    )


def prior_tool(
    server: McpServerRecord,
    name: str,
    *,
    owner: str | None = None,
) -> McpToolRecord:
    """A row a previous snapshot left behind, for seeding the store directly."""
    return McpToolRecord(
        server_id=server.id,
        server_name=server.name,
        owner_user_id=owner or server.owner_user_id,
        name=name,
        call_name=call_name_for(server.name, name),
        description=name + " tool",
        content=name + " " + server.name,
    )


async def seed(tool_store: McpToolStore, *records: McpToolRecord) -> None:
    """Store rows as callable, the way a successful earlier sync would have."""
    await tool_store.upsert_snapshot(
        list(records),
        seen_ids={tool_id_for(record.server_id, record.name) for record in records},
    )


# --------------------------------------------------------------------------- #
# Fixtures: the real stores over SQLite, the fake client, the sync under test
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp_sync.db").as_posix()
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Base.metadata.tables[table] for table in TABLES],
            )
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def server_store(session_factory) -> McpServerStore:
    return McpServerStore(session_factory)


@pytest.fixture
def tool_store(session_factory) -> McpToolStore:
    return McpToolStore(session_factory)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def sync(server_store, tool_store, client) -> McpCatalogSync:
    return McpCatalogSync(server_store, tool_store, client)


# --------------------------------------------------------------------------- #
# Failure isolation and full-catalog semantics
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_one_failed_server_does_not_hide_the_other(
    server_store, tool_store, client, sync
) -> None:
    good = await server_store.create(server_record("good"))
    bad = await server_store.create(server_record("bad"))
    client.results[good.id] = [mcp_tool("search")]
    client.errors[bad.id] = McpServerUnavailable("down")
    await seed(tool_store, prior_tool(bad, "old"))

    stats = await sync.sync("test")

    assert stats.servers_scanned == 2
    assert stats.servers_failed == 1
    # One good tool plus one carried row: the snapshot covers both servers.
    assert stats.scanned == 2
    good_row = await tool_store.get_by_call_name(
        "mcp__good__search", good.owner_user_id
    )
    bad_row = await tool_store.get_by_call_name("mcp__bad__old", bad.owner_user_id)
    assert good_row is not None and good_row.callable is True
    assert bad_row is not None and bad_row.callable is False
    assert bad_row.reachable is False
    assert stats.error is not None
    assert "bad" in stats.error and "down" in stats.error


@pytest.mark.asyncio
async def test_same_tool_name_on_two_servers_gets_distinct_call_names(
    server_store, tool_store, client, sync
) -> None:
    alpha = await server_store.create(server_record("alpha"))
    beta = await server_store.create(server_record("beta"))
    client.results[alpha.id] = [mcp_tool("search", description="alpha search")]
    client.results[beta.id] = [mcp_tool("search", description="beta search")]

    stats = await sync.sync("test")

    one = await tool_store.get_by_call_name("mcp__alpha__search", OWNER)
    two = await tool_store.get_by_call_name("mcp__beta__search", OWNER)
    assert one is not None and two is not None
    assert one.call_name == "mcp__alpha__search"
    assert two.call_name == "mcp__beta__search"
    assert one.id != two.id
    assert one.callable is True and two.callable is True
    # The retrieval text names the owning server, so BM25 can tell the twins apart.
    assert "server: alpha" in one.content
    assert "server: beta" in two.content
    assert stats.index_failed is False


@pytest.mark.asyncio
async def test_disabled_server_carries_its_rows_without_listing_them(
    server_store, tool_store, client, sync
) -> None:
    off = await server_store.create(server_record("off", enabled=False))
    await seed(tool_store, prior_tool(off, "search"))
    seeded = await tool_store.get_by_call_name("mcp__off__search", OWNER)
    assert seeded is not None and seeded.callable is True

    stats = await sync.sync("test")

    assert client.called == []
    assert stats.servers_scanned == 1
    assert stats.servers_failed == 0
    row = await tool_store.get_by_call_name("mcp__off__search", OWNER)
    assert row is not None
    assert row.callable is False
    assert row.enabled is False
    assert row.reachable is False


@pytest.mark.asyncio
async def test_a_failed_server_keeps_its_tools_while_another_server_updates(
    server_store, tool_store, client
) -> None:
    alpha = await server_store.create(server_record("alpha"))
    beta = await server_store.create(server_record("beta"))
    recorder = RecordingToolStore(tool_store)
    catalog_sync = McpCatalogSync(server_store, recorder, client)

    client.results[alpha.id] = [mcp_tool("search")]
    client.results[beta.id] = [mcp_tool("fetch"), mcp_tool("query")]
    await catalog_sync.sync("first")
    assert (await tool_store.get_by_call_name("mcp__beta__fetch", OWNER)).callable

    # Round two: alpha grows a tool, beta is down.
    client.results[alpha.id] = [mcp_tool("search"), mcp_tool("describe")]
    client.errors[beta.id] = McpServerUnavailable("down")
    stats = await catalog_sync.sync("second")

    assert stats.servers_failed == 1
    describe = await tool_store.get_by_call_name("mcp__alpha__describe", OWNER)
    assert describe is not None and describe.callable is True
    beta_fetch = await tool_store.get_by_call_name("mcp__beta__fetch", OWNER)
    assert beta_fetch is not None
    assert beta_fetch.callable is False
    assert beta_fetch.description == "fetch tool"
    # Only the new tool was published, and only beta's two rows were retired.
    assert stats.upserted == 1
    assert stats.deleted == 2
    # Both syncs wrote exactly one snapshot, and it covered every server.
    assert len(recorder.snapshots) == 2
    records, seen = recorder.snapshots[-1]
    assert {record.server_id for record in records} == {alpha.id, beta.id}
    assert seen == {
        tool_id_for(alpha.id, "search"),
        tool_id_for(alpha.id, "describe"),
    }
    assert stats.seen == 2
    assert [failure.server_id for failure in stats.failed_servers] == [beta.id]


@pytest.mark.asyncio
async def test_a_tool_stored_for_another_owner_is_skipped_and_reported(
    server_store, tool_store, client
) -> None:
    shared = await server_store.create(server_record("shared"))
    client.results[shared.id] = [mcp_tool("search"), mcp_tool("list")]
    recorder = RecordingToolStore(tool_store)
    catalog_sync = McpCatalogSync(server_store, recorder, client)
    await seed(tool_store, prior_tool(shared, "search", owner=OTHER_OWNER))

    stats = await catalog_sync.sync("test")

    assert stats.conflicts == ("mcp__shared__search",)
    stranger = await tool_store.get_by_call_name("mcp__shared__search", OTHER_OWNER)
    assert stranger is not None
    assert stranger.owner_user_id == OTHER_OWNER
    assert stranger.callable is False
    assert await tool_store.get_by_call_name("mcp__shared__search", OWNER) is None
    # The rest of that server still syncs: one bad row is not a bad server.
    listed = await tool_store.get_by_call_name("mcp__shared__list", OWNER)
    assert listed is not None and listed.callable is True
    assert tool_id_for(shared.id, "search") not in recorder.snapshots[-1][1]


@pytest.mark.asyncio
async def test_an_empty_server_list_writes_one_empty_snapshot(
    server_store, tool_store, client
) -> None:
    recorder = RecordingToolStore(tool_store)
    catalog_sync = McpCatalogSync(server_store, recorder, client)

    stats = await catalog_sync.sync("test")

    assert stats.servers_scanned == 0
    assert stats.servers_failed == 0
    assert stats.scanned == 0
    assert stats.upserted == 0
    assert stats.deleted == 0
    assert stats.error is None
    assert stats.index_failed is False
    assert recorder.snapshots == [([], set())]
    assert client.called == []


@pytest.mark.asyncio
async def test_two_owners_with_the_same_slug_are_reported_separately(
    server_store, tool_store, client, sync
) -> None:
    mine = await server_store.create(server_record("anysearch"))
    theirs = await server_store.create(server_record("anysearch", owner=OTHER_OWNER))
    client.errors[mine.id] = McpServerUnavailable("down")
    client.errors[theirs.id] = McpServerUnavailable("timed out")

    stats = await sync.sync("test")

    assert stats.servers_failed == 2
    # A slug is unique per owner, never globally: the id is what the runtime stamps.
    assert {
        (failure.server_id, failure.owner_user_id) for failure in stats.failed_servers
    } == {(mine.id, OWNER), (theirs.id, OTHER_OWNER)}
    assert all("anysearch" in failure.error for failure in stats.failed_servers)
    assert stats.error is not None and stats.error.count("anysearch") == 2


@pytest.mark.asyncio
async def test_a_failed_server_without_prior_tools_is_reported(
    server_store, tool_store, client, sync
) -> None:
    server = await server_store.create(server_record("empty"))
    client.errors[server.id] = McpServerUnavailable("down")

    stats = await sync.sync("test")

    assert stats.servers_failed == 1
    assert stats.scanned == 0
    # Nothing was carried, so no tool row could name the server: the outcome must.
    assert len(stats.failed_servers) == 1
    failure = stats.failed_servers[0]
    assert failure.server_id == server.id
    assert failure.owner_user_id == OWNER
    assert "empty" in failure.error and "down" in failure.error
    assert failure.error == stats.error
    assert await tool_store.list_all() == []


@pytest.mark.asyncio
async def test_a_malformed_upstream_tool_is_skipped_but_reported(
    server_store, tool_store, client, sync
) -> None:
    server = await server_store.create(server_record("odd"))
    client.results[server.id] = [mcp_tool("x" * 300), mcp_tool("search")]

    stats = await sync.sync("test")

    # A name past its identity column is dropped, not stored truncated, and the rest
    # of the server still publishes.
    assert stats.skipped == 1
    assert stats.seen == 2
    assert stats.servers_failed == 0
    assert stats.failed_servers == ()
    listed = await tool_store.get_by_call_name("mcp__odd__search", OWNER)
    assert listed is not None and listed.callable is True


@pytest.mark.asyncio
async def test_a_row_of_a_vanished_server_is_retired(
    server_store, tool_store, client, sync
) -> None:
    gone = await server_store.create(server_record("gone"))
    await seed(tool_store, prior_tool(gone, "search"))
    await server_store.delete(gone.id, OWNER)

    stats = await sync.sync("test")

    row = await tool_store.get_by_call_name("mcp__gone__search", OWNER)
    assert row is not None and row.callable is False
    assert stats.servers_scanned == 0
    assert stats.deleted == 1


@pytest.mark.asyncio
async def test_duration_is_measured_with_the_monotonic_clock(
    server_store, tool_store, client
) -> None:
    ticks = iter([10.0, 10.25])
    catalog_sync = McpCatalogSync(
        server_store, tool_store, client, clock=lambda: next(ticks)
    )

    stats = await catalog_sync.sync("test")

    assert stats.duration_ms == 250


# --------------------------------------------------------------------------- #
# Connection building: decryption, and never contacting a server we cannot auth
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_connection_carries_the_decrypted_headers(
    server_store, tool_store, client
) -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())
    token = cipher.encrypt({"Authorization": "Bearer s3cr3t"})
    assert token is not None
    server = await server_store.create(
        server_record("secure", headers_encrypted=token)
    )
    client.results[server.id] = [mcp_tool("search")]
    catalog_sync = McpCatalogSync(server_store, tool_store, client, cipher=cipher)

    await catalog_sync.sync("test")

    connection = client.connections[0]
    assert connection.server_id == server.id
    assert connection.url == server.url
    assert connection.headers == {"Authorization": "Bearer s3cr3t"}


@pytest.mark.asyncio
async def test_headers_that_do_not_decrypt_fail_only_their_server(
    server_store, tool_store, client
) -> None:
    writer = HeaderCipher(HeaderCipher.generate_key())
    token = writer.encrypt({"Authorization": "Bearer s3cr3t"})
    assert token is not None
    broken = await server_store.create(
        server_record("broken", headers_encrypted=token)
    )
    healthy = await server_store.create(server_record("healthy"))
    client.results[healthy.id] = [mcp_tool("search")]
    other_key = HeaderCipher(HeaderCipher.generate_key())
    catalog_sync = McpCatalogSync(
        server_store, tool_store, client, cipher=other_key
    )

    stats = await catalog_sync.sync("test")

    assert stats.servers_failed == 1
    assert broken.id not in client.called
    assert stats.error is not None
    assert "could not be decrypted" in stats.error
    assert token not in stats.error
    assert "s3cr3t" not in stats.error
    assert [failure.server_id for failure in stats.failed_servers] == [broken.id]
    assert stats.failed_servers[0].owner_user_id == OWNER
    healthy_row = await tool_store.get_by_call_name("mcp__healthy__search", OWNER)
    assert healthy_row is not None and healthy_row.callable is True


@pytest.mark.asyncio
async def test_stored_headers_without_a_cipher_fail_the_server(
    server_store, tool_store, client, sync
) -> None:
    server = await server_store.create(
        server_record("secure", headers_encrypted="fernet-token")
    )
    client.results[server.id] = [mcp_tool("search")]

    stats = await sync.sync("test")

    assert stats.servers_failed == 1
    assert client.called == []
    assert stats.error is not None
    assert "no cipher" in stats.error
    assert "fernet-token" not in stats.error
    assert [failure.server_id for failure in stats.failed_servers] == [server.id]


@pytest.mark.asyncio
async def test_upstream_failure_text_is_sanitized(
    server_store, tool_store, client, sync
) -> None:
    server = await server_store.create(
        server_record("leaky", url=URL + "?api_key=topsecret")
    )
    client.errors[server.id] = McpProtocolError(
        "boom " + URL + "?api_key=topsecret Authorization: Bearer s3cr3t"
    )

    stats = await sync.sync("test")

    assert stats.error is not None
    assert "leaky" in stats.error
    assert "topsecret" not in stats.error
    assert "s3cr3t" not in stats.error
    assert "https://" not in stats.error


def test_an_unexpected_exception_is_stored_as_its_class_only() -> None:
    note = sanitize_error_message(
        RuntimeError("GET " + URL + "?api_key=topsecret failed"),
        label="MCP server 'odd'",
    )

    assert note == "MCP server 'odd': sync failed (RuntimeError)"
    assert "topsecret" not in note


# --------------------------------------------------------------------------- #
# Milvus: the database commits first, the index is best effort
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_index_failure_keeps_the_database_commit(
    server_store, tool_store, client
) -> None:
    server = await server_store.create(server_record("anysearch"))
    client.results[server.id] = [mcp_tool("search")]
    index = FakeIndex(error=RuntimeError("milvus is down"))
    embedder = object()
    catalog_sync = McpCatalogSync(
        server_store, tool_store, client, index=index, embedder=embedder
    )

    stats = await catalog_sync.sync("test", full=True)

    row = await tool_store.get_by_call_name("mcp__anysearch__search", OWNER)
    assert row is not None and row.callable is True
    assert stats.upserted == 1
    assert stats.index_failed is True
    assert stats.error is not None and "RuntimeError" in stats.error
    assert index.calls[-1]["full"] is True
    assert index.calls[-1]["embedder"] is embedder
    assert [record.call_name for record in index.calls[-1]["records"]] == [
        "mcp__anysearch__search"
    ]


@pytest.mark.asyncio
async def test_index_is_skipped_when_no_embedder_is_configured(
    server_store, tool_store, client
) -> None:
    server = await server_store.create(server_record("anysearch"))
    client.results[server.id] = [mcp_tool("search")]
    index = FakeIndex()
    catalog_sync = McpCatalogSync(server_store, tool_store, client, index=index)

    stats = await catalog_sync.sync("test")

    assert index.calls == []
    assert stats.index_failed is False
    assert stats.error is None


# --------------------------------------------------------------------------- #
# Catalog: the retrieval text of one tool
# --------------------------------------------------------------------------- #


def test_parameter_names_reads_the_declared_properties_in_order() -> None:
    schema = {"type": "object", "properties": {"query": {}, "repo": {}}}

    assert parameter_names(schema) == ("query", "repo")
    assert parameter_names(None) == ()
    assert parameter_names("not a schema") == ()


def test_tool_content_names_the_server_and_its_parameters() -> None:
    content = build_tool_content(
        "search",
        description="Find records",
        server="anysearch",
        parameters=("query", "repo"),
    )

    assert content == "search\nserver: anysearch\nFind records\nparameters: query, repo"


def test_tool_content_does_not_repeat_the_name_as_a_display_name() -> None:
    assert build_tool_content("search", display_name="search") == "search"


def test_tool_content_is_bounded() -> None:
    assert len(build_tool_content("x", description="d" * 100, max_chars=16)) == 16
    assert DEFAULT_MAX_DOCUMENT_CHARS == 4000


def test_tool_document_carries_the_title_and_the_schema_parameters() -> None:
    document = build_tool_document(
        name="search",
        title="  Search everything  ",
        description="Find records",
        server="anysearch",
        input_schema={"type": "object", "properties": {"query": {}}},
    )

    assert document.display_name == "Search everything"
    assert document.parameters == ("query",)
    assert document.content.splitlines()[0] == "search"
    assert "server: anysearch" in document.content


def test_tool_document_without_a_title_has_no_display_name() -> None:
    document = build_tool_document(
        name="search", input_schema={"type": "object", "properties": {}}
    )

    assert document.display_name is None
    assert document.parameters == ()


def test_sync_stats_default_to_a_no_op_run() -> None:
    stats = McpSyncStats()

    assert stats.error is None
    assert stats.index_failed is False
    assert stats.conflicts == ()

# --------------------------------------------------------------------------- #
# Filtered passes: one server's refresh must not walk (or retire) the rest
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_filtered_pass_contacts_only_the_named_servers(
    server_store, tool_store, client
) -> None:
    """A management write is one server's business.

    Contacting every owner's server would make one user's click wait on a
    stranger's dead endpoint, so the pass takes a server filter.  The snapshot
    stays complete: the out-of-scope rows are handed over as preserved, which is
    what keeps their callability intact.
    """
    mine = await server_store.create(server_record("mine"))
    other = await server_store.create(server_record("other", owner=OTHER_OWNER))
    client.results[mine.id] = [mcp_tool("search")]
    client.errors[other.id] = McpServerUnavailable("dead endpoint")
    await seed(tool_store, prior_tool(other, "old"))
    recording = RecordingToolStore(tool_store)
    sync = McpCatalogSync(server_store, recording, client)

    stats = await sync.sync("server_refresh", server_ids={mine.id})

    assert client.called == [mine.id]
    assert stats.servers_scanned == 1
    assert stats.servers_failed == 0
    assert stats.error is None
    # The stranger's row was neither rewritten nor retired.
    kept = await tool_store.get_by_call_name(
        call_name_for("other", "old"), OTHER_OWNER
    )
    assert kept is not None
    assert kept.callable is True
    assert kept.id in recording.preserved[0]
    # ... and the named server's tools are live.
    fresh = await tool_store.get_by_call_name(call_name_for("mine", "search"), OWNER)
    assert fresh is not None and fresh.callable is True


@pytest.mark.asyncio
async def test_a_filtered_pass_carries_a_disabled_servers_rows(
    server_store, tool_store, client
) -> None:
    """The disable path is a filtered pass over a switched-off server.

    Nothing is contacted (the server is off), and its rows come back
    reachable=False, which is what makes the store drop mcp_tools.callable.
    """
    off = await server_store.create(server_record("off", enabled=False))
    await seed(tool_store, prior_tool(off, "search"))
    sync = McpCatalogSync(server_store, tool_store, client)

    stats = await sync.sync("server_disabled", server_ids={off.id})

    assert client.called == []
    assert stats.servers_scanned == 1
    rows = await tool_store.list_for_server(off.id, OWNER)
    assert len(rows) == 1
    assert rows[0].callable is False
    assert rows[0].enabled is False


@pytest.mark.asyncio
async def test_a_whole_catalog_pass_still_retires_an_unreachable_servers_tools(
    server_store, tool_store, client, sync
) -> None:
    """The filter must not weaken the unfiltered pass it sits next to."""
    good = await server_store.create(server_record("good"))
    bad = await server_store.create(server_record("bad"))
    client.results[good.id] = [mcp_tool("search")]
    client.errors[bad.id] = McpServerUnavailable("down")
    await seed(tool_store, prior_tool(bad, "old"))

    await sync.sync("daily", full=True)

    assert sorted(client.called) == sorted([good.id, bad.id])
    retired = await tool_store.get_by_call_name(call_name_for("bad", "old"), OWNER)
    assert retired is not None and retired.callable is False

@pytest.mark.asyncio
async def test_a_stored_url_that_resolves_privately_is_never_contacted(
    server_store, tool_store, client, monkeypatch
) -> None:
    """A stored row can outlive the check that wrote it, so the pass re-resolves."""
    server = await server_store.create(server_record("rebound"))
    client.results[server.id] = [mcp_tool("search")]
    monkeypatch.setattr(
        net, "resolve_host_addresses", lambda host, port=None: ("127.0.0.1",)
    )
    sync = McpCatalogSync(server_store, tool_store, client)

    stats = await sync.sync("daily")

    assert client.called == []
    assert stats.servers_failed == 1
    assert [failure.server_id for failure in stats.failed_servers] == [server.id]
    assert "private" in (stats.failed_servers[0].error or "")
    assert "rebound" in (stats.failed_servers[0].error or "")


@pytest.mark.asyncio
async def test_allow_private_networks_lets_the_pass_reach_a_private_server(
    server_store, tool_store, client, monkeypatch
) -> None:
    server = await server_store.create(
        server_record("local", url="http://127.0.0.1:8080/mcp")
    )
    client.results[server.id] = [mcp_tool("search")]
    monkeypatch.setattr(
        net, "resolve_host_addresses", lambda host, port=None: ("127.0.0.1",)
    )
    sync = McpCatalogSync(
        server_store, tool_store, client, allow_private_networks=True
    )

    stats = await sync.sync("daily")

    assert client.called == [server.id]
    assert stats.servers_failed == 0


