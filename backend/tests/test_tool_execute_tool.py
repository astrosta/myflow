"""tool_execute: retrieve-gated ContextForge MCP execution and its fallback.

tool_search hands the model a ``cf__<name>`` call name, its verbatim schema and
the arguments it must send; tool_execute is the only thing that actually runs
the tool, and it runs it through ContextForge MCP tools/call with the original
(unprefixed) name.  These tests pin the contract around that call:

* two gates before anything leaves the process - the call name must be in
  ``ThreadState.retrieved_tool_names`` for this run, and the row must be
  owner-scoped *and* callable in ``contextforge_tools``;
* local validation with Draft202012Validator, with remote $ref retrieval disabled
  so a stored schema can never make the gateway fetch a URL - and, because the
  check cannot be skipped, a schema that will not compile or resolve is refused
  instead of deferred to ContextForge (design 9.3: fail closed);
* the failure fallback of design 9.1: a local validation failure and an upstream
  invalid-params/schema-mismatch refresh the catalog and answer with the new
  schema but never retry, a stale or missing tool refreshes and retries exactly
  once, and a timeout/5xx/transport error neither refreshes nor retries;
* the reply is a ToolMessage that carries the inner name, tool_id and call name
  in ``myflow_tool_meta``, so an audit sees the tool that ran, not tool_execute;
* a refresh that completed but whose catalog re-read then raised is
  ``contextforge_unavailable``, never a refreshed ``tool_not_found``: the third
  outcome exists because "the row is gone" and "I could not look" are different
  facts, and only one of them tells the model to stop calling the tool.
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
from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.mcp_client import ContextForgeMcpClient
from agentkit.contextforge.models import ContextForgeTimeout, ContextForgeUnavailable
from agentkit.contextforge.runtime import (
    build_contextforge_runtime,
    configure_contextforge_runtime,
    reset_contextforge_runtime,
)
from agentkit.contextforge.store import (
    ContextForgeToolRecord,
    ContextForgeToolStore,
    call_name_for,
)
from agentkit.contextforge.sync import ContextForgeSyncStats
from agentkit.persistence.schema import Base
from agentkit.tools.builtin.tool_execute import TOOL_NAME, tool_execute

CALL_NAME = "cf__github_search"

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
def _reset_runtime() -> Any:
    reset_contextforge_runtime()
    yield
    reset_contextforge_runtime()


@asynccontextmanager
async def catalog(tmp_path: Path) -> AsyncIterator[ContextForgeToolStore]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "contextforge.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield ContextForgeToolStore(
            async_sessionmaker(engine, expire_on_commit=False)
        )
    finally:
        await engine.dispose()


def _record(
    name: str = "github_search",
    *,
    owner: str = "u1",
    schema: dict[str, Any] | None = None,
) -> ContextForgeToolRecord:
    return ContextForgeToolRecord(
        id="tool-" + name,
        name=name,
        call_name=call_name_for(name),
        display_name=name,
        description="Search GitHub code",
        input_schema=dict(QUERY_SCHEMA) if schema is None else schema,
        tags=("github",),
        parameters=("query",),
        owner_user_id=owner,
        enabled=True,
        reachable=True,
        content=name,
    )


async def _store_record(
    store: ContextForgeToolStore,
    record: ContextForgeToolRecord,
    *,
    seen: bool = True,
) -> None:
    """Write one row; ``seen=False`` stores it without making it callable."""
    await store.upsert_snapshot([record], [record.name] if seen else [])


def _ok(*blocks: Any) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=list(blocks), isError=False)


def _text(text: str) -> mcp_types.TextContent:
    return mcp_types.TextContent(type="text", text=text)


def _error(text: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[_text(text)], isError=True)


class FakeMcp:
    """The ContextForge MCP slice tool_execute uses, scripted per call.

    An empty script raises instead of repeating itself: a test that says "one
    call" must fail if the executor calls twice.
    """

    def __init__(self, *script: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._script = list(script)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> mcp_types.CallToolResult:
        self.calls.append(
            {"name": name, "arguments": dict(arguments), "timeout": timeout}
        )
        if not self._script:
            raise AssertionError("tool_execute called ContextForge more often than allowed")
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class UnreadableStore:
    """The real store, whose catalog read starts failing after ``after`` reads.

    A database that disappears mid-call is not evidence that the row is gone:
    the sync completed and the read did not.  That third outcome is what these
    tests drive, because reporting it as tool_not_found tells the model to stop
    calling a tool that is probably still there.
    """

    def __init__(self, store: ContextForgeToolStore, *, after: int = 1) -> None:
        self._store = store
        self._after = after
        self.reads = 0

    async def get_by_call_name(
        self, call_name: str, owner_user_id: str
    ) -> ContextForgeToolRecord | None:
        self.reads += 1
        if self.reads > self._after:
            raise RuntimeError("database connection lost")
        return await self._store.get_by_call_name(call_name, owner_user_id)


class FakeExecutionRuntime:
    """The process runtime, shaped like ContextForgeRuntime: store + MCP + refresh."""

    def __init__(
        self,
        store: ContextForgeToolStore,
        mcp: FakeMcp,
        *,
        on_refresh: Any = None,
        refresh_error: Exception | None = None,
        refresh_delay: float = 0.0,
        refresh_timeout: float = 1.0,
    ) -> None:
        self.store = store
        self.mcp_client = mcp
        self.config = ContextForgeConfig(
            enabled=True,
            base_url="http://cf",
            admin_token="token",
            search={"enabled": False},
            sync={"tool_error_refresh_timeout_seconds": refresh_timeout},
        )
        self.refresh_reasons: list[str] = []
        self.retries: list[tuple[str, str]] = []
        self._on_refresh = on_refresh
        self._refresh_error = refresh_error
        self._refresh_delay = refresh_delay

    async def refresh_now(self, reason: str = "tool_error") -> ContextForgeSyncStats:
        self.refresh_reasons.append(reason)
        if self._refresh_delay:
            await asyncio.sleep(self._refresh_delay)
        if self._refresh_error is not None:
            raise self._refresh_error
        if self._on_refresh is not None:
            await self._on_refresh()
        return ContextForgeSyncStats()

    def record_retry(self, *, tool_name: str, result: str) -> None:
        self.retries.append((tool_name, result))


class StatsErrorRuntime(FakeExecutionRuntime):
    """A refresh that reports a failure in its stats, as the real runtime does.

    ContextForgeRuntime.refresh_now swallows an upstream, database or budget
    failure into ContextForgeSyncStats.error instead of raising, so a normal
    return is not evidence the snapshot was replaced.  index_failed is the
    exception: the DB snapshot was committed and only the Milvus index failed.
    """

    def __init__(
        self,
        store: ContextForgeToolStore,
        mcp: FakeMcp,
        *,
        on_refresh: Any = None,
        index_failed: bool = False,
    ) -> None:
        super().__init__(store, mcp, on_refresh=on_refresh)
        self.index_failed = index_failed

    async def refresh_now(self, reason: str = "tool_error") -> ContextForgeSyncStats:
        self.refresh_reasons.append(reason)
        if self._on_refresh is not None:
            await self._on_refresh()
        return ContextForgeSyncStats(
            error="index_unavailable" if self.index_failed else "admin_read_failed",
            index_failed=self.index_failed,
        )


async def _run(
    execution: Any,
    *,
    call_name: str = CALL_NAME,
    arguments: dict[str, Any] | None = None,
    retrieved: Sequence[str] = (CALL_NAME,),
    user_id: str = "u1",
    tool_call_id: str = "call-1",
) -> ToolMessage:
    configure_contextforge_runtime(execution)
    try:
        result = await tool_execute.coroutine(
            tool_name=call_name,
            arguments=dict(arguments or {}),
            runtime=SimpleNamespace(
                context={"user_id": user_id},
                state={"retrieved_tool_names": list(retrieved)},
            ),
            tool_call_id=tool_call_id,
        )
    finally:
        reset_contextforge_runtime()
    assert isinstance(result, ToolMessage)
    assert result.name == TOOL_NAME
    assert result.tool_call_id == tool_call_id
    return result


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
# Gates: retrieved names, then owner + callable
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_name_that_was_never_retrieved_is_refused(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, retrieved=())

    payload = _payload(message)
    assert payload["origin"] == "not_retrieved"
    assert payload["call_name"] == CALL_NAME
    assert "tool_search" in payload["error"]
    assert mcp.calls == []
    assert execution.refresh_reasons == []
    assert _meta(message)["origin"] == "not_retrieved"


@pytest.mark.asyncio
async def test_another_users_tool_is_not_executed(tmp_path: Path) -> None:
    """The DB read is owner-scoped: a guessed call_name is not a capability."""
    async with catalog(tmp_path) as store:
        await _store_record(store, _record(owner="u2"))
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution)

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert mcp.calls == []
    assert execution.refresh_reasons == ["call_error"]


@pytest.mark.asyncio
async def test_an_uncallable_tool_is_not_executed(tmp_path: Path) -> None:
    """A stored row that stopped qualifying keeps its definition but not its life."""
    async with catalog(tmp_path) as store:
        await _store_record(store, _record(), seen=False)
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution)

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_without_state_nothing_is_retrieved(tmp_path: Path) -> None:
    """Fail closed: a runtime that cannot show the run's names retrieves nothing."""
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        execution = FakeExecutionRuntime(store, FakeMcp())

        message = await _run(execution, retrieved=())

    assert _payload(message)["origin"] == "not_retrieved"


@pytest.mark.asyncio
async def test_require_retrieved_false_skips_the_gate_but_not_the_schema(
    tmp_path: Path,
) -> None:
    """contextforge.search.require_retrieved is the switch; safety still applies.

    With the gate deliberately loosened, an unretrieved call name reaches the
    owner-scoped store and the stored schema, so the arguments are still
    validated before anything runs upstream.
    """
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)
        execution.config.search.require_retrieved = False

        message = await _run(execution, retrieved=())

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert mcp.calls == []


# --------------------------------------------------------------------------- #
# Local validation (design 9.1 A)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_local_validation_failure_returns_the_new_schema_without_retrying(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())

        async def refresh() -> None:
            await store.upsert_snapshot([_record(schema=TEXT_SCHEMA)], ["github_search"])

        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp, on_refresh=refresh)

        message = await _run(execution, arguments={"limit": 3})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert payload["schema_changed"] is True
    assert payload["input_schema"] == TEXT_SCHEMA
    assert "query" in payload["error"]
    assert mcp.calls == [], "a local validation failure never reaches ContextForge"
    assert execution.refresh_reasons == ["call_error"]
    assert _meta(message)["origin"] == "local_validation"


@pytest.mark.asyncio
async def test_local_validation_failure_with_an_unchanged_schema_returns_the_error(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert "schema_changed" not in payload
    assert "input_schema" not in payload
    assert "'query' is a required property" in payload["error"]
    assert mcp.calls == []
    assert execution.refresh_reasons == ["call_error"]


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_local_validation_error(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(
            store, mcp, refresh_error=RuntimeError("contextforge unreachable")
        )

        message = await _run(execution, arguments={})

    payload = _payload(message)
    assert payload["origin"] == "local_validation"
    assert "input_schema" not in payload
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_a_slow_refresh_is_bounded_by_the_configured_timeout(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(
            store, mcp, refresh_delay=5.0, refresh_timeout=0.05
        )

        loop = asyncio.get_running_loop()
        started = loop.time()
        message = await _run(execution, arguments={})
        elapsed = loop.time() - started

    assert _payload(message)["origin"] == "local_validation"
    assert elapsed < 1.0, "the refresh budget, not the refresh, decides how long this takes"
    assert mcp.calls == []


# --------------------------------------------------------------------------- #
# Not found (design 9.1 B): refresh, then retry exactly once
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_stale_call_name_is_refreshed_then_retried_once(tmp_path: Path) -> None:
    """The row was missing locally; the refresh brings it back and we run it."""
    async with catalog(tmp_path) as store:

        async def refresh() -> None:
            await _store_record(store, _record())

        mcp = FakeMcp(_ok(_text("found 3 repos")))
        execution = FakeExecutionRuntime(store, mcp, on_refresh=refresh)

        message = await _run(execution, arguments={"query": "pytest"})

    assert message.status == "success"
    assert execution.refresh_reasons == ["call_error"]
    assert mcp.calls == [
        {"name": "github_search", "arguments": {"query": "pytest"}, "timeout": None}
    ]
    assert execution.retries == [(CALL_NAME, "success")]


@pytest.mark.asyncio
async def test_a_missing_tool_is_reported_after_one_refresh(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert CALL_NAME in payload["error"]
    assert "tool_search" in payload["error"]
    assert payload["retryable"] is False, (
        "the refresh already ran and the reply says do not repeat this call"
    )
    assert "Do not repeat this call" in payload["error"]
    assert execution.refresh_reasons == ["call_error"], "exactly one refresh"
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_upstream_not_found_is_refreshed_and_retried_once(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(_error("tool not found: github_search"), _ok(_text("ok")))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    assert message.status == "success"
    assert [call["name"] for call in mcp.calls] == ["github_search", "github_search"]
    assert execution.refresh_reasons == ["call_error"]
    assert execution.retries == [(CALL_NAME, "success")]


@pytest.mark.asyncio
async def test_a_second_not_found_is_not_retried_again(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(_error("tool not found"), _error("tool not found"))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    assert _payload(message)["origin"] == "tool_not_found"
    assert len(mcp.calls) == 2
    assert execution.refresh_reasons == ["call_error"], "one refresh, one retry"
    assert execution.retries == [(CALL_NAME, "failed")]


@pytest.mark.asyncio
async def test_a_failed_refresh_on_upstream_not_found_keeps_the_catalog_answer(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(_error("tool not found: github_search"))
        execution = FakeExecutionRuntime(
            store, mcp, refresh_error=RuntimeError("contextforge unreachable")
        )

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert "did not complete" in payload["error"]
    assert len(mcp.calls) == 1, "a refresh that never happened cannot justify a retry"
    assert execution.refresh_reasons == ["call_error"]


@pytest.mark.asyncio
async def test_a_refresh_that_reports_an_error_did_not_refresh(tmp_path: Path) -> None:
    """refresh_now reports failure in stats.error instead of raising."""
    async with catalog(tmp_path) as store:
        mcp = FakeMcp()
        execution = StatsErrorRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_not_found"
    assert "did not complete" in payload["error"]
    assert mcp.calls == []
    assert execution.refresh_reasons == ["call_error"]


@pytest.mark.asyncio
async def test_an_index_only_refresh_failure_still_uses_the_committed_snapshot(
    tmp_path: Path,
) -> None:
    """index_failed committed the DB snapshot; only the Milvus index is degraded."""
    async with catalog(tmp_path) as store:

        async def refresh() -> None:
            await _store_record(store, _record())

        mcp = FakeMcp(_ok(_text("found 3 repos")))
        execution = StatsErrorRuntime(store, mcp, on_refresh=refresh, index_failed=True)

        message = await _run(execution, arguments={"query": "pytest"})

    assert message.status == "success"
    assert execution.refresh_reasons == ["call_error"]
    assert execution.retries == [(CALL_NAME, "success")]


# --------------------------------------------------------------------------- #
# The third state: the sync worked and the catalog read then failed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_re_read_that_raises_is_unavailable_not_a_refreshed_not_found(
    tmp_path: Path,
) -> None:
    """(None, True) used to turn a DB failure into "refreshed, so not found".

    Nothing is known about the row here: the refresh completed and the read that
    follows it raised.  The model must not be told the tool is gone.
    """
    async with catalog(tmp_path) as store:
        unreadable = UnreadableStore(store, after=1)
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(unreadable, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "contextforge_unavailable"
    assert payload["retryable"] is False
    assert "tool_not_found" not in payload["origin"]
    assert "refreshed" not in payload["error"].casefold()
    assert "not found" not in payload["error"].casefold()
    assert "could not be read" in payload["error"]
    assert unreadable.reads == 2, "one lookup, one re-read after the refresh"
    assert execution.refresh_reasons == ["call_error"]
    assert mcp.calls == [], "an unreadable catalog never reaches ContextForge"
    assert _meta(message)["origin"] == "contextforge_unavailable"


@pytest.mark.asyncio
async def test_an_upstream_not_found_whose_re_read_raises_is_unavailable(
    tmp_path: Path,
) -> None:
    """ContextForge said not found, then the re-read raised: still not a retry."""
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        unreadable = UnreadableStore(store, after=1)
        mcp = FakeMcp(_error("tool not found: github_search"))
        execution = FakeExecutionRuntime(unreadable, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "contextforge_unavailable"
    assert payload["retryable"] is False
    assert "refreshed" not in payload["error"].casefold()
    assert len(mcp.calls) == 1, "no retry without a catalog read that succeeded"
    assert execution.retries == []


# --------------------------------------------------------------------------- #
# Upstream schema mismatch (design 9.1 C): refresh and answer, never retry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upstream_schema_mismatch_returns_the_new_schema_without_retrying(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())

        async def refresh() -> None:
            await store.upsert_snapshot([_record(schema=TEXT_SCHEMA)], ["github_search"])

        mcp = FakeMcp(_error("invalid params: schema validation failed"))
        execution = FakeExecutionRuntime(store, mcp, on_refresh=refresh)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "schema_mismatch"
    assert payload["schema_changed"] is True
    assert payload["input_schema"] == TEXT_SCHEMA
    assert len(mcp.calls) == 1, "the old arguments are never retried"
    assert execution.refresh_reasons == ["call_error"]


@pytest.mark.asyncio
async def test_upstream_schema_mismatch_without_a_change_returns_the_error(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(_error("invalid arguments for tool"))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "invalid_arguments"
    assert "input_schema" not in payload
    assert len(mcp.calls) == 1
    assert execution.refresh_reasons == ["call_error"]


# --------------------------------------------------------------------------- #
# No retry: timeout, 5xx, transport, and any other execution error
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_timeout_is_neither_refreshed_nor_retried(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(ContextForgeTimeout("request timed out"))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "timeout"
    assert len(mcp.calls) == 1
    assert execution.refresh_reasons == []


@pytest.mark.asyncio
async def test_a_transport_failure_is_neither_refreshed_nor_retried(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(ContextForgeUnavailable("ContextForge returned 502 Bad Gateway"))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "transport_error"
    assert len(mcp.calls) == 1
    assert execution.refresh_reasons == []


@pytest.mark.asyncio
async def test_an_upstream_5xx_result_is_not_retried(tmp_path: Path) -> None:
    """isError with a 5xx/timeout marker: the result is uncertain, so no retry."""
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(_error("internal server error: 502"))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_error"
    assert len(mcp.calls) == 1
    assert execution.refresh_reasons == []


# --------------------------------------------------------------------------- #
# Success: MCP name, content blocks, and the audit stamp
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_result_is_stamped_with_the_inner_name_tool_id_and_call_name(
    tmp_path: Path,
) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(
            _ok(
                _text("found 3 repos"),
                mcp_types.ImageContent(type="image", data="aGk=", mimeType="image/png"),
                mcp_types.EmbeddedResource(
                    type="resource",
                    resource=mcp_types.TextResourceContents(
                        uri="file:///notes.txt", mimeType="text/plain", text="body"
                    ),
                ),
            )
        )
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    assert message.status == "success"
    assert message.content == [
        {"type": "text", "text": "found 3 repos"},
        {"type": "image", "base64": "aGk=", "mime_type": "image/png"},
        {"type": "text", "text": "file:///notes.txt\nbody"},
    ]
    assert mcp.calls == [
        {"name": "github_search", "arguments": {"query": "pytest"}, "timeout": None}
    ], "ContextForge receives the original name, not the cf__ call name"
    meta = _meta(message)
    assert meta["inner_name"] == "github_search"
    assert meta["call_name"] == CALL_NAME
    assert meta["tool_id"] == "tool-github_search"
    assert meta["status"] == "success"


@pytest.mark.asyncio
async def test_an_upstream_error_result_is_an_error_tool_message(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(_error("unauthorized: bad token"))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "tool_error"
    assert payload["error"] == "unauthorized: bad token"
    assert payload["inner_name"] == "github_search"
    assert payload["tool_id"] == "tool-github_search"
    assert _meta(message)["status"] == "error"
    assert execution.refresh_reasons == []


@pytest.mark.asyncio
async def test_a_structured_result_without_content_still_answers(tmp_path: Path) -> None:
    async with catalog(tmp_path) as store:
        await _store_record(store, _record())
        mcp = FakeMcp(
            mcp_types.CallToolResult(
                content=[],
                structuredContent={"total": 2},
                isError=False,
            )
        )
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    assert message.status == "success"
    assert message.content == [{"type": "text", "text": '{"total": 2}'}]


@pytest.mark.asyncio
async def test_the_built_runtime_hands_the_executor_its_store_and_mcp_client(
    tmp_path: Path,
) -> None:
    """The production wiring, not just the test doubles: build_contextforge_runtime."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "wiring.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = build_contextforge_runtime(
        ContextForgeConfig(
            enabled=True,
            base_url="http://cf",
            admin_token="token",
            search={"enabled": False},
        ),
        session_factory=factory,
    )
    assert runtime is not None
    try:
        assert isinstance(runtime.store, ContextForgeToolStore)
        assert isinstance(runtime.mcp_client, ContextForgeMcpClient)
    finally:
        reset_contextforge_runtime()
        await runtime.admin.aclose()
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Fail closed: a schema this process cannot check (design 9.3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_schema_that_cannot_resolve_remotely_is_refused(
    tmp_path: Path,
) -> None:
    """A remote $ref is never fetched (9.3): the tool is refused, not deferred.

    Fetching it would make an upstream tool author's schema trigger a request from
    the gateway (SSRF); skipping validation would send arguments nothing checked.
    """
    schema = {
        "type": "object",
        "properties": {"query": {"$ref": "https://example.com/args.json"}},
        "required": ["query"],
    }
    async with catalog(tmp_path) as store:
        await _store_record(store, _record(schema=schema))
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "schema_unresolved"
    assert payload["retryable"] is False
    assert "example.com" in payload["error"], "the reason is reported, not swallowed"
    assert mcp.calls == [], "unvalidated arguments never leave the process"
    assert execution.refresh_reasons == []
    assert _meta(message)["origin"] == "schema_unresolved"


@pytest.mark.asyncio
async def test_a_schema_that_does_not_compile_is_refused(tmp_path: Path) -> None:
    """The same rule for a schema jsonschema itself rejects."""
    schema = {
        "type": "object",
        "properties": {"query": {"type": "nonsense"}},
        "required": ["query"],
    }
    async with catalog(tmp_path) as store:
        await _store_record(store, _record(schema=schema))
        mcp = FakeMcp()
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    payload = _payload(message)
    assert payload["origin"] == "schema_unresolved"
    assert "UnknownType" in payload["error"]
    assert mcp.calls == []
    assert execution.refresh_reasons == []


@pytest.mark.asyncio
async def test_a_stored_schema_that_is_missing_still_defers(tmp_path: Path) -> None:
    """No schema is not a broken schema: with nothing to check, MCP decides."""
    async with catalog(tmp_path) as store:
        await _store_record(store, _record(schema={}))
        mcp = FakeMcp(_ok(_text("ok")))
        execution = FakeExecutionRuntime(store, mcp)

        message = await _run(execution, arguments={"query": "pytest"})

    assert message.status == "success"
    assert len(mcp.calls) == 1


# --------------------------------------------------------------------------- #
# The retryable flag, pinned for every origin the executor can report
# --------------------------------------------------------------------------- #

#: The decision, per origin.  False is the default for anything that already had
#: its one fallback: tool_not_found was refreshed and retried once and its own
#: wording says "do not repeat this call" (9.1 B.4), so a True there would
#: contradict the message the model reads.
RETRYABLE_BY_ORIGIN: dict[str, bool] = {
    executor_module.ORIGIN_NOT_RETRIEVED: True,
    executor_module.ORIGIN_LOCAL_VALIDATION: True,
    executor_module.ORIGIN_SCHEMA_MISMATCH: True,
    executor_module.ORIGIN_INVALID_ARGUMENTS: True,
    executor_module.ORIGIN_TOOL_NOT_FOUND: False,
    executor_module.ORIGIN_TRANSPORT_ERROR: False,
    executor_module.ORIGIN_TIMEOUT: False,
    executor_module.ORIGIN_TOOL_ERROR: False,
    executor_module.ORIGIN_UNAVAILABLE: False,
    executor_module.ORIGIN_SCHEMA_UNRESOLVED: False,
}


@pytest.mark.parametrize(
    ("origin", "expected"), sorted(RETRYABLE_BY_ORIGIN.items())
)
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
