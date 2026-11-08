"""tool_execute: direct execution against the MCP server that owns the tool.

tool_search hands the model an ``mcp__<server>__<tool>`` call name, its verbatim
schema and the arguments it must send; tool_execute is the only thing that
actually runs the tool, and it runs it by connecting straight to the server that
owns it and calling ``tools/call`` with the original (unprefixed) name.  These
tests pin the contract around that call:

* two gates before anything leaves the process - the call name must be in
  ``ThreadState.retrieved_tool_names`` for this run, and the row must be this
  owner's, still callable in ``mcp_tools``, and behind a server that is enabled
  and reachable.  A refusal names *which* gate closed: ``server_disabled``,
  ``server_unavailable`` or ``tool_not_found``;
* local validation with Draft202012Validator, with remote $ref retrieval disabled
  so a stored schema can never make the gateway fetch a URL - and, because the
  check cannot be skipped, a schema that will not compile or resolve is refused
  instead of deferred to the server (design 9.3: fail closed);
* the failure fallback of design 9.1: a local validation failure and an upstream
  invalid-params/schema-mismatch refresh the server and answer with the new schema
  but never retry, a stale or missing tool refreshes and retries exactly once, and
  a timeout/unreachable server/protocol failure/upstream error neither refreshes
  nor retries;
* only a not-found or a schema/argument mismatch refreshes the owning server, and
  ``refresh_server`` is called with that server's id;
* the reply is a ToolMessage that carries the inner name, tool_id and call name in
  ``myflow_tool_meta``, so an audit sees the tool that ran, not tool_execute;
* a refresh that completed but whose catalog re-read then raised is
  ``mcp_unavailable``, never a refreshed ``tool_not_found``: the third outcome
  exists because "the row is gone" and "I could not look" are different facts, and
  only one of them tells the model to stop calling the tool;
* no failure text ever carries a request header, a bearer token or an endpoint.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from mcp import types as mcp_types
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import agentkit.tools.builtin.tool_execute as executor_module
from agentkit.agents.middlewares.tool_result_meta import TOOL_META_KEY
from agentkit.mcp.errors import (
    McpProtocolError,
    McpServerTimeout,
    McpServerUnavailable,
    McpToolError,
)
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.runtime_protocol import (
    get_mcp_runtime,
    reset_mcp_runtime,
    set_mcp_runtime,
)
from agentkit.mcp.store.repository import McpServerStore
from agentkit.mcp.store.tool_repository import (
    McpToolRecord,
    McpToolStore,
    call_name_for,
    tool_id_for,
)
from agentkit.persistence.schema import Base
from agentkit.tools.builtin.tool_execute import TOOL_NAME, tool_execute

SERVER_ID = "s1"
SERVER_NAME = "anysearch"
CALL_NAME = call_name_for(SERVER_NAME, "search")

#: The schema tool_search showed the model: ``query`` is required.
QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["query"],
}
#: The upstream schema after someone edited the tool: the required field moved.
TEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


@pytest.fixture(autouse=True)
def _unpublished_runtime() -> Any:
    reset_mcp_runtime()
    yield
    reset_mcp_runtime()


@asynccontextmanager
async def catalog(
    tmp_path: Path,
) -> AsyncIterator[tuple[McpToolStore, McpServerStore]]:
    """One SQLite database with both MCP stores, as the runtime builds them."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "mcp.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield McpToolStore(factory), McpServerStore(factory)
    finally:
        await engine.dispose()


async def _server(
    server_store: McpServerStore,
    *,
    server_id: str = SERVER_ID,
    name: str = SERVER_NAME,
    owner: str = "u1",
    enabled: bool = True,
    reachable: bool = True,
) -> McpServerRecord:
    """One stored server, left in the state the test needs."""
    created = await server_store.create(
        McpServerRecord(
            id=server_id,
            owner_user_id=owner,
            name=name,
            transport="streamable_http",
            url="https://mcp.example.com/mcp",
        )
    )
    await server_store.set_status(
        created.id,
        owner,
        status="active" if reachable else "error",
        reachable=reachable,
        last_error=None if reachable else "connect timed out",
    )
    if not enabled:
        await server_store.set_enabled(created.id, owner, False)
    stored = await server_store.get(created.id, owner)
    assert stored is not None
    return stored


def _tool(
    name: str = "search",
    *,
    owner: str = "u1",
    server_id: str = SERVER_ID,
    server_name: str = SERVER_NAME,
    schema: dict[str, Any] | None = None,
) -> McpToolRecord:
    return McpToolRecord(
        server_id=server_id,
        server_name=server_name,
        owner_user_id=owner,
        name=name,
        call_name=call_name_for(server_name, name),
        description="Search the web",
        input_schema=dict(QUERY_SCHEMA) if schema is None else schema,
        parameters=("query",),
        content=name,
    )


async def _store_tool(
    store: McpToolStore,
    record: McpToolRecord,
    *,
    seen: bool = True,
) -> None:
    """Write one row; ``seen=False`` stores it without making it callable."""
    ids = [tool_id_for(record.server_id, record.name)] if seen else []
    await store.upsert_snapshot([record], ids)


def _ok(*blocks: Any) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=list(blocks), isError=False)


def _text(text: str) -> mcp_types.TextContent:
    return mcp_types.TextContent(type="text", text=text)


def _error(text: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[_text(text)], isError=True)


class FakeRuntime:
    """The published runtime, shaped like McpToolRuntime: stores, refresh, call.

    ``script`` is consumed one entry per tools/call: an exception entry is raised,
    a CallToolResult is returned.  An empty script raises instead of repeating
    itself, because a test that says "one call" must fail if the executor calls
    twice.
    """

    def __init__(
        self,
        tool_store: Any,
        server_store: Any,
        *script: Any,
        on_refresh: Any = None,
        refresh_error: Exception | None = None,
        refresh_delay: float = 0.0,
        refresh_stats: Any = None,
        retry_on_tool_error: bool = True,
        require_retrieved: bool | None = None,
    ) -> None:
        self.tool_store = tool_store
        self.server_store = server_store
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.refreshed: list[str] = []
        self.retries: list[tuple[str, str]] = []
        self.retry_on_tool_error = retry_on_tool_error
        self.require_retrieved = require_retrieved
        self.config = SimpleNamespace(
            tool_search=SimpleNamespace(require_retrieved=True),
        )
        self._script = list(script)
        self._on_refresh = on_refresh
        self._refresh_error = refresh_error
        self._refresh_delay = refresh_delay
        self._refresh_stats = refresh_stats

    async def call_tool(
        self, record: McpToolRecord, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        self.calls.append((record.server_id, record.name, dict(arguments)))
        if not self._script:
            raise AssertionError(
                "tool_execute called the MCP server more often than allowed"
            )
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def refresh_server(self, server_id: str) -> Any:
        self.refreshed.append(server_id)
        if self._refresh_delay:
            await asyncio.sleep(self._refresh_delay)
        if self._refresh_error is not None:
            raise self._refresh_error
        if self._on_refresh is not None:
            await self._on_refresh()
        return self._refresh_stats

    def record_retry(self, *, tool_name: str, result: str) -> None:
        self.retries.append((tool_name, result))


class UnreadableStore:
    """The real store, whose catalog read starts failing after ``after`` reads.

    A database that disappears mid-call is not evidence that the row is gone: the
    sync completed and the read did not.  That third outcome is what these tests
    drive, because reporting it as tool_not_found tells the model to stop calling
    a tool that is probably still there.
    """

    def __init__(self, store: McpToolStore, *, after: int = 1) -> None:
        self._store = store
        self._after = after
        self.reads = 0

    async def get_by_call_name(
        self, call_name: str, owner_user_id: str
    ) -> McpToolRecord | None:
        self.reads += 1
        if self.reads > self._after:
            raise RuntimeError("database connection lost")
        return await self._store.get_by_call_name(call_name, owner_user_id)


class UnreadableServerStore:
    """The real server store, whose slug lookup fails: "could not look", not "none".

    A row that is missing locally leaves only the call name, so the executor asks
    the server table which server to refresh.  A database that cannot answer that
    question must not be reported as "no such server of yours".
    """

    def __init__(self, store: McpServerStore) -> None:
        self._store = store

    async def get(
        self, server_id: str, owner_user_id: str
    ) -> McpServerRecord | None:
        return await self._store.get(server_id, owner_user_id)

    async def get_by_name(
        self, owner_user_id: str, name: str
    ) -> McpServerRecord | None:
        raise RuntimeError("database connection lost")


async def _invoke(
    *,
    call_name: str = CALL_NAME,
    arguments: dict[str, Any] | None = None,
    retrieved: Sequence[str] = (CALL_NAME,),
    user_id: str = "u1",
    tool_call_id: str = "call-1",
) -> ToolMessage:
    """Run the executor the way ToolNode does, against whatever is published."""
    result = await tool_execute.coroutine(
        tool_name=call_name,
        arguments=dict(arguments or {}),
        runtime=SimpleNamespace(
            context={"user_id": user_id},
            state={"retrieved_tool_names": list(retrieved)},
        ),
        tool_call_id=tool_call_id,
    )
    assert isinstance(result, ToolMessage)
    assert result.name == TOOL_NAME
    assert result.tool_call_id == tool_call_id
    return result


async def _run(
    runtime: Any,
    *,
    call_name: str = CALL_NAME,
    arguments: dict[str, Any] | None = None,
    retrieved: Sequence[str] = (CALL_NAME,),
    user_id: str = "u1",
    tool_call_id: str = "call-1",
) -> ToolMessage:
    set_mcp_runtime(runtime)
    try:
        return await _invoke(
            call_name=call_name,
            arguments=arguments,
            retrieved=retrieved,
            user_id=user_id,
            tool_call_id=tool_call_id,
        )
    finally:
        reset_mcp_runtime()


def _payload(message: ToolMessage) -> dict[str, Any]:
    assert message.status == "error"
    assert isinstance(message.content, str)
    payload = json.loads(message.content)
    assert isinstance(payload, dict)
    return payload


def _meta(message: ToolMessage) -> dict[str, Any]:
    meta = message.additional_kwargs.get(TOOL_META_KEY)
    assert isinstance(meta, dict)
    return meta


# --------------------------------------------------------------------------- #
# The published runtime: the one seam between the tool and the application
# --------------------------------------------------------------------------- #


def test_the_runtime_slot_publishes_and_clears() -> None:
    """Task 8 publishes the real runtime here; every test tears it down."""
    assert get_mcp_runtime() is None
    runtime = object()
    set_mcp_runtime(runtime)
    assert get_mcp_runtime() is runtime
    reset_mcp_runtime()
    assert get_mcp_runtime() is None


@pytest.mark.asyncio
async def test_the_executor_reads_the_runtime_the_application_published(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, _ok(_text("ok")))

        assert executor_module.get_mcp_runtime is get_mcp_runtime
        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert runtime.calls == [(SERVER_ID, "search", {"query": "pytest"})]


@pytest.mark.asyncio
async def test_a_test_may_patch_the_getter_instead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The import is a module attribute, so monkeypatching it works too."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, _ok(_text("ok")))
        monkeypatch.setattr(executor_module, "get_mcp_runtime", lambda: runtime)

        message = await _invoke(arguments={"query": "pytest"})

    assert message.status == "success"
    assert runtime.calls == [(SERVER_ID, "search", {"query": "pytest"})]


# --------------------------------------------------------------------------- #
# Gates: retrieved names, then owner + callable + server state
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_name_that_was_never_retrieved_is_refused(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, _ok(_text("ok")))

        message = await _run(runtime, retrieved=())

    payload = _payload(message)
    assert payload["origin"] == "not_retrieved"
    assert payload["call_name"] == CALL_NAME
    assert payload["inner_name"] == "search", "the audit names the tool, not a slug"
    assert "tool_search" in payload["error"]
    assert runtime.calls == []
    assert runtime.refreshed == []
    assert _meta(message)["origin"] == "not_retrieved"
    assert _meta(message)["inner_name"] == "search"


@pytest.mark.asyncio
async def test_without_state_nothing_is_retrieved(tmp_path: Path) -> None:
    """Fail closed: a runtime that cannot show the run's names retrieves nothing."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, _ok(_text("ok")))

        message = await _run(runtime, retrieved=())

    assert _payload(message)["origin"] == "not_retrieved"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_require_retrieved_false_skips_the_gate_but_not_the_schema(
    tmp_path: Path,
) -> None:
    """tool_search.require_retrieved is the switch; safety still applies.

    With the gate deliberately loosened, an unretrieved call name still reaches
    the owner-scoped store and the stored schema, so the arguments are validated
    before anything runs upstream.
    """
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, _ok(_text("ok")))
        runtime.config.tool_search.require_retrieved = False

        message = await _run(runtime, retrieved=())

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_runtime_level_require_retrieved_override_wins(tmp_path: Path) -> None:
    """The policy may be published on the runtime instead of in tool_search config."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store, server_store, _ok(_text("ok")), require_retrieved=False
        )

        message = await _run(runtime, arguments={"query": "pytest"}, retrieved=())

    assert message.status == "success"
    assert runtime.calls == [(SERVER_ID, "search", {"query": "pytest"})]


@pytest.mark.asyncio
async def test_an_unpublished_runtime_is_unavailable(tmp_path: Path) -> None:
    """No runtime is not "no tools": it is a deployment that cannot execute."""
    message = await _invoke(arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "mcp_unavailable"
    assert payload["retryable"] is False
    assert payload["inner_name"] == "search"
    assert "MCP" in payload["error"]
    assert "ContextForge" not in payload["error"]


@pytest.mark.asyncio
async def test_a_half_built_runtime_is_unavailable(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, _server_store):
        runtime = FakeRuntime(store, None, _ok(_text("ok")))

        message = await _run(runtime, arguments={"query": "pytest"})

    assert _payload(message)["origin"] == "mcp_unavailable"


# --------------------------------------------------------------------------- #
# The direct call: the owning server, the remote name, the arguments
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_call_goes_to_the_server_that_owns_the_tool(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, _ok(_text("found 3 repos")))

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert runtime.calls == [(SERVER_ID, "search", {"query": "pytest"})], (
        "the record's server id, the remote name and the arguments are the call"
    )
    assert runtime.refreshed == []
    assert runtime.retries == []


@pytest.mark.asyncio
async def test_the_result_is_stamped_with_the_inner_name_tool_id_and_call_name(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            _ok(
                _text("found 3 repos"),
                mcp_types.ImageContent(type="image", data="aGk=", mimeType="image/png"),
                mcp_types.EmbeddedResource(
                    type="resource",
                    resource=mcp_types.TextResourceContents(
                        uri="file:///notes.txt", mimeType="text/plain", text="body"
                    ),
                ),
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert message.content == [
        {"type": "text", "text": "found 3 repos"},
        {"type": "image", "base64": "aGk=", "mime_type": "image/png"},
        {"type": "text", "text": "file:///notes.txt\nbody"},
    ]
    meta = _meta(message)
    assert meta["inner_name"] == "search"
    assert meta["call_name"] == CALL_NAME
    assert meta["tool_id"] == tool_id_for(SERVER_ID, "search")
    assert meta["status"] == "success"


@pytest.mark.asyncio
async def test_a_structured_result_without_content_still_answers(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            mcp_types.CallToolResult(
                content=[],
                structuredContent={"total": 2},
                isError=False,
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert message.content == [{"type": "text", "text": '{"total": 2}'}]


@pytest.mark.asyncio
async def test_a_stored_schema_that_is_missing_still_defers(tmp_path: Path) -> None:
    """No schema is not a broken schema: with nothing to check, the server decides."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool(schema={}))
        runtime = FakeRuntime(store, server_store, _ok(_text("ok")))

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert len(runtime.calls) == 1


# --------------------------------------------------------------------------- #
# Refusals: owner, callability, and the server that explains why
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_another_users_tool_is_not_executed(tmp_path: Path) -> None:
    """The lookup is owner-scoped: a guessed call name is not a capability."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store, owner="u2")
        await _store_tool(store, _tool(owner="u2"))
        runtime = FakeRuntime(store, server_store, _ok(_text("ok")))

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert runtime.calls == []
    assert runtime.refreshed == [], "another owner's server is not ours to refresh"


@pytest.mark.asyncio
async def test_a_tool_the_sync_no_longer_sees_is_refused_and_refreshed_once(
    tmp_path: Path,
) -> None:
    """A stored row that stopped qualifying keeps its definition, not its life."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool(), seen=False)
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert payload["inner_name"] == "search", "the audit still names the tool"
    assert runtime.calls == []
    assert runtime.refreshed == [SERVER_ID], "the server was healthy, so one refresh"
    assert "refreshed" in payload["error"].casefold()


@pytest.mark.asyncio
async def test_a_disabled_server_refuses_the_call(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store, enabled=False)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "server_disabled"
    assert payload["retryable"] is False
    assert SERVER_NAME in payload["error"]
    assert runtime.calls == []
    assert runtime.refreshed == [], "refreshing a disabled server is meaningless"
    assert _meta(message)["origin"] == "server_disabled"


@pytest.mark.asyncio
async def test_an_unreachable_server_refuses_the_call(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store, reachable=False)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "server_unavailable"
    assert payload["retryable"] is False
    assert SERVER_NAME in payload["error"]
    assert runtime.calls == []
    assert runtime.refreshed == []


@pytest.mark.asyncio
async def test_a_tool_whose_server_was_deleted_is_not_executed(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert runtime.calls == []
    assert runtime.refreshed == [], "a deleted server has nothing to refresh"


# --------------------------------------------------------------------------- #
# Local validation (design 9.1 A)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_local_validation_failure_returns_the_new_schema_without_retrying(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())

        async def refresh() -> None:
            await _store_tool(store, _tool(schema=TEXT_SCHEMA))

        runtime = FakeRuntime(store, server_store, on_refresh=refresh)

        message = await _run(runtime, arguments={"limit": 3})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert payload["schema_changed"] is True
    assert payload["input_schema"] == TEXT_SCHEMA
    assert "query" in payload["error"]
    assert runtime.calls == [], "a local validation failure never reaches the server"
    assert runtime.refreshed == [SERVER_ID]
    assert _meta(message)["origin"] == "local_validation"


@pytest.mark.asyncio
async def test_local_validation_failure_with_an_unchanged_schema_returns_the_error(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert "schema_changed" not in payload
    assert "input_schema" not in payload
    assert "'query' is a required property" in payload["error"]
    assert runtime.calls == []
    assert runtime.refreshed == [SERVER_ID]


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_local_validation_error(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store, server_store, refresh_error=RuntimeError("server unreachable")
        )

        message = await _run(runtime, arguments={})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert "input_schema" not in payload
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_slow_refresh_is_bounded_by_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(executor_module, "DEFAULT_REFRESH_TIMEOUT_SECONDS", 0.05)
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())

        async def refresh() -> None:
            await _store_tool(store, _tool(schema=TEXT_SCHEMA))

        runtime = FakeRuntime(
            store, server_store, on_refresh=refresh, refresh_delay=5.0
        )

        loop = asyncio.get_running_loop()
        started = loop.time()
        message = await _run(runtime, arguments={})
        elapsed = loop.time() - started

    assert _payload(message)["origin"] == "local_validation"
    assert elapsed < 1.0, (
        "the refresh budget, not the refresh, decides how long this takes"
    )
    assert "input_schema" not in _payload(message), (
        "a cancelled refresh changed nothing"
    )
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_retry_on_tool_error_false_does_not_refresh_a_validation_failure(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, retry_on_tool_error=False)

        message = await _run(runtime, arguments={})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert "input_schema" not in payload
    assert runtime.refreshed == []
    assert runtime.calls == []


# --------------------------------------------------------------------------- #
# Not found (design 9.1 B): refresh the owning server, then retry exactly once
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_stale_call_name_is_refreshed_then_retried_once(tmp_path: Path) -> None:
    """The row was missing locally; the refresh brings it back and we run it."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)

        async def refresh() -> None:
            await _store_tool(store, _tool())

        runtime = FakeRuntime(
            store, server_store, _ok(_text("found 3 repos")), on_refresh=refresh
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert runtime.refreshed == [SERVER_ID], "the server named by the call name"
    assert runtime.calls == [(SERVER_ID, "search", {"query": "pytest"})]
    assert runtime.retries == [(CALL_NAME, "success")]


@pytest.mark.asyncio
async def test_a_missing_tool_is_reported_after_one_refresh(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert CALL_NAME in payload["error"]
    assert payload["inner_name"] == "search", "parsed out of mcp__<server>__<tool>"
    assert "tool_search" in payload["error"]
    assert payload["retryable"] is False, (
        "the refresh already ran and the reply says do not repeat this call"
    )
    assert "Do not repeat this call" in payload["error"]
    assert runtime.refreshed == [SERVER_ID], "exactly one refresh"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_call_name_that_names_no_server_is_not_refreshed(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert "nothing to refresh" in payload["error"]
    assert runtime.refreshed == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_retry_on_tool_error_false_does_not_refresh_a_missing_tool(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        runtime = FakeRuntime(store, server_store, retry_on_tool_error=False)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert "retry_on_tool_error" in payload["error"]
    assert runtime.refreshed == []
    assert runtime.retries == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_refresh_that_reports_an_error_did_not_refresh(tmp_path: Path) -> None:
    """A sync reports its failure in stats instead of raising."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        runtime = FakeRuntime(
            store,
            server_store,
            refresh_stats=SimpleNamespace(error="admin_read_failed"),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert "did not complete" in payload["error"]
    assert runtime.calls == []
    assert runtime.refreshed == [SERVER_ID]


@pytest.mark.asyncio
async def test_an_index_only_refresh_failure_still_uses_the_committed_snapshot(
    tmp_path: Path,
) -> None:
    """index_failed committed the DB snapshot; only the vector index is degraded."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)

        async def refresh() -> None:
            await _store_tool(store, _tool())

        runtime = FakeRuntime(
            store,
            server_store,
            _ok(_text("found 3 repos")),
            on_refresh=refresh,
            refresh_stats=SimpleNamespace(error="index_unavailable", index_failed=True),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert runtime.refreshed == [SERVER_ID]
    assert runtime.retries == [(CALL_NAME, "success")]


# --------------------------------------------------------------------------- #
# Upstream isError: the result is data, and only some of it is refreshable
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upstream_not_found_is_refreshed_and_retried_once(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store, server_store, _error("tool not found: search"), _ok(_text("ok"))
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert [call[1] for call in runtime.calls] == ["search", "search"]
    assert runtime.refreshed == [SERVER_ID]
    assert runtime.retries == [(CALL_NAME, "success")]


@pytest.mark.asyncio
async def test_a_second_not_found_is_not_retried_again(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store, server_store, _error("tool not found"), _error("tool not found")
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert _payload(message)["origin"] == "tool_not_found"
    assert len(runtime.calls) == 2
    assert runtime.refreshed == [SERVER_ID], "one refresh, one retry"
    assert runtime.retries == [(CALL_NAME, "failed")]


@pytest.mark.asyncio
async def test_retry_on_tool_error_false_does_not_retry_an_upstream_not_found(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            _error("tool not found: search"),
            retry_on_tool_error=False,
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert _payload(message)["origin"] == "tool_not_found"
    assert len(runtime.calls) == 1, "the upstream call is never repeated"
    assert runtime.refreshed == []
    assert runtime.retries == []


@pytest.mark.asyncio
async def test_a_failed_refresh_on_upstream_not_found_keeps_the_catalog_answer(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            _error("tool not found: search"),
            refresh_error=RuntimeError("refresh exploded"),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert "did not complete" in payload["error"]
    assert len(runtime.calls) == 1, (
        "a refresh that never happened cannot justify a retry"
    )
    assert runtime.refreshed == [SERVER_ID]


@pytest.mark.asyncio
async def test_upstream_schema_mismatch_returns_the_new_schema_without_retrying(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())

        async def refresh() -> None:
            await _store_tool(store, _tool(schema=TEXT_SCHEMA))

        runtime = FakeRuntime(
            store,
            server_store,
            _error("invalid params: schema validation failed"),
            on_refresh=refresh,
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "schema_mismatch"
    assert payload["schema_changed"] is True
    assert payload["input_schema"] == TEXT_SCHEMA
    assert len(runtime.calls) == 1, "the old arguments are never retried"
    assert runtime.refreshed == [SERVER_ID]


@pytest.mark.asyncio
async def test_upstream_schema_mismatch_without_a_change_returns_the_error(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store, server_store, _error("invalid arguments for tool")
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "invalid_arguments"
    assert "input_schema" not in payload
    assert len(runtime.calls) == 1
    assert runtime.refreshed == [SERVER_ID]


@pytest.mark.asyncio
async def test_an_upstream_error_result_is_an_error_tool_message(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(store, server_store, _error("unauthorized: bad token"))

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_error"
    assert payload["error"] == "unauthorized: bad token"
    assert payload["inner_name"] == "search"
    assert payload["tool_id"] == tool_id_for(SERVER_ID, "search")
    assert _meta(message)["status"] == "error"
    assert runtime.refreshed == [], "an upstream answer is never refreshed or retried"


@pytest.mark.asyncio
async def test_an_upstream_5xx_result_is_not_retried(tmp_path: Path) -> None:
    """isError with a 5xx/timeout marker: the result is uncertain, so no retry."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store, server_store, _error("internal server error: 502")
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_error"
    assert len(runtime.calls) == 1
    assert runtime.refreshed == []


# --------------------------------------------------------------------------- #
# No retry: timeout, transport, protocol, and the JSON-RPC tool error
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_timeout_is_neither_refreshed_nor_retried(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            McpServerTimeout("MCP server 's1' timed out during tools/call"),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "timeout"
    assert payload["retryable"] is False
    assert len(runtime.calls) == 1
    assert runtime.refreshed == []


@pytest.mark.asyncio
async def test_an_unreachable_server_is_neither_refreshed_nor_retried(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            McpServerUnavailable("MCP server 's1' is unavailable (ConnectError)"),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "server_unavailable"
    assert len(runtime.calls) == 1
    assert runtime.refreshed == []


@pytest.mark.asyncio
async def test_a_protocol_failure_is_neither_refreshed_nor_retried(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            McpProtocolError(
                "MCP server 's1' returned a malformed tools/call response"
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "transport_error"
    assert len(runtime.calls) == 1
    assert runtime.refreshed == []


@pytest.mark.asyncio
async def test_a_json_rpc_tool_error_is_reported_without_retrying(
    tmp_path: Path,
) -> None:
    """McpToolError is the path where no result exists: tool_error, never retried.

    Ruling: a JSON-RPC rejection can be a remote -32601 (unknown tool) or -32602
    (invalid params), but repeating a call the server already refused is exactly
    what design 9.1 forbids, so it stays final.  The safe refresh paths are the
    local not-found and the upstream isError ones; cost if wrong: a remote
    "unknown tool" JSON-RPC error does not auto-refresh.
    """
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            McpToolError(
                "MCP server 's1' rejected the tool call (JSON-RPC error -32603)"
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_error"
    assert "-32603" in payload["error"]
    assert len(runtime.calls) == 1
    assert runtime.refreshed == []


@pytest.mark.asyncio
async def test_a_tool_error_that_carries_a_result_reports_its_text(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            McpToolError("rejected", result=_error("upstream exploded")),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_error"
    assert payload["error"] == "upstream exploded"
    assert len(runtime.calls) == 1
    assert runtime.refreshed == []


# --------------------------------------------------------------------------- #
# The third state: the sync worked and the catalog read then failed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_re_read_that_raises_is_unavailable_not_a_refreshed_not_found(
    tmp_path: Path,
) -> None:
    """Nothing is known about the row here: the refresh ran, the read raised.

    The model must not be told the tool is gone, because it probably still exists.
    """
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        unreadable = UnreadableStore(store, after=1)
        runtime = FakeRuntime(unreadable, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "mcp_unavailable"
    assert payload["retryable"] is False
    assert "not found" not in payload["error"].casefold()
    assert "could not be read" in payload["error"]
    assert unreadable.reads == 2, "one lookup, one re-read after the refresh"
    assert runtime.refreshed == [SERVER_ID]
    assert runtime.calls == [], "an unreadable catalog never reaches the server"
    assert _meta(message)["origin"] == "mcp_unavailable"


@pytest.mark.asyncio
async def test_an_upstream_not_found_whose_re_read_raises_is_unavailable(
    tmp_path: Path,
) -> None:
    """The server said not found, then the re-read raised: still not a retry."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        unreadable = UnreadableStore(store, after=1)
        runtime = FakeRuntime(
            unreadable, server_store, _error("tool not found: search")
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "mcp_unavailable"
    assert payload["retryable"] is False
    assert len(runtime.calls) == 1, "no retry without a catalog read that succeeded"
    assert runtime.retries == []


# --------------------------------------------------------------------------- #
# Fail closed: a schema this process cannot check (design 9.3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_schema_that_cannot_resolve_remotely_is_refused(
    tmp_path: Path,
) -> None:
    """A remote $ref is never fetched: the tool is refused, not deferred.

    Fetching it would make an upstream tool author's schema trigger a request from
    the gateway (SSRF); skipping validation would send arguments nothing checked.
    """
    schema = {
        "type": "object",
        "properties": {"query": {"$ref": "https://example.com/args.json"}},
        "required": ["query"],
    }
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool(schema=schema))
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "schema_unresolved"
    assert payload["retryable"] is False
    assert "example.com" in payload["error"], "the reason is reported, not swallowed"
    assert runtime.calls == [], "unvalidated arguments never leave the process"
    assert runtime.refreshed == []
    assert _meta(message)["origin"] == "schema_unresolved"


@pytest.mark.asyncio
async def test_a_schema_that_does_not_compile_is_refused(tmp_path: Path) -> None:
    """The same rule for a schema jsonschema itself rejects."""
    schema = {
        "type": "object",
        "properties": {"query": {"type": "nonsense"}},
        "required": ["query"],
    }
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool(schema=schema))
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "schema_unresolved"
    assert "UnknownType" in payload["error"]
    assert runtime.calls == []
    assert runtime.refreshed == []


# --------------------------------------------------------------------------- #
# Nothing secret in a failure: no header, no token, no endpoint
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_transport_failure_never_carries_a_header_a_token_or_a_url(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            McpServerUnavailable(
                "connect failed at https://user:secret@mcp.example.com/mcp"
                "?token=sk-live-123 (Authorization: Bearer sk-live-123)"
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    error = _payload(message)["error"]
    assert "sk-live-123" not in error
    assert "secret@" not in error
    assert "example.com" not in error
    assert "****" in error, "the credential is redacted, not silently dropped"


@pytest.mark.asyncio
async def test_a_tool_error_result_is_scrubbed_of_credentials(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            McpToolError(
                "rejected", result=_error("Authorization: Bearer sk-live-999")
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    error = _payload(message)["error"]
    assert "sk-live-999" not in error
    assert "****" in error


# --------------------------------------------------------------------------- #
# One refresh per call: a recovered row has already spent the budget
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_recovered_row_that_fails_validation_does_not_refresh_again(
    tmp_path: Path,
) -> None:
    """Missing row -> refresh -> recovered row -> local validation: re-read only."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)

        async def refresh() -> None:
            await _store_tool(store, _tool())

        runtime = FakeRuntime(store, server_store, on_refresh=refresh)

        message = await _run(runtime, arguments={})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert "'query' is a required property" in payload["error"]
    assert runtime.refreshed == [SERVER_ID], "one refresh per call, never two"
    assert runtime.calls == []
    assert "input_schema" not in payload, "the re-read saw the same schema"


@pytest.mark.asyncio
async def test_a_recovered_row_whose_call_fails_does_not_refresh_again(
    tmp_path: Path,
) -> None:
    """Missing row -> refresh -> recovered -> upstream not found: re-read, one retry."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)

        async def refresh() -> None:
            await _store_tool(store, _tool())

        runtime = FakeRuntime(
            store,
            server_store,
            _error("tool not found: search"),
            _ok(_text("ok")),
            on_refresh=refresh,
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    assert message.status == "success"
    assert runtime.refreshed == [SERVER_ID], "the second fallback only re-reads"
    assert len(runtime.calls) == 2, "one call, one retry - no second sync"
    assert runtime.retries == [(CALL_NAME, "success")]


# --------------------------------------------------------------------------- #
# Upstream text is data, and data is scrubbed before the model reads it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_upstream_error_text_is_scrubbed_before_the_model_sees_it(
    tmp_path: Path,
) -> None:
    """An upstream tool can echo the request it saw: no header, token or URL out."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            _error(
                "request failed: Authorization: Bearer sk-live-777 at "
                "https://user:secret@mcp.example.com/mcp?token=sk-live-777"
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_error"
    assert "sk-live-777" not in payload["error"]
    assert "secret@" not in payload["error"]
    assert "example.com" not in payload["error"]
    assert "****" in payload["error"], "the credential is redacted, not dropped"


@pytest.mark.asyncio
async def test_a_retried_upstream_error_text_is_scrubbed_too(tmp_path: Path) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        await _store_tool(store, _tool())
        runtime = FakeRuntime(
            store,
            server_store,
            _error("tool not found"),
            _error(
                "tool not found: https://user:secret@mcp.example.com/mcp?token=sk-9"
            ),
        )

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert "sk-9" not in payload["error"]
    assert "example.com" not in payload["error"]
    assert len(runtime.calls) == 2, "the retry still happened; only the text changed"


# --------------------------------------------------------------------------- #
# A missing row's server decides: its state, not just its id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_missing_tool_on_a_disabled_server_is_refused_without_refreshing(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store, enabled=False)
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "server_disabled"
    assert payload["inner_name"] == "search"
    assert runtime.refreshed == [], "a disabled server is not worth a sync"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_missing_tool_on_an_unreachable_server_is_refused_without_refreshing(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store, reachable=False)
        runtime = FakeRuntime(store, server_store)

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "server_unavailable"
    assert payload["inner_name"] == "search"
    assert runtime.refreshed == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_server_lookup_that_raises_is_unavailable_not_unknown_server(
    tmp_path: Path,
) -> None:
    """The missing row's server table could not be read: "could not look" is not
    "no such server of yours"."""
    async with catalog(tmp_path) as (store, server_store):
        await _server(server_store)
        runtime = FakeRuntime(store, UnreadableServerStore(server_store))

        message = await _run(runtime, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "mcp_unavailable"
    assert "could not be read" in payload["error"]
    assert "nothing to refresh" not in payload["error"]
    assert runtime.refreshed == []
    assert runtime.calls == []


# --------------------------------------------------------------------------- #
# The retryable flag, pinned for every origin the executor can report
# --------------------------------------------------------------------------- #

#: The decision, per origin.  False is the default for anything that already had
#: its one fallback: tool_not_found was refreshed and retried once and its own
#: wording says "do not repeat this call" (9.1 B.4), so a True there would
#: contradict the message the model reads.  server_disabled and server_unavailable
#: are refusals too - a disabled or unreachable server is not fixed by re-sending
#: the same call.
RETRYABLE_BY_ORIGIN: dict[str, bool] = {
    executor_module.ORIGIN_NOT_RETRIEVED: True,
    executor_module.ORIGIN_LOCAL_VALIDATION: True,
    executor_module.ORIGIN_SCHEMA_MISMATCH: True,
    executor_module.ORIGIN_INVALID_ARGUMENTS: True,
    executor_module.ORIGIN_TOOL_NOT_FOUND: False,
    executor_module.ORIGIN_TRANSPORT_ERROR: False,
    executor_module.ORIGIN_TIMEOUT: False,
    executor_module.ORIGIN_TOOL_ERROR: False,
    executor_module.ORIGIN_MCP_UNAVAILABLE: False,
    executor_module.ORIGIN_SERVER_UNAVAILABLE: False,
    executor_module.ORIGIN_SERVER_DISABLED: False,
    executor_module.ORIGIN_SCHEMA_UNRESOLVED: False,
}


@pytest.mark.parametrize(("origin", "expected"), sorted(RETRYABLE_BY_ORIGIN.items()))
def test_the_retryable_flag_is_pinned_per_origin(
    origin: str, expected: bool
) -> None:
    reply = executor_module._error_reply(
        "call-1", call_name=CALL_NAME, origin=origin, message="boom"
    )
    assert _payload(reply)["retryable"] is expected


def test_every_origin_the_executor_reports_has_a_retryable_decision() -> None:
    reported = {
        value
        for name, value in vars(executor_module).items()
        if name.startswith("ORIGIN_") and isinstance(value, str)
    }
    assert reported == set(RETRYABLE_BY_ORIGIN), (
        "a new origin needs an explicit retryable decision, not a default"
    )
