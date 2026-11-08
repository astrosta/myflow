"""tool_execute: run one retrieved MCP tool against the server that owns it.

MCP tools are not bound to the agent: tool_search hands the model a
``mcp__<server>__<tool>`` call name, its verbatim schema and the argument
contract, and this tool is the only thing that actually runs one.  It always runs
it the same way: look the row up in ``mcp_tools`` by owner + call name, refuse it
unless the store still calls it callable and the server that owns it can serve it,
validate the arguments against the stored schema, then run one ``tools/call``
against the owning server with the *original* (unprefixed) name.  No LangChain
tool is created at runtime and ToolNode stays static.

Two things shape the code more than anything else.

The gates come first.  require_retrieved is on: a call name that tool_search did
not return in this run is refused before the database is touched, because a model
that guessed ``mcp__anysearch__search`` must not turn guesswork into a capability.
The lookup is owner-scoped, and callability is enforced here explicitly instead of
by filtering the query, because the *reason* is part of the answer: a stored row
whose server is disabled is ``server_disabled``, one whose server is unreachable is
``server_unavailable``, and everything else - another owner's row, a tool the last
sync no longer saw, a row whose server was deleted - is ``tool_not_found``.  A
non-callable row is never executed, whatever the call name says.

The fallback is per error class (design 9.1), and most errors have none.  Only
two things justify refreshing the owning server: the tool is missing (refresh,
then retry exactly once) and a schema/argument mismatch, local or upstream
(refresh, answer with the new schema, never retry the old arguments - the model
has to reorganise them).  A timeout, an unreachable server, a protocol failure or
an upstream ``isError`` answer may already have run upstream, so this tool neither
refreshes nor retries there.  The refresh is
``runtime.refresh_server(record.server_id)``, bounded by
REFRESH_TIMEOUT_SECONDS, and the re-read that follows it can itself fail: "the row
is gone" and "I could not look" are different facts, so an unreadable catalog is
``mcp_unavailable`` and never a refreshed ``tool_not_found``.

The local-validation branch also carries ``origin=local_validation`` in the reply
and in myflow_tool_meta: its fallback already ran, so a middleware must not
refresh a second time and re-run the call on top of that reply.

One rule comes from 9.3.  A stored schema this process cannot compile or resolve
is refused outright: validation is the gate between the model's arguments and the
server, so a schema that cannot be checked must not be waved through - a remote
``$ref`` would otherwise make the gateway fetch an author-chosen URL.

Task 2's client returns the CallToolResult even when ``isError`` is true, so an
upstream tool failure arrives here as *data* and is branched on ``result.isError``
exactly like any other result, never as a transport failure.  Only ``McpToolError``
- the JSON-RPC/protocol path, where no result exists - is reported as
``tool_error`` with the server's own sanitized text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from jsonschema import Draft202012Validator
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from pydantic import BaseModel, Field

from agentkit.agents.middlewares.tool_result_meta import (
    TOOL_META_KEY,
    normalize_tool_message,
)
from agentkit.mcp.errors import (
    McpServerError,
    McpServerTimeout,
    McpServerUnavailable,
    McpToolError,
    sanitize_error_text,
)
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.runtime_protocol import get_mcp_runtime
from agentkit.mcp.store.tool_repository import (
    CALL_NAME_PREFIX,
    McpToolRecord,
    schema_hash_for,
)
from agentkit.runtime.user_context import resolve_runtime_user_id

logger = logging.getLogger(__name__)

TOOL_NAME = "tool_execute"
DEFAULT_REFRESH_TIMEOUT_SECONDS = 10.0
MAX_ERROR_CHARS = 2000
MAX_VALIDATION_ERRORS = 5

#: Why tool_execute did not run the tool. Recorded in the reply and in
#: myflow_tool_meta so an audit and the model use the same vocabulary.
ORIGIN_NOT_RETRIEVED = "not_retrieved"
ORIGIN_LOCAL_VALIDATION = "local_validation"
ORIGIN_TOOL_NOT_FOUND = "tool_not_found"
ORIGIN_SCHEMA_MISMATCH = "schema_mismatch"
ORIGIN_INVALID_ARGUMENTS = "invalid_arguments"
ORIGIN_TRANSPORT_ERROR = "transport_error"
ORIGIN_TIMEOUT = "timeout"
ORIGIN_TOOL_ERROR = "tool_error"
#: This deployment has no usable MCP runtime, or its catalog could not be read.
ORIGIN_MCP_UNAVAILABLE = "mcp_unavailable"
#: The borrowed distinctions: the server, not the tool, is what refuses the call.
ORIGIN_SERVER_UNAVAILABLE = "server_unavailable"
ORIGIN_SERVER_DISABLED = "server_disabled"
ORIGIN_SCHEMA_UNRESOLVED = "schema_unresolved"

#: Origins where a corrected call can succeed, so the model should adjust and try
#: again. Everything else is final for this call: a timeout/transport failure may
#: already have run upstream, a missing tool is reported with "do not repeat" (its
#: refresh already ran), a disabled or unreachable server cannot be fixed by
#: re-sending the same request, and an unusable stored schema is broken data.
_RECOVERABLE_ORIGINS = frozenset(
    {
        ORIGIN_NOT_RETRIEVED,
        ORIGIN_LOCAL_VALIDATION,
        ORIGIN_SCHEMA_MISMATCH,
        ORIGIN_INVALID_ARGUMENTS,
    }
)

#: What the refresh + re-read pair produced.
REREAD_REFRESHED = "refreshed"
REREAD_NOT_REFRESHED = "not_refreshed"
REREAD_DISABLED = "disabled"
REREAD_UNKNOWN_SERVER = "unknown_server"

_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded", "read timeout")


class _UpstreamError(Enum):
    """The upstream failures one refresh and one retry may be worth (design 7)."""

    NONE = "none"
    NOT_FOUND = "not_found"
    INVALID_ARGUMENTS = "invalid_arguments"


#: Text an upstream server uses when the tool itself is gone, or when it rejects
#: the arguments.  The vocabulary is part of this tool, so no gateway module is
#: imported at runtime.
_NOT_FOUND_MARKERS = (
    "tool not found",
    "unknown tool",
    "tool does not exist",
    "no such tool",
    "工具不存在",
    "未找到工具",
    "-32601",
)
_INVALID_ARGUMENT_MARKERS = (
    "validation error",
    "invalid arguments",
    "invalid params",
    "schema validation",
    "input validation",
    "参数校验",
    "参数错误",
    "缺少必填参数",
    "-32602",
)
#: Failures that must never be refreshed or retried, whatever else they say: the
#: call may have run, or repeating it is simply wrong.
_NEVER_RETRY_MARKERS = (
    "unauthorized",
    "forbidden",
    "authentication",
    "timeout",
    "timed out",
    "rate limit",
    "too many requests",
    "429",
    "internal server error",
    "502",
    "503",
    "504",
    "connection reset",
    "connection closed",
)

NOT_RETRIEVED_HINT = (
    "Call tool_search first and pass the call_name from the card it returned; "
    "tool_execute only accepts names retrieved in this run."
)
MCP_UNAVAILABLE_MESSAGE = (
    "MCP execution is unavailable in this deployment: the process has no MCP "
    "runtime. Do not retry; tell the user."
)
CATALOG_UNAVAILABLE_MESSAGE = (
    "The MCP tool catalog could not be read. Do not retry; tell the user."
)
SCHEMA_UNUSABLE_MESSAGE = (
    "The stored input_schema for this tool cannot be compiled or resolved here, "
    "so the arguments cannot be validated and the tool was not run. Do not retry "
    "with different arguments; tell the user the tool definition is broken."
)
EMPTY_RESULT = "The tool returned no content."

try:  # jsonschema >= 4.18 resolves $ref through the referencing package
    from referencing import Registry as _Registry
except ImportError:  # pragma: no cover - jsonschema ships its resolver
    _Registry = None  # type: ignore[assignment,misc]

#: An empty registry: an in-schema $ref still resolves against the schema itself,
#: while a remote $ref has nowhere to come from.  Retrieving one would fetch a URL
#: an upstream tool author chose, from inside a tool call.
_EMPTY_REGISTRY = _Registry() if _Registry is not None else None


class ToolExecuteInput(BaseModel):
    tool_name: str = Field(
        description=(
            "The call_name tool_search returned, verbatim (mcp__ prefixed). "
            "Never the bare upstream name and never a guessed name."
        )
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments that match the input_schema returned with the card; "
            "no owner, user or token fields."
        ),
    )


@tool(TOOL_NAME, args_schema=ToolExecuteInput)
async def tool_execute(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    runtime: ToolRuntime[Any, Any] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> ToolMessage:
    """Run one tool returned by tool_search, on the server that owns it.

    Pass its call_name and arguments matching the input_schema from the card.
    The reply carries the tool result, or the failure origin and - when the
    stored schema changed - the new input_schema to reorganise the arguments
    against. Failures that may already have run upstream are never retried.
    """
    call_name = (tool_name or "").strip()
    args = dict(arguments) if isinstance(arguments, Mapping) else {}
    execution = get_mcp_runtime()
    # tool_search.require_retrieved (default true) is the gate the design puts in
    # front of every call: retrieval is what makes a name callable, so a name this
    # run never saw never reaches the store or an upstream server.  The default
    # when no runtime is configured is still true - an unconfigured process has no
    # basis to accept a name either.
    if _require_retrieved(execution) and call_name not in _retrieved_names(runtime):
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_NOT_RETRIEVED,
            message=(
                f"tool_execute refused {call_name or 'an empty tool name'}: it was "
                f"not returned by tool_search in this run. {NOT_RETRIEVED_HINT}"
            ),
        )
    if not _usable(execution):
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_MCP_UNAVAILABLE,
            message=MCP_UNAVAILABLE_MESSAGE,
        )

    owner = resolve_runtime_user_id(_context(runtime), allow_default=True)
    resolution = await _resolve(execution, call_name, owner)
    if resolution.origin is not None:
        return _unresolved_reply(
            tool_call_id,
            call_name=call_name,
            resolution=resolution,
            state=REREAD_NOT_REFRESHED,
        )
    recovered_from_refresh = False
    if resolution.record is None:
        # One refresh of the owning server, then one re-read (design 9.1 B).  That
        # run is what record_retry reports on; a plain hit has nothing to book.
        resolution, state = await _reread_after_refresh(
            execution, call_name, owner, resolution.server_id
        )
        if resolution.record is None:
            return _unresolved_reply(
                tool_call_id, call_name=call_name, resolution=resolution, state=state
            )
        recovered_from_refresh = True

    record = resolution.record
    check = _validate_arguments(record, args)
    if check.unusable is not None:
        # Design 9.3: fail closed.  An unverifiable schema is not a licence to
        # skip the gate - and a remote $ref must never be fetched to fix it.
        logger.warning(
            "refusing %s: its stored input_schema is unusable (%s)",
            record.call_name,
            check.unusable,
        )
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_SCHEMA_UNRESOLVED,
            message=f"{SCHEMA_UNUSABLE_MESSAGE} ({check.unusable})",
            record=record,
        )
    if check.problem is not None:
        return await _validation_reply(
            execution,
            owner,
            call_name,
            record,
            check.problem,
            tool_call_id,
            refreshed=recovered_from_refresh,
        )

    outcome = await _call_tool(execution, record, args)
    if outcome.origin is not None:
        if recovered_from_refresh:
            _record_retry(execution, call_name, False)
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=outcome.origin,
            message=outcome.message,
            record=record,
        )
    if bool(getattr(outcome.result, "isError", False)):
        return await _upstream_error_reply(
            execution,
            owner,
            call_name,
            record,
            args,
            outcome,
            tool_call_id,
            refreshed=recovered_from_refresh,
        )
    if recovered_from_refresh:
        _record_retry(execution, call_name, True)
    return _success_reply(tool_call_id, record, outcome.result)


# --------------------------------------------------------------------------- #
# Lookup: the catalog row, the server behind it, and why a call is refused
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Resolution:
    """What one catalog read says about a call name.

    Three outcomes, and the difference between them is the difference between a
    fact and a question.  `record` set means a stored row this process may call
    (its server is this owner's, enabled and reachable, and the last sync still
    saw the tool).  `origin` set means a definite refusal a refresh cannot change,
    with `message` explaining it.  Neither set means "no callable row", which one
    refresh of `server_id` may still fix.

    `known` is the row the catalog did return, callable or not: it is never used
    to run anything, but a refusal should still name the tool an audit expects
    ("search"), not the call name it was asked for ("mcp__anysearch__search").
    """

    record: McpToolRecord | None = None
    origin: str | None = None
    message: str = ""
    #: The server a not-found refresh should target, when the call name names one.
    server_id: str = ""
    #: The stored row behind this answer, for myflow_tool_meta; never executed.
    known: McpToolRecord | None = None


def _usable(execution: Any) -> bool:
    """Does this process have the runtime pieces an execution needs?

    Checked member by member so a half-built runtime is answered with
    `mcp_unavailable` instead of an AttributeError inside a tool call.
    """
    return (
        execution is not None
        and getattr(execution, "tool_store", None) is not None
        and getattr(execution, "server_store", None) is not None
        and callable(getattr(execution, "call_tool", None))
    )


async def _resolve(execution: Any, call_name: str, owner: str | None) -> _Resolution:
    """One catalog read, plus the server state that decides whether we may call it.

    Callability is checked here rather than filtered into the query, because the
    *reason* a row is not callable is what the model needs: a disabled server and
    a tool that vanished upstream are different answers.  A non-callable row is
    never returned as a record, so it can never be executed.
    """
    try:
        record = await execution.tool_store.get_by_call_name(call_name, owner)
    except Exception as exc:  # noqa: BLE001 - a tool result, not a run failure
        logger.warning("MCP tool catalog read failed: %s", _exception_text(exc))
        return _Resolution(
            origin=ORIGIN_MCP_UNAVAILABLE, message=CATALOG_UNAVAILABLE_MESSAGE
        )
    if record is None:
        # Not in this owner's catalog.  One refresh may still find it, so the
        # server named by the call name is resolved first - and a catalog read
        # that fails here is "could not look", never "no such server".
        server, failed = await _server_for_call_name(execution, call_name, owner)
        if failed:
            return _Resolution(
                origin=ORIGIN_MCP_UNAVAILABLE, message=CATALOG_UNAVAILABLE_MESSAGE
            )
        return _missing_tool_resolution(call_name, server)
    if record.owner_user_id and owner and record.owner_user_id != owner:
        # The query is owner-scoped, so this can only be a wiring bug; the call is
        # refused anyway, because a stored row is data and never a capability.
        return _Resolution(
            origin=ORIGIN_TOOL_NOT_FOUND,
            message=_not_found_message(call_name, refreshed=False),
            known=record,
        )
    try:
        server = await execution.server_store.get(record.server_id, owner)
    except Exception as exc:  # noqa: BLE001 - "could not look", not "not there"
        logger.warning("MCP server read failed: %s", _exception_text(exc))
        return _Resolution(
            origin=ORIGIN_MCP_UNAVAILABLE,
            message=CATALOG_UNAVAILABLE_MESSAGE,
            known=record,
        )
    if server is None:
        # The server was deleted and its tools with it; a row that outlived it
        # names nothing anyone can call, and there is nothing left to refresh.
        return _Resolution(
            origin=ORIGIN_TOOL_NOT_FOUND,
            message=_not_found_message(call_name, refreshed=False),
            known=record,
        )
    if not server.enabled:
        return _Resolution(
            origin=ORIGIN_SERVER_DISABLED,
            message=_server_disabled_message(call_name, server),
            known=record,
        )
    if not server.reachable:
        return _Resolution(
            origin=ORIGIN_SERVER_UNAVAILABLE,
            message=_server_unavailable_message(call_name, server),
            known=record,
        )
    if not record.callable:
        # The server is healthy, so the tool itself stopped qualifying: the last
        # sync no longer saw it, or it is disabled upstream.  One refresh of that
        # server can change this, so it takes the same fallback as a missing row.
        return _Resolution(server_id=record.server_id, known=record)
    return _Resolution(record=record, known=record)


def _missing_tool_resolution(
    call_name: str, server: McpServerRecord | None
) -> _Resolution:
    """What an absent row means, given the server its call name names.

    A disabled or unreachable server is a fact about the *server*, and a refresh
    cannot change it: syncing a switched-off server only costs a round trip and
    then answers "not found", which reads as if the tool had vanished.  Only a
    healthy server - or none at all - leaves a question one refresh can answer.
    """
    if server is None:
        return _Resolution()
    if not server.enabled:
        return _Resolution(
            origin=ORIGIN_SERVER_DISABLED,
            message=_server_disabled_message(call_name, server),
        )
    if not server.reachable:
        return _Resolution(
            origin=ORIGIN_SERVER_UNAVAILABLE,
            message=_server_unavailable_message(call_name, server),
        )
    return _Resolution(server_id=server.id)


async def _server_for_call_name(
    execution: Any, call_name: str, owner: str | None
) -> tuple[McpServerRecord | None, bool]:
    """The server a call name names, and whether that lookup itself failed.

    A call name is all a missing row still tells us, and a sync is per server, so
    this is what a not-found refresh targets.  "No such server of yours" and "the
    server table could not be read" are different answers - the second one is
    mcp_unavailable, because this process does not know whether the row exists.
    """
    server_name = _server_name_from_call_name(call_name)
    if not server_name:
        return None, False
    try:
        server = await execution.server_store.get_by_name(owner, server_name)
    except Exception as exc:  # noqa: BLE001 - reported as unavailable, not not-found
        logger.warning("MCP server lookup failed: %s", _exception_text(exc))
        return None, True
    return server, False


async def _reread_after_refresh(
    execution: Any,
    call_name: str,
    owner: str | None,
    server_id: str,
    *,
    refreshed: bool = False,
) -> tuple[_Resolution, str]:
    """One refresh of one server, then the single re-read that follows it.

    `refreshed=True` means this call already refreshed that server (the row had
    to be recovered first): the design allows one refresh per user call, so the
    second fallback only re-reads.  Refreshing again would sync the same server
    twice for one call - and a server whose sync is slow would pay that twice.

    The state is not a "refreshed" boolean.  A re-read that *raises* is a third
    outcome: the sync succeeded and the catalog read failed afterwards, so this
    process knows neither that the row exists nor that it does not.  Returning
    "refreshed" there used to make a database failure back a "refreshed,
    therefore not found" answer; it is `mcp_unavailable` instead.
    """
    if refreshed:
        return await _resolve(execution, call_name, owner), REREAD_REFRESHED
    if not _retry_on_tool_error(execution):
        return _Resolution(server_id=server_id), REREAD_DISABLED
    if not server_id:
        return _Resolution(), REREAD_UNKNOWN_SERVER
    if not await _refresh_server(execution, server_id):
        return _Resolution(server_id=server_id), REREAD_NOT_REFRESHED
    return await _resolve(execution, call_name, owner), REREAD_REFRESHED


async def _refresh_server(execution: Any, server_id: str) -> bool:
    """One awaited, bounded refresh of one server; False means it did not happen.

    Task 8's `refresh_server` must return a *per-target* result - an object that
    exposes `error` and `index_failed` for the server it was given - not a global
    sync summary: a whole-catalog failure would otherwise veto the retry of one
    healthy server, and a global success would claim a target that failed.
    """
    refresh = getattr(execution, "refresh_server", None)
    if not callable(refresh):
        return False
    try:
        stats = await asyncio.wait_for(
            refresh(server_id), timeout=DEFAULT_REFRESH_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - a failed fallback is not a run failure
        logger.warning(
            "MCP server refresh failed for %s: %s", server_id, _exception_text(exc)
        )
        return False
    if _refresh_failed(stats):
        # A sync that reports its failure in stats instead of raising did not
        # replace the snapshot, so returning normally is not evidence it changed.
        logger.warning(
            "MCP server refresh reported a failure: %s", getattr(stats, "error", None)
        )
        return False
    logger.info("MCP server %s refreshed after a failed execution", server_id)
    return True


def _refresh_failed(stats: Any) -> bool:
    """Did this sync leave the stored snapshot in place?

    `error` means the snapshot was not replaced, so the re-read that follows would
    only see the stale row.  An `index_failed` error is the exception: the
    database snapshot was committed and only the derived vector index failed, so
    the catalog tool_execute reads is already fresh.
    """
    error = getattr(stats, "error", None)
    if not error:
        return False
    return not bool(getattr(stats, "index_failed", False))


def _retry_on_tool_error(execution: Any) -> bool:
    """Whether a failed call may refresh its server and retry once (design 7).

    The design makes this a switch ("若配置开启"), and the switch lives on the
    runtime because the `mcp_servers` config block carries no retry policy.  An
    absent field keeps the design default (on): only an explicit false turns the
    fallback off.
    """
    return bool(getattr(execution, "retry_on_tool_error", True))


def _schema_changed(before: McpToolRecord, after: McpToolRecord) -> bool:
    if before.schema_hash and after.schema_hash:
        return before.schema_hash != after.schema_hash
    return schema_hash_for(before.input_schema) != schema_hash_for(after.input_schema)


def _record_retry(execution: Any, call_name: str, ok: bool) -> None:
    record = getattr(execution, "record_retry", None)
    if not callable(record):
        return
    try:
        record(tool_name=call_name, result="success" if ok else "failed")
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the call
        logger.debug("record_retry failed: %s", type(exc).__name__)


# --------------------------------------------------------------------------- #
# Replies for a lookup that produced no callable row
# --------------------------------------------------------------------------- #


def _unresolved_reply(
    tool_call_id: str,
    *,
    call_name: str,
    resolution: _Resolution,
    state: str,
) -> ToolMessage:
    """The reply for a lookup that produced no callable row: refused or missing."""
    if resolution.origin is not None:
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=resolution.origin,
            message=resolution.message,
            record=resolution.known,
        )
    return _missing_reply(
        tool_call_id, call_name=call_name, state=state, record=resolution.known
    )


def _missing_reply(
    tool_call_id: str,
    *,
    call_name: str,
    state: str,
    record: McpToolRecord | None = None,
) -> ToolMessage:
    """A call name this process could not resolve to a callable row."""
    if state == REREAD_DISABLED:
        message = _retry_disabled_message(call_name)
    elif state == REREAD_UNKNOWN_SERVER:
        message = _unknown_server_message(call_name)
    else:
        message = _not_found_message(call_name, refreshed=state == REREAD_REFRESHED)
    return _error_reply(
        tool_call_id,
        call_name=call_name,
        origin=ORIGIN_TOOL_NOT_FOUND,
        message=message,
        record=record,
    )


async def _validation_reply(
    execution: Any,
    owner: str | None,
    call_name: str,
    record: McpToolRecord,
    problem: str,
    tool_call_id: str,
    *,
    refreshed: bool = False,
) -> ToolMessage:
    """Design 9.1 A: refresh, then answer with the new schema - no retry, no call.

    A record recovered from a refresh already spent this call's one refresh, so
    there only the re-read runs (see `_reread_after_refresh`).
    """
    fresh, _state = await _reread_after_refresh(
        execution, call_name, owner, record.server_id, refreshed=refreshed
    )
    if fresh.record is not None and _schema_changed(record, fresh.record):
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_LOCAL_VALIDATION,
            message=problem,
            record=fresh.record,
            schema=fresh.record.input_schema,
        )
    return _error_reply(
        tool_call_id,
        call_name=call_name,
        origin=ORIGIN_LOCAL_VALIDATION,
        message=problem,
        record=record,
    )


async def _upstream_error_reply(
    execution: Any,
    owner: str | None,
    call_name: str,
    record: McpToolRecord,
    args: dict[str, Any],
    outcome: _CallOutcome,
    tool_call_id: str,
    *,
    refreshed: bool = False,
) -> ToolMessage:
    """Design 9.1 B/C: what the owning server answering isError entitles us to.

    The lookup for a refresh-and-retry decision reads the *raw* upstream text, but
    everything shown to the model goes through `_result_message`: an upstream tool
    can echo the request it saw, Authorization header and endpoint included, and
    this reply is the model's.
    """
    raw = _result_text(outcome.result)
    kind = _classify_upstream(raw)
    text = _result_message(raw)
    if kind is _UpstreamError.NOT_FOUND:
        fresh, state = await _reread_after_refresh(
            execution, call_name, owner, record.server_id, refreshed=refreshed
        )
        if fresh.origin is not None or fresh.record is None:
            return _unresolved_reply(
                tool_call_id, call_name=call_name, resolution=fresh, state=state
            )
        retry = await _call_tool(execution, fresh.record, args)
        if retry.origin is not None:
            _record_retry(execution, call_name, False)
            return _error_reply(
                tool_call_id,
                call_name=call_name,
                origin=retry.origin,
                message=retry.message,
                record=fresh.record,
            )
        if bool(getattr(retry.result, "isError", False)):
            _record_retry(execution, call_name, False)
            retry_text = _result_text(retry.result)
            return _error_reply(
                tool_call_id,
                call_name=call_name,
                origin=_upstream_origin(retry_text),
                message=_result_message(retry_text),
                record=fresh.record,
            )
        _record_retry(execution, call_name, True)
        return _success_reply(tool_call_id, fresh.record, retry.result)

    if kind is _UpstreamError.INVALID_ARGUMENTS:
        fresh, _state = await _reread_after_refresh(
            execution, call_name, owner, record.server_id, refreshed=refreshed
        )
        if fresh.record is not None and _schema_changed(record, fresh.record):
            return _error_reply(
                tool_call_id,
                call_name=call_name,
                origin=ORIGIN_SCHEMA_MISMATCH,
                message=text,
                record=fresh.record,
                schema=fresh.record.input_schema,
            )
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_INVALID_ARGUMENTS,
            message=text,
            record=record,
        )

    # Timeout, 5xx, auth, or any other execution error: the call may have run, so
    # this is reported as it came back (scrubbed) and never repeated.  The origin
    # is named from the raw text, which is what carries the markers.
    return _error_reply(
        tool_call_id,
        call_name=call_name,
        origin=_failure_origin(raw),
        message=text,
        record=record,
    )


# --------------------------------------------------------------------------- #
# The MCP call
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    """Either a CallToolResult, or the origin and text of a call that failed."""

    result: Any = None
    origin: str | None = None
    message: str = ""


async def _call_tool(
    execution: Any, record: McpToolRecord, args: dict[str, Any]
) -> _CallOutcome:
    """One tools/call on the owning server, by the original (unprefixed) name."""
    try:
        result = await execution.call_tool(record, dict(args))
    except McpToolError as exc:
        # The JSON-RPC/protocol path: the call produced no result, so it is
        # reported as tool_error and never retried.  A JSON-RPC rejection can be a
        # remote -32601 (unknown tool) or -32602 (invalid params), but repeating a
        # call the server already refused is exactly what design 9.1 forbids; the
        # safe refresh paths are the local not-found and the upstream isError text
        # ones above.  A carried CallToolResult is the server's own text; either
        # way the text is scrubbed of credentials before it reaches the model.
        logger.warning("MCP tools/call failed for %s (tool_error)", record.name)
        return _CallOutcome(origin=ORIGIN_TOOL_ERROR, message=_tool_error_text(exc))
    except Exception as exc:  # noqa: BLE001 - classified, then reported as a result
        origin = _exception_origin(exc)
        logger.warning("MCP tools/call failed for %s (%s)", record.name, origin)
        message = (
            f"MCP tools/call failed for {record.name} ({origin}): "
            f"{_exception_text(exc)}"
        )
        return _CallOutcome(origin=origin, message=message[:MAX_ERROR_CHARS])
    return _CallOutcome(result=result)


def _result_message(text: str) -> str:
    """Upstream text this tool may show: scrubbed first, then bounded.

    An upstream tool's error text is not ours to trust - it can echo the request
    it just saw, Authorization header and endpoint included - so the shared
    sanitizer runs before any of it reaches a reply or the model.
    """
    return sanitize_error_text(text)[:MAX_ERROR_CHARS]


def _tool_error_text(exc: McpToolError) -> str:
    """An McpToolError's own text, with any credential scrubbed out of it."""
    result = getattr(exc, "result", None)
    if result is not None:
        text = _result_message(_result_text(result))
        if text:
            return text
    return _result_message(str(exc)) or type(exc).__name__


def _exception_text(exc: BaseException) -> str:
    """The part of a failure this process may show: never a URL or a header.

    The MCP client builds its messages from fixed text plus the server id, so an
    McpServerError's own text is the useful part and is kept (scrubbed once more
    here); any other exception is reduced to its class name, because its text - a
    proxy body, a database error - is not ours to trust.
    """
    if isinstance(exc, McpServerError):
        return sanitize_error_text(str(exc)) or type(exc).__name__
    return type(exc).__name__


def _exception_origin(exc: BaseException) -> str:
    """Which origin a tools/call exception deserves.

    A timeout or an unreachable server never justifies a refresh: the call may
    already have run and repeating it is exactly what the design forbids.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, McpServerTimeout)):
        return ORIGIN_TIMEOUT
    if isinstance(exc, McpServerUnavailable):
        return ORIGIN_SERVER_UNAVAILABLE
    name = type(exc).__name__.casefold()
    if any(marker in name for marker in ("timeout", "timedout", "deadline")):
        return ORIGIN_TIMEOUT
    if any(marker in str(exc).casefold() for marker in _TIMEOUT_MARKERS):
        return ORIGIN_TIMEOUT
    # An McpProtocolError, or any other unexpected exception: no result exists and
    # the call may or may not have run, so this is final for the call.
    return ORIGIN_TRANSPORT_ERROR


def _failure_origin(text: str) -> str:
    """An isError we neither refresh nor retry, named for the audit trail."""
    folded = text.casefold()
    if any(marker in folded for marker in _TIMEOUT_MARKERS):
        return ORIGIN_TIMEOUT
    return ORIGIN_TOOL_ERROR


def _upstream_origin(text: str) -> str:
    """Name an upstream failure that was already retried once."""
    kind = _classify_upstream(text)
    if kind is _UpstreamError.NOT_FOUND:
        return ORIGIN_TOOL_NOT_FOUND
    if kind is _UpstreamError.INVALID_ARGUMENTS:
        return ORIGIN_INVALID_ARGUMENTS
    return _failure_origin(text)


def _classify_upstream(value: Any) -> _UpstreamError:
    """Does this upstream error text argue for one refresh and one retry?

    The "never retry" markers outrank the rest: an error that also mentions a
    timeout, a 5xx or an auth failure may have run upstream, and "tool not found:
    503" must not turn into a refresh.
    """
    text = str(value or "").casefold()
    if not text:
        return _UpstreamError.NONE
    if any(marker in text for marker in _NEVER_RETRY_MARKERS):
        return _UpstreamError.NONE
    if any(marker in text for marker in _NOT_FOUND_MARKERS):
        return _UpstreamError.NOT_FOUND
    if any(marker in text for marker in _INVALID_ARGUMENT_MARKERS):
        return _UpstreamError.INVALID_ARGUMENTS
    return _UpstreamError.NONE


# --------------------------------------------------------------------------- #
# Local validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _ValidationCheck:
    """What the local schema check found: a mismatch, or a schema it cannot use.

    `unusable` is the fail-closed outcome of design 9.3.  "I could not compile or
    resolve this schema" is not "the arguments are fine": validation is the only
    thing standing between the model's arguments and the upstream server, so the
    executor refuses instead of forwarding the call unvalidated.
    """

    problem: str | None = None
    unusable: str | None = None


def _validate_arguments(
    record: McpToolRecord, args: dict[str, Any]
) -> _ValidationCheck:
    """The local argument/schema mismatch, a schema we cannot use, or neither."""
    schema = record.input_schema if isinstance(record.input_schema, dict) else {}
    if not schema:
        # No schema at all is not a broken schema: the server is authoritative.
        return _ValidationCheck()
    try:
        validator = (
            Draft202012Validator(schema, registry=_EMPTY_REGISTRY)
            if _EMPTY_REGISTRY is not None
            else Draft202012Validator(schema)
        )
        errors = sorted(validator.iter_errors(args), key=_error_sort_key)
    except Exception as exc:  # noqa: BLE001 - design 9.3: fail closed, never fetch
        detail = (str(exc).splitlines() or [""])[0][:200]
        return _ValidationCheck(unusable=f"{type(exc).__name__}: {detail}".strip(": "))
    if not errors:
        return _ValidationCheck()
    return _ValidationCheck(problem=_describe_errors(errors))


def _error_sort_key(error: Any) -> list[str]:
    return [str(part) for part in getattr(error, "absolute_path", ())]


def _describe_errors(errors: list[Any]) -> str:
    lines: list[str] = []
    for error in errors[:MAX_VALIDATION_ERRORS]:
        location = "/".join(str(part) for part in getattr(error, "absolute_path", ()))
        message = str(getattr(error, "message", "") or "invalid value")
        lines.append(f"{location}: {message}" if location else message)
    text = "; ".join(lines)
    return f"arguments do not match the stored input_schema: {text}"[:MAX_ERROR_CHARS]


# --------------------------------------------------------------------------- #
# Replies: content blocks, the error payload, and myflow_tool_meta
# --------------------------------------------------------------------------- #


def _success_reply(
    tool_call_id: str,
    record: McpToolRecord,
    result: Any,
) -> ToolMessage:
    message = ToolMessage(
        content=_blocks(result),
        tool_call_id=tool_call_id or "missing_tool_call_id",
        name=TOOL_NAME,
        status="success",
        artifact=_artifact(result),
    )
    return _stamp(message, record=record, origin=None)


def _error_reply(
    tool_call_id: str,
    *,
    call_name: str,
    origin: str,
    message: str,
    record: McpToolRecord | None = None,
    schema: dict[str, Any] | None = None,
) -> ToolMessage:
    payload: dict[str, Any] = {
        "error": message[:MAX_ERROR_CHARS],
        "origin": origin,
        "retryable": origin in _RECOVERABLE_ORIGINS,
        "call_name": call_name,
        "inner_name": record.name if record is not None else _inner_name(call_name),
        "tool_id": record.id if record is not None else "",
    }
    if schema is not None:
        payload["schema_changed"] = True
        payload["input_schema"] = schema
        payload["message"] = (
            "The stored input_schema changed. Reorganise the arguments to match "
            "input_schema above and call tool_execute again; the previous "
            "arguments were not retried."
        )
    reply = ToolMessage(
        content=json.dumps(payload, ensure_ascii=False, default=str),
        tool_call_id=tool_call_id or "missing_tool_call_id",
        name=TOOL_NAME,
        status="error",
    )
    return _stamp(reply, record=record, origin=origin, call_name=call_name)


def _stamp(
    message: ToolMessage,
    *,
    record: McpToolRecord | None,
    origin: str | None,
    call_name: str = "",
) -> ToolMessage:
    """myflow_tool_meta must name the tool that ran, not tool_execute.

    The status/error classification is the middleware's own function, reused here
    so a tool_execute reply and a normal tool reply describe failures the same
    way; the identity fields ride along in the same dict.
    """
    normalized = normalize_tool_message(message)
    meta = dict(normalized.additional_kwargs.get(TOOL_META_KEY) or {})
    meta["inner_name"] = record.name if record is not None else _inner_name(call_name)
    meta["tool_id"] = record.id if record is not None else ""
    meta["call_name"] = record.call_name if record is not None else call_name
    if origin is not None:
        meta["origin"] = origin
    kwargs = {**normalized.additional_kwargs, TOOL_META_KEY: meta}
    return normalized.model_copy(update={"additional_kwargs": kwargs})


def _blocks(result: Any) -> list[dict[str, Any]]:
    """MCP text/image/resource blocks as LangChain content blocks."""
    blocks: list[dict[str, Any]] = []
    for item in _content(result):
        block = _block(item)
        if block is not None:
            blocks.append(block)
    if not blocks:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            blocks.append(
                {
                    "type": "text",
                    "text": json.dumps(structured, ensure_ascii=False, default=str),
                }
            )
    if not blocks:
        blocks.append({"type": "text", "text": EMPTY_RESULT})
    return blocks


def _block(item: Any) -> dict[str, Any] | None:
    kind = str(getattr(item, "type", "") or "")
    if kind == "text":
        return {"type": "text", "text": str(getattr(item, "text", "") or "")}
    if kind == "image":
        return {
            "type": "image",
            "base64": str(getattr(item, "data", "") or ""),
            "mime_type": str(getattr(item, "mimeType", "") or "image/png"),
        }
    if kind == "audio":
        mime = str(getattr(item, "mimeType", "") or "audio")
        return {
            "type": "text",
            "text": f"[audio content ({mime}) omitted: it cannot be replayed here]",
        }
    if kind == "resource":
        return _resource_block(getattr(item, "resource", None))
    if kind == "resource_link":
        uri = str(getattr(item, "uri", "") or "")
        name = str(getattr(item, "name", "") or "")
        if name and uri:
            label = f"[resource link {name}: {uri}]"
        else:
            label = f"[resource link {name or uri}]"
        return {"type": "text", "text": label}
    return {"type": "text", "text": str(item)[:MAX_ERROR_CHARS]}


def _resource_block(resource: Any) -> dict[str, Any] | None:
    if resource is None:
        return None
    uri = str(getattr(resource, "uri", "") or "")
    mime = str(getattr(resource, "mimeType", "") or "")
    text = getattr(resource, "text", None)
    if isinstance(text, str):
        return {"type": "text", "text": f"{uri}\n{text}" if uri else text}
    blob = getattr(resource, "blob", None)
    if isinstance(blob, str):
        return {
            "type": "text",
            "text": (
                f"[resource {uri or 'unknown'} ({mime or 'binary'}) omitted: "
                f"{len(blob)} base64 characters]"
            ),
        }
    return {
        "type": "text",
        "text": f"[resource {uri or 'unknown'} ({mime or 'binary'})]",
    }


def _content(result: Any) -> list[Any]:
    content = getattr(result, "content", None)
    return list(content) if isinstance(content, (list, tuple)) else []


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for item in _content(result):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        kind = str(getattr(item, "type", "") or "")
        if kind:
            parts.append(f"[{kind}]")
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        return ""
    return json.dumps(structured, ensure_ascii=False, default=str)


def _artifact(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    return None if structured is None else {"structured_content": structured}


# --------------------------------------------------------------------------- #
# Wording: what the model reads, and the two policies it is gated by
# --------------------------------------------------------------------------- #


def _retry_disabled_message(call_name: str) -> str:
    return (
        f"{call_name} is not callable for this user, and the automatic server "
        "refresh is disabled by retry_on_tool_error. Do not repeat this call; "
        "search again with tool_search or tell the user."
    )


def _unknown_server_message(call_name: str) -> str:
    return (
        f"{call_name} is not in the MCP catalog for this user, and its call name "
        "names no MCP server of yours, so there is nothing to refresh. Do not "
        "repeat this call; search again with tool_search or tell the user."
    )


def _not_found_message(call_name: str, refreshed: bool) -> str:
    if refreshed:
        return (
            f"{call_name} is not callable for this user: the tool does not exist, "
            "or its server no longer lists it, or it is disabled upstream. The "
            "server was just refreshed. Do not repeat this call; search again with "
            "tool_search or tell the user."
        )
    return (
        f"{call_name} is not callable for this user and the server refresh did not "
        "complete. Do not guess another call name; search again with tool_search "
        "or tell the user."
    )


def _server_disabled_message(call_name: str, server: McpServerRecord) -> str:
    return (
        f"{call_name} belongs to MCP server {server.name!r}, which is disabled. "
        "Do not retry; enable that server or tell the user."
    )


def _server_unavailable_message(call_name: str, server: McpServerRecord) -> str:
    return (
        f"{call_name} belongs to MCP server {server.name!r}, which is unreachable "
        "right now. Do not repeat this call; check the server or tell the user."
    )


def _split_call_name(call_name: str) -> tuple[str, str]:
    """`(server, tool)` for a `mcp__<server>__<tool>` call name, else `("", "")`.

    Mirrors the store's own derivation: a server slug is [a-z0-9-]+ and therefore
    cannot contain the "__" separator, so the first segment after the prefix is
    the server name even when the upstream tool name contains "__" itself.
    """
    text = str(call_name or "")
    if not text.startswith(CALL_NAME_PREFIX):
        return "", ""
    server_name, separator, tool_name = text[len(CALL_NAME_PREFIX) :].partition("__")
    if not separator or not tool_name:
        return "", ""
    return server_name, tool_name


def _inner_name(call_name: str) -> str:
    """The upstream tool name behind a call name, for a reply with no stored row.

    `mcp__anysearch__search` -> `search`.  Stripping only the prefix would report
    `anysearch__search`, which is neither the remote tool name nor a call name the
    model ever saw.  A string that is not a call name is returned unchanged:
    guessing a tool name out of it would be worse than echoing it.
    """
    _server_name, tool_name = _split_call_name(call_name)
    return tool_name or str(call_name or "")


def _server_name_from_call_name(call_name: str) -> str:
    """The server slug a call name names, or "" when it is not one."""
    return _split_call_name(call_name)[0]


def _require_retrieved(execution: Any) -> bool:
    """Whether this process gates execution on the run's retrieval set.

    The policy is tool_search.require_retrieved so a deployment can loosen it
    deliberately; a runtime that publishes `require_retrieved` itself wins, and an
    absent runtime or field keeps the design default (true), because an unreadable
    policy is not permission.
    """
    explicit = getattr(execution, "require_retrieved", None)
    if isinstance(explicit, bool):
        return explicit
    search = getattr(getattr(execution, "config", None), "tool_search", None)
    return bool(getattr(search, "require_retrieved", True))


def _retrieved_names(runtime: Any) -> frozenset[str]:
    """The run-scoped names tool_search returned; fail closed when absent."""
    state = getattr(runtime, "state", None)
    if not isinstance(state, Mapping):
        return frozenset()
    names = state.get("retrieved_tool_names")
    if not isinstance(names, (list, tuple)):
        return frozenset()
    return frozenset(name for name in names if isinstance(name, str) and name)


def _context(runtime: Any) -> Mapping[str, Any]:
    context = getattr(runtime, "context", None)
    return context if isinstance(context, Mapping) else {}


__all__ = [
    "TOOL_NAME",
    "ToolExecuteInput",
    "tool_execute",
]
