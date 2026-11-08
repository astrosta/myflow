"""MCP server service: URL policy, owner scope, encryption and sync orchestration.

The service is the only place that turns a wire write into a stored row.  These
tests pin the invariants the router and the runtime depend on: every operation is
owner-scoped, headers are encrypted at rest and masked on the way out, the URL
policy blocks SSRF-shaped inputs, and a failed sync leaves an inspectable error
row instead of losing the user's configuration.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.mcp.crypto import HeaderCipher
from agentkit.mcp.errors import (
    McpServerConflict,
    McpServerNotFound,
    McpServerUnavailable,
    McpServerValidationError,
)
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.service import (
    MASK,
    McpServerService,
    McpServerWrite,
    ServerUrlPolicy,
)
from agentkit.mcp.store import McpServerStore
from agentkit.persistence.base import Base


class FakeSync:
    """The sync boundary: records the server ids, optionally fails, marks active."""

    def __init__(self, store: McpServerStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str | None]] = []
        self.removed: list[str] = []
        self.failure: BaseException | None = None

    async def sync_server(self, server_id: str, reason: str | None = None) -> None:
        self.calls.append((server_id, reason))
        if self.failure is not None:
            raise self.failure
        row = next((row for row in await self.store.list_all() if row.id == server_id), None)
        if row is not None:
            await self.store.set_status(
                server_id, row.owner_user_id, status="active", reachable=True
            )

    async def remove_server_catalog(self, server_id: str) -> None:
        self.removed.append(server_id)


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
async def store(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp-service.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Base.metadata.tables["mcp_servers"]]
            )
        )
    yield McpServerStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


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
        allowed_schemes=["https"],
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


def test_url_policy_accepts_https_with_path_and_query(policy: ServerUrlPolicy) -> None:
    url = "https://mcp.example.com/v1/mcp?tenant=acme"
    assert policy.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://localhost/mcp",
        "https://LOCALHOST./mcp",
        "https://api.localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://10.0.0.1/mcp",
        "https://169.254.169.254/latest/meta-data/",
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
async def test_set_enabled_is_owner_scoped_and_syncs_only_when_enabling(
    service: McpServerService, sync: FakeSync
) -> None:
    created = await service.create_server("u1", write())
    sync.calls.clear()

    disabled = await service.set_enabled(created.id, "u1", False)
    assert disabled.enabled is False
    assert disabled.status == "disabled"
    assert sync.calls == []

    enabled = await service.set_enabled(created.id, "u1", True)
    assert enabled.enabled is True
    assert enabled.status == "active"
    assert sync.calls == [(created.id, None)]

    with pytest.raises(McpServerNotFound):
        await service.set_enabled(created.id, "u2", True)


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
