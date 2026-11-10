"""MCP server service: URL policy, owner scope, encryption and sync orchestration.

The service is the only place that turns a wire write into a stored row.  These
tests pin the invariants the router and the runtime depend on: every operation is
owner-scoped, headers are encrypted at rest and masked on the way out, the URL
policy blocks SSRF-shaped inputs, and a failed sync leaves an inspectable error
row instead of losing the user's configuration.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.mcp import net
from agentkit.mcp.crypto import HeaderCipher, validate_headers
from agentkit.mcp.errors import (
    McpServerConfigurationError,
    McpServerConflict,
    McpServerNotFound,
    McpServerUnavailable,
    McpServerValidationError,
)
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.service import (
    MASK,
    McpServerReindexStats,
    McpServerService,
    McpServerWrite,
    ServerUrlPolicy,
    url_has_query,
    url_without_query,
)
from agentkit.mcp.store import (
    McpServerStore,
    McpToolRecord,
    McpToolStore,
    tool_id_for,
)
from agentkit.persistence.base import Base


class FakeSync:
    """The sync boundary: records the server ids, optionally fails, marks active."""

    def __init__(self, store: McpServerStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str | None]] = []
        self.removed: list[str] = []
        self.failure: BaseException | None = None
        self.reindex_stats = McpServerReindexStats()
        self.reindex_owners: list[str] = []

    async def sync_server(self, server_id: str, reason: str | None = None) -> None:
        self.calls.append((server_id, reason))
        if self.failure is not None:
            raise self.failure
        row = next((row for row in await self.store.list_all() if row.id == server_id), None)
        # Mirrors the runtime's stamp rule: a pass never revives a disabled row,
        # because nothing contacted it.
        if row is not None and row.enabled:
            await self.store.set_status(
                server_id, row.owner_user_id, status="active", reachable=True
            )

    async def remove_server_catalog(self, server_id: str) -> None:
        self.removed.append(server_id)

    async def reindex(self, owner_user_id: str) -> McpServerReindexStats:
        self.reindex_owners.append(owner_user_id)
        return self.reindex_stats


class FakeToolStore:
    def __init__(self, tools: list[Any] | None = None) -> None:
        self.tools = list(tools or [])
        self.calls: list[tuple[str, str]] = []

    async def list_for_server(self, server_id: str, owner_user_id: str) -> list[Any]:
        self.calls.append((server_id, owner_user_id))
        return [tool for tool in self.tools if tool.server_id == server_id]


def tool_record(*, server_id: str = "s1", name: str = "search") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{server_id}:{name}",
        server_id=server_id,
        owner_user_id="u1",
        name=name,
        call_name=f"mcp__anysearch__{name}",
        display_name=name.title(),
        description=f"{name} description",
        parameters=["q"],
        input_schema={"type": "object"},
        callable=True,
        enabled=True,
    )


def write(**overrides: Any) -> McpServerWrite:
    base: dict[str, Any] = {
        "name": "anysearch",
        "display_name": "AnySearch",
        "description": "实时搜索",
        "url": "https://api.anysearch.com/mcp",
        "transport": "streamable_http",
        "headers": None,
        "verify_tls": True,
        "enabled": True,
    }
    base.update(overrides)
    return McpServerWrite(**base)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp-service.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    Base.metadata.tables["mcp_servers"],
                    Base.metadata.tables["mcp_tools"],
                ],
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def store(session_factory):
    return McpServerStore(session_factory)


@pytest_asyncio.fixture
async def real_tool_store(session_factory):
    return McpToolStore(session_factory)


@pytest.fixture
def cipher() -> HeaderCipher:
    return HeaderCipher(HeaderCipher.generate_key())


@pytest.fixture
def sync(store: McpServerStore) -> FakeSync:
    return FakeSync(store)


@pytest.fixture
def tool_store() -> FakeToolStore:
    return FakeToolStore([tool_record(server_id="s1")])


@pytest.fixture
def policy() -> ServerUrlPolicy:
    return ServerUrlPolicy(
        allowed_schemes=["http", "https"],
        allow_private_networks=False,
        max_servers_per_user=3,
    )


@pytest.fixture
def service(
    store: McpServerStore,
    cipher: HeaderCipher,
    sync: FakeSync,
    tool_store: FakeToolStore,
    policy: ServerUrlPolicy,
) -> McpServerService:
    return McpServerService(
        store=store,
        cipher=cipher,
        client=SimpleNamespace(),
        sync=sync,
        tool_store=tool_store,
        policy=policy,
    )


# --------------------------------------------------------------------------- #
# Create: encryption at rest, pending state, sync
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_encrypts_headers_and_never_returns_them(
    service: McpServerService, store: McpServerStore
) -> None:
    record = await service.create_server(
        "u1", write(headers={"Authorization": "Bearer secret"})
    )

    assert record.owner_user_id == "u1"
    stored = await store.get(record.id, "u1")
    assert stored is not None
    assert stored.headers_encrypted and "secret" not in stored.headers_encrypted
    assert service.masked_headers(stored)["Authorization"] == MASK
    assert "secret" not in repr(service.masked_headers(stored))


@pytest.mark.asyncio
async def test_create_inserts_pending_then_returns_the_refreshed_row(
    service: McpServerService, sync: FakeSync
) -> None:
    record = await service.create_server("u1", write())

    assert sync.calls == [(record.id, None)]
    assert record.status == "active"
    assert record.reachable is True
    assert record.name == "anysearch"


@pytest.mark.asyncio
async def test_create_failure_keeps_the_row_and_records_a_sanitized_error(
    service: McpServerService, store: McpServerStore, sync: FakeSync
) -> None:
    sync.failure = McpServerUnavailable(
        "MCP server 'srv' is unavailable (ConnectError): Authorization: Bearer secret"
    )

    record = await service.create_server(
        "u1", write(headers={"Authorization": "Bearer secret"})
    )

    assert record.status == "error"
    assert record.reachable is False
    assert record.last_error is not None
    assert "secret" not in record.last_error
    assert "Bearer" not in record.last_error
    assert (await store.get(record.id, "u1")) is not None


@pytest.mark.asyncio
async def test_create_failure_redacts_the_endpoint_and_its_query_token(
    service: McpServerService, store: McpServerStore, sync: FakeSync
) -> None:
    sync.failure = McpServerUnavailable(
        "MCP server 'srv' is unavailable (ConnectError): "
        "POST https://api.anysearch.com/mcp?api_key=topsecret"
    )

    record = await service.create_server("u1", write())

    assert record.last_error is not None
    assert "unavailable" in record.last_error
    assert "topsecret" not in record.last_error
    assert "https://" not in record.last_error
    stored = await store.get(record.id, "u1")
    assert stored is not None and stored.last_error == record.last_error


@pytest.mark.asyncio
async def test_create_rejects_a_duplicate_name_for_the_same_owner(
    service: McpServerService,
) -> None:
    await service.create_server("u1", write())

    with pytest.raises(McpServerConflict):
        await service.create_server("u1", write())


@pytest.mark.asyncio
async def test_two_owners_may_use_the_same_name(service: McpServerService) -> None:
    first = await service.create_server("u1", write())
    second = await service.create_server("u2", write())

    assert first.id != second.id
    assert second.owner_user_id == "u2"


@pytest.mark.asyncio
async def test_create_enforces_the_per_user_limit(service: McpServerService) -> None:
    await service.create_server("u1", write(name="one"))
    await service.create_server("u1", write(name="two"))
    await service.create_server("u1", write(name="three"))

    with pytest.raises(McpServerConflict, match="limit"):
        await service.create_server("u1", write(name="four"))


@pytest.mark.asyncio
async def test_create_rejects_crlf_and_too_many_headers(
    service: McpServerService,
) -> None:
    with pytest.raises(McpServerValidationError, match="CR"):
        await service.create_server(
            "u1",
            write(name="header-injection", headers={"X-Test": "a\r\nInjected: x"}),
        )
    too_many = {f"X-Custom-{index}": "v" for index in range(33)}
    with pytest.raises(McpServerValidationError, match="at most"):
        await service.create_server(
            "u1", write(name="too-many", headers=too_many)
        )


@pytest.mark.asyncio
async def test_headers_may_use_the_full_32_kib_total_budget(
    service: McpServerService, cipher: HeaderCipher, store: McpServerStore
) -> None:
    headers = {f"X-Custom-{index}": "x" * 3000 for index in range(8)}
    record = await service.create_server(
        "u1", write(name="large-headers", headers=headers)
    )

    stored = await store.get(record.id, "u1")
    assert stored is not None
    assert cipher.decrypt(stored.headers_encrypted) == headers


@pytest.mark.asyncio
async def test_a_tab_is_allowed_inside_a_header_value(
    service: McpServerService, cipher: HeaderCipher, store: McpServerStore
) -> None:
    record = await service.create_server(
        "u1", write(name="tabbed", headers={"X-Note": "a\tb"})
    )

    stored = await store.get(record.id, "u1")
    assert stored is not None
    assert cipher.decrypt(stored.headers_encrypted) == {"X-Note": "a\tb"}


@pytest.mark.asyncio
async def test_create_rejects_a_literal_mask_header(service: McpServerService) -> None:
    with pytest.raises(McpServerValidationError, match="masked"):
        await service.create_server(
            "u1", write(name="masked", headers={"Authorization": MASK})
        )


@pytest.mark.asyncio
async def test_create_rejects_explicit_null_verify_tls(
    service: McpServerService,
) -> None:
    with pytest.raises(McpServerValidationError, match="verify_tls"):
        await service.create_server(
            "u1", write(name="tls-null", verify_tls=None)
        )


# --------------------------------------------------------------------------- #
# Owner scoping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_owner_cannot_read_another_users_server(service: McpServerService) -> None:
    record = await service.create_server("u1", write())

    with pytest.raises(McpServerNotFound):
        await service.get_server(record.id, "u2")
    with pytest.raises(McpServerNotFound):
        await service.update_server(record.id, "u2", McpServerWrite(display_name="x"))
    with pytest.raises(McpServerNotFound):
        await service.delete_server(record.id, "u2")


@pytest.mark.asyncio
async def test_list_is_owner_scoped(service: McpServerService) -> None:
    await service.create_server("u1", write(name="mine"))
    await service.create_server("u2", write(name="theirs"))

    assert [row.name for row in await service.list_servers("u1")] == ["mine"]
    assert [row.name for row in await service.list_servers("u2")] == ["theirs"]


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.com/v1/mcp?tenant=acme",
        "http://mcp.example.com/v1/mcp?tenant=acme",
    ],
)
def test_url_policy_accepts_http_and_https_with_path_and_query(
    policy: ServerUrlPolicy, url: str
) -> None:
    assert policy.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/mcp",
        "http://localhost/mcp",
        "http://127.0.0.1/mcp",
        "http://169.254.169.254/latest/meta-data/",
        "https://LOCALHOST./mcp",
        "https://api.localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://10.0.0.1/mcp",
        "https://169.254.169.254/latest/meta-data/",
        # CGNAT space: not is_private/is_reserved under ipaddress, but not globally
        # routable either - this is where a cloud metadata endpoint can live.
        "https://100.100.100.200/latest/meta-data/",
        "https://0.0.0.0/mcp",
        "https://127.1/mcp",
        "https://0177.0.0.1/mcp",
        "https://0x7f000001/mcp",
        "https://2130706433/mcp",
        "https://user:secret@mcp.example.com/mcp",
        "https://mcp.example.com/mcp#frag",
        "ftp://mcp.example.com/mcp",
        "not-a-url",
    ],
)
def test_url_policy_rejects_ssrf_shaped_urls(
    policy: ServerUrlPolicy, url: str
) -> None:
    with pytest.raises(McpServerValidationError):
        policy.validate_url(url)


def test_url_policy_can_explicitly_allow_private_networks() -> None:
    policy = ServerUrlPolicy(
        allowed_schemes=["http", "https"], allow_private_networks=True
    )

    assert policy.validate_url("http://127.0.0.1:8080/mcp")
    assert policy.validate_url("https://localhost/mcp")


def test_url_query_is_redacted_but_reported() -> None:
    url = "https://mcp.example.com/v1/mcp?tenant=acme&token=query-secret"

    assert url_without_query(url) == "https://mcp.example.com/v1/mcp"
    assert url_has_query(url) is True
    assert url_has_query("https://mcp.example.com/v1/mcp") is False
    assert (
        url_without_query("https://mcp.example.com/v1/mcp")
        == "https://mcp.example.com/v1/mcp"
    )


# --------------------------------------------------------------------------- #
# Update: masked header preservation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_preserves_masked_headers_from_the_encrypted_map(
    service: McpServerService, store: McpServerStore, cipher: HeaderCipher
) -> None:
    created = await service.create_server(
        "u1",
        write(headers={"Authorization": "Bearer old", "X-Env": "prod"}),
    )

    updated = await service.update_server(
        created.id,
        "u1",
        McpServerWrite(
            display_name="AnySearch 2",
            headers={"Authorization": MASK, "X-Env": "staging"},
        ),
    )

    assert updated.display_name == "AnySearch 2"
    assert updated.headers_encrypted != created.headers_encrypted
    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert cipher.decrypt(stored.headers_encrypted) == {
        "Authorization": "Bearer old",
        "X-Env": "staging",
    }


@pytest.mark.asyncio
async def test_update_omitted_headers_keep_the_existing_encrypted_map(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server(
        "u1", write(headers={"Authorization": "Bearer old"})
    )

    updated = await service.update_server(
        created.id, "u1", McpServerWrite(description="new description")
    )

    assert updated.headers_encrypted == created.headers_encrypted
    assert updated.description == "new description"


@pytest.mark.asyncio
async def test_update_rejects_a_masked_header_with_no_stored_value(
    service: McpServerService,
) -> None:
    created = await service.create_server("u1", write())

    with pytest.raises(McpServerValidationError, match="masked"):
        await service.update_server(
            created.id, "u1", McpServerWrite(headers={"Authorization": MASK})
        )


@pytest.mark.asyncio
async def test_update_cannot_change_the_name(service: McpServerService) -> None:
    created = await service.create_server("u1", write())

    with pytest.raises(McpServerValidationError, match="name"):
        await service.update_server(
            created.id, "u1", McpServerWrite(name="renamed")
        )


@pytest.mark.asyncio
async def test_update_syncs_and_returns_the_refreshed_row(
    service: McpServerService, sync: FakeSync
) -> None:
    created = await service.create_server("u1", write())
    sync.calls.clear()

    updated = await service.update_server(
        created.id, "u1", McpServerWrite(display_name="Renamed")
    )

    assert sync.calls == [(created.id, None)]
    assert updated.status == "active"


@pytest.mark.asyncio
async def test_update_sync_failure_keeps_the_new_configuration(
    service: McpServerService, store: McpServerStore, sync: FakeSync
) -> None:
    created = await service.create_server("u1", write())
    sync.failure = McpServerUnavailable("unreachable")

    updated = await service.update_server(
        created.id, "u1", McpServerWrite(display_name="Still saved")
    )

    assert updated.display_name == "Still saved"
    assert updated.status == "error"
    assert updated.reachable is False


@pytest.mark.asyncio
async def test_update_rejects_explicit_null_verify_tls(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server("u1", write())

    with pytest.raises(McpServerValidationError, match="verify_tls"):
        await service.update_server(
            created.id, "u1", McpServerWrite(verify_tls=None)
        )

    stored = await store.get(created.id, "u1")
    assert stored is not None and stored.verify_tls is True


@pytest.mark.asyncio
async def test_update_preserves_the_stored_query_when_incoming_has_none(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server(
        "u1",
        write(url="https://api.anysearch.com/mcp?tenant=acme&token=query-secret"),
    )

    updated = await service.update_server(
        created.id,
        "u1",
        McpServerWrite(url="https://api.anysearch.com/v2/mcp"),
    )

    assert updated.id == created.id
    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://api.anysearch.com/v2/mcp?tenant=acme&token=query-secret"
    assert url_without_query(stored.url) == "https://api.anysearch.com/v2/mcp"


@pytest.mark.asyncio
async def test_update_accepts_an_explicit_new_query(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server(
        "u1", write(url="https://api.anysearch.com/mcp?tenant=acme")
    )

    await service.update_server(
        created.id,
        "u1",
        McpServerWrite(url="https://api.anysearch.com/mcp?tenant=other"),
    )

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://api.anysearch.com/mcp?tenant=other"


@pytest.mark.asyncio
async def test_update_of_a_disabled_server_does_not_sync(
    service: McpServerService, sync: FakeSync
) -> None:
    created = await service.create_server(
        "u1", write(name="off", enabled=False)
    )
    sync.calls.clear()

    updated = await service.update_server(
        created.id, "u1", McpServerWrite(display_name="Still off")
    )

    assert updated.display_name == "Still off"
    assert updated.status == "disabled"
    assert sync.calls == []


# --------------------------------------------------------------------------- #
# Delete, state, refresh, tools
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_removes_the_row_then_the_catalog(
    service: McpServerService, store: McpServerStore, sync: FakeSync
) -> None:
    created = await service.create_server("u1", write())

    assert await service.delete_server(created.id, "u1") is True
    assert await store.get(created.id, "u1") is None
    assert sync.removed == [created.id]


@pytest.mark.asyncio
async def test_delete_is_owner_scoped_and_never_touches_the_catalog(
    service: McpServerService, sync: FakeSync
) -> None:
    created = await service.create_server("u1", write())

    with pytest.raises(McpServerNotFound):
        await service.delete_server(created.id, "u2")

    assert sync.removed == []


@pytest.mark.asyncio
async def test_disable_retires_the_catalog_and_enable_syncs_it_again(
    service: McpServerService, sync: FakeSync
) -> None:
    created = await service.create_server("u1", write())
    sync.calls.clear()

    disabled = await service.set_enabled(created.id, "u1", False)
    assert disabled.enabled is False
    assert disabled.status == "disabled"
    # Disabling is a write *and* a catalog change: callable is a store output, so
    # the rows have to be carried as unreachable before tool_search stops offering
    # them.  One pass, with its own reason.
    assert sync.calls == [(created.id, "server_disabled")]

    enabled = await service.set_enabled(created.id, "u1", True)
    assert enabled.enabled is True
    assert enabled.status == "active"
    assert sync.calls[-1] == (created.id, None)

    with pytest.raises(McpServerNotFound):
        await service.set_enabled(created.id, "u2", True)


@pytest.mark.asyncio
async def test_a_failed_retirement_keeps_the_server_disabled_and_uncallable(
    service: McpServerService,
    sync: FakeSync,
    real_tool_store: McpToolStore,
) -> None:
    created = await service.create_server("u1", write())
    await real_tool_store.upsert_snapshot(
        [
            McpToolRecord(
                server_id=created.id,
                server_name="anysearch",
                owner_user_id="u1",
                name="search",
                description="search tool",
                input_schema={"type": "object"},
                content="search anysearch",
            )
        ],
        seen_ids={tool_id_for(created.id, "search")},
    )
    tool_id = (await real_tool_store.list_for_server(created.id, "u1"))[0].id
    service.tool_store = real_tool_store
    sync.calls.clear()
    sync.failure = RuntimeError("catalog is down")

    disabled = await service.set_enabled(created.id, "u1", False)

    # The disable landed; a sync failure must not flip the row to error and must
    # not fail the request.  The pass was actually attempted, and the DB join in
    # get_by_ids keeps the still-callable row out of tool_search.
    assert sync.calls == [(created.id, "server_disabled")]
    assert disabled.enabled is False
    assert disabled.status == "disabled"
    assert (await real_tool_store.list_for_server(created.id, "u1"))[0].callable is True
    assert await real_tool_store.get_by_ids([tool_id], "u1") == []


@pytest.mark.asyncio
async def test_refresh_syncs_with_the_refresh_reason(
    service: McpServerService, sync: FakeSync
) -> None:
    created = await service.create_server("u1", write())
    sync.calls.clear()

    refreshed = await service.refresh_server(created.id, "u1")

    assert refreshed.status == "active"
    assert sync.calls == [(created.id, "server_refresh")]


@pytest.mark.asyncio
async def test_list_tools_is_owner_scoped_and_returns_store_rows(
    service: McpServerService, tool_store: FakeToolStore
) -> None:
    created = await service.create_server("u1", write())
    tool_store.tools = [tool_record(server_id=created.id)]

    tools = await service.list_tools(created.id, "u1")

    assert [tool.name for tool in tools] == ["search"]
    assert tool_store.calls == [(created.id, "u1")]
    with pytest.raises(McpServerNotFound):
        await service.list_tools(created.id, "u2")


# --------------------------------------------------------------------------- #
# Status, reindex and storage failures
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_is_owner_scoped_and_reports_real_counts(
    service: McpServerService,
    store: McpServerStore,
    tool_store: FakeToolStore,
) -> None:
    active = await service.create_server("u1", write(name="active"))
    errored = await service.create_server("u1", write(name="errored"))
    await store.set_status(
        errored.id, "u1", status="error", reachable=False, last_error="boom"
    )
    await service.create_server("u2", write(name="other-owner"))
    tool_store.tools = [
        tool_record(server_id=active.id, name="one"),
        tool_record(server_id=errored.id, name="two"),
    ]

    stats = await service.status("u1")

    assert stats["servers_total"] == 2
    assert stats["servers_active"] == 1
    assert stats["servers_error"] == 1
    assert stats["servers_pending"] == 0
    assert stats["servers_disabled"] == 0
    assert stats["servers_reachable"] == 1
    assert stats["tools_total"] == 2
    assert stats["indexed_tools"] == 2
    assert stats["last_error"] == "boom"
    assert {call[1] for call in tool_store.calls} == {"u1"}


@pytest.mark.asyncio
async def test_reindex_returns_the_typed_sync_stats(
    service: McpServerService, sync: FakeSync
) -> None:
    sync.reindex_stats = McpServerReindexStats(
        scanned=2,
        changed=1,
        upserted=3,
        deleted=4,
        failed=1,
        duration_ms=12,
        error="partial",
    )

    stats = await service.reindex("u1")

    assert stats == sync.reindex_stats
    assert sync.reindex_owners == ["u1"]


@pytest.mark.asyncio
async def test_masked_headers_reports_a_configuration_error_not_a_422(
    service: McpServerService, store: McpServerStore
) -> None:
    other_cipher = HeaderCipher(HeaderCipher.generate_key())
    token = other_cipher.encrypt({"Authorization": "Bearer old"})
    record = await store.create(
        McpServerRecord(
            owner_user_id="u1",
            name="wrong-key",
            url="https://api.anysearch.com/mcp",
            headers_encrypted=token,
        )
    )

    with pytest.raises(McpServerConfigurationError):
        service.masked_headers(record)


def test_shared_header_validator_keeps_the_32_kib_budget() -> None:
    headers = {f"X-Custom-{index}": "x" * 3000 for index in range(8)}

    assert validate_headers(headers) == headers

# --------------------------------------------------------------------------- #
# URL query custody: the stored query is a credential
# --------------------------------------------------------------------------- #

QUERY_URL = "https://api.anysearch.com/mcp?token=old-secret"


@pytest.mark.asyncio
async def test_a_same_origin_path_change_preserves_the_stored_query(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server("u1", write(url=QUERY_URL))

    await service.update_server(
        created.id, "u1", McpServerWrite(url="https://api.anysearch.com/v2/mcp")
    )

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://api.anysearch.com/v2/mcp?token=old-secret"


@pytest.mark.asyncio
async def test_a_cross_host_edit_drops_the_stored_query(
    service: McpServerService, store: McpServerStore
) -> None:
    """The old token must never be re-attached to a different server."""
    created = await service.create_server("u1", write(url=QUERY_URL))

    await service.update_server(
        created.id, "u1", McpServerWrite(url="https://mcp.example.net/mcp")
    )

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://mcp.example.net/mcp"
    assert "old-secret" not in stored.url


@pytest.mark.asyncio
async def test_a_cross_port_edit_is_a_different_origin_too(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server("u1", write(url=QUERY_URL))

    await service.update_server(
        created.id, "u1", McpServerWrite(url="https://api.anysearch.com:8443/mcp")
    )

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert "old-secret" not in stored.url


@pytest.mark.asyncio
async def test_clear_query_removes_a_stored_query_without_changing_the_url(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server("u1", write(url=QUERY_URL))

    await service.update_server(created.id, "u1", McpServerWrite(clear_query=True))

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://api.anysearch.com/mcp"
    assert url_has_query(stored.url) is False


@pytest.mark.asyncio
async def test_clear_query_wins_over_a_query_in_the_incoming_url(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server("u1", write(url=QUERY_URL))

    await service.update_server(
        created.id,
        "u1",
        McpServerWrite(url="https://api.anysearch.com/mcp?token=new", clear_query=True),
    )

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://api.anysearch.com/mcp"


@pytest.mark.asyncio
async def test_an_explicit_new_query_replaces_the_stored_one(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server("u1", write(url=QUERY_URL))

    await service.update_server(
        created.id, "u1", McpServerWrite(url="https://api.anysearch.com/mcp?token=new")
    )

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://api.anysearch.com/mcp?token=new"


# --------------------------------------------------------------------------- #
# Header masking: every value, not a name allowlist
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_header_value_is_masked_not_just_known_names(
    service: McpServerService, store: McpServerStore
) -> None:
    created = await service.create_server(
        "u1",
        write(
            headers={
                "Authorization": "Bearer secret",
                "X-Custom-Token": "vendor-secret",
                "X-Goog-Api-Key": "google-secret",
                "X-Env": "prod",
            }
        ),
    )

    masked = service.masked_headers(await store.get(created.id, "u1"))

    assert set(masked) == {"Authorization", "X-Custom-Token", "X-Goog-Api-Key", "X-Env"}
    assert set(masked.values()) == {MASK}
    assert "vendor-secret" not in repr(masked)
    assert "google-secret" not in repr(masked)
    assert "prod" not in repr(masked)


@pytest.mark.asyncio
async def test_masked_headers_round_trip_restores_every_value(
    service: McpServerService, store: McpServerStore, cipher: HeaderCipher
) -> None:
    created = await service.create_server(
        "u1", write(headers={"X-Custom-Token": "vendor-secret"})
    )
    stored = await store.get(created.id, "u1")
    assert stored is not None

    await service.update_server(
        created.id, "u1", McpServerWrite(headers=service.masked_headers(stored))
    )

    assert cipher.decrypt((await store.get(created.id, "u1")).headers_encrypted) == {
        "X-Custom-Token": "vendor-secret"
    }


# --------------------------------------------------------------------------- #
# display_name is a VARCHAR(255): the service refuses a longer label
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_display_name_longer_than_the_column_is_a_validation_error(
    service: McpServerService,
) -> None:
    with pytest.raises(McpServerValidationError, match="display_name"):
        await service.create_server("u1", write(display_name="x" * 256))


@pytest.mark.asyncio
async def test_display_name_at_the_column_width_is_accepted(
    service: McpServerService,
) -> None:
    record = await service.create_server("u1", write(display_name="x" * 255))

    assert record.display_name == "x" * 255


@pytest.mark.asyncio
async def test_an_update_cannot_smuggle_a_too_long_display_name(
    service: McpServerService,
) -> None:
    created = await service.create_server("u1", write())

    with pytest.raises(McpServerValidationError, match="display_name"):
        await service.update_server(
            created.id, "u1", McpServerWrite(display_name="y" * 256)
        )


# --------------------------------------------------------------------------- #
# SSRF: the hostname is resolved, not just matched as a literal
# --------------------------------------------------------------------------- #


def _resolves_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[str]:
    asked: list[str] = []

    def _resolve(host: str, port: int | None = None) -> tuple[str, ...]:
        asked.append(host)
        return tuple(addresses)

    monkeypatch.setattr(net, "resolve_host_addresses", _resolve)
    return asked


@pytest.mark.asyncio
async def test_a_hostname_resolving_to_loopback_is_rejected(
    service: McpServerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolves_to(monkeypatch, "127.0.0.1")

    with pytest.raises(McpServerValidationError, match="private"):
        await service.create_server(
            "u1", write(url="https://rebind.anysearch.com/mcp")
        )


@pytest.mark.asyncio
async def test_a_hostname_resolving_to_a_private_range_is_rejected(
    service: McpServerService, store: McpServerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolves_to(monkeypatch, "93.184.216.34", "10.1.2.3")

    with pytest.raises(McpServerValidationError, match="10.1.2.3"):
        await service.create_server("u1", write(url="https://mixed.anysearch.com/mcp"))

    assert await store.list_for_owner("u1") == []


@pytest.mark.asyncio
async def test_an_update_cannot_point_a_stored_server_at_a_private_host(
    service: McpServerService, store: McpServerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await service.create_server("u1", write())
    _resolves_to(monkeypatch, "169.254.169.254")

    with pytest.raises(McpServerValidationError, match="private"):
        await service.update_server(
            created.id, "u1", McpServerWrite(url="https://metadata.anysearch.com/mcp")
        )

    stored = await store.get(created.id, "u1")
    assert stored is not None
    assert stored.url == "https://api.anysearch.com/mcp"


@pytest.mark.asyncio
async def test_a_public_resolution_is_accepted(
    service: McpServerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _resolves_to(monkeypatch, "93.184.216.34")

    record = await service.create_server("u1", write())

    assert record.url == "https://api.anysearch.com/mcp"
    assert asked == ["api.anysearch.com"]


@pytest.mark.asyncio
async def test_allow_private_networks_skips_the_resolution_check(
    store: McpServerStore,
    cipher: HeaderCipher,
    sync: FakeSync,
    tool_store: FakeToolStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _resolves_to(monkeypatch, "127.0.0.1")
    service = McpServerService(
        store=store,
        cipher=cipher,
        client=SimpleNamespace(),
        sync=sync,
        tool_store=tool_store,
        policy=ServerUrlPolicy(
            allowed_schemes=["http", "https"],
            allow_private_networks=True,
            max_servers_per_user=3,
        ),
    )

    record = await service.create_server("u1", write(url="http://127.0.0.1:8080/mcp"))

    assert record.url == "http://127.0.0.1:8080/mcp"
    assert asked == []


@pytest.mark.asyncio
async def test_a_failed_resolution_is_retried_instead_of_cached_as_public(
    service: McpServerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure must not mask the address the host starts resolving to.

    The first lookup fails (an empty tuple to the guard), the second returns
    loopback.  Caching the empty result would keep accepting this host forever and
    silently defeat the re-check on every later sync/call.
    """
    calls: list[str] = []

    def _flaky(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        calls.append(host)
        if len(calls) == 1:
            raise socket.gaierror("temporary failure")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ]

    monkeypatch.setattr(net.socket, "getaddrinfo", _flaky)

    await service.create_server(
        "u1",
        write(name="first-lookup", url="https://retry-once.anysearch.com/mcp"),
    )
    with pytest.raises(McpServerValidationError, match="127.0.0.1"):
        await service.create_server(
            "u1",
            write(name="second-lookup", url="https://retry-once.anysearch.com/mcp"),
        )

    assert calls == ["retry-once.anysearch.com", "retry-once.anysearch.com"]


@pytest.mark.asyncio
async def test_an_unresolvable_hostname_is_not_rejected_here(
    service: McpServerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best effort: an offline deployment must still be able to store a server."""
    _resolves_to(monkeypatch)

    record = await service.create_server("u1", write())

    assert record.url == "https://api.anysearch.com/mcp"
