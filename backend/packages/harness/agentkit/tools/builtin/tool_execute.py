"""tool_execute: run one retrieved ContextForge tool through MCP tools/call.

ContextForge tools are not bound to the agent; tool_search hands the model a
cf__<name> call name, its verbatim schema and the argument contract, and this
tool is the only thing that actually runs one.  It always runs it the same way:
look the row up in contextforge_tools by owner + call name + callable, validate
the arguments against the stored schema, then call ContextForge MCP tools/call
with the *original* (unprefixed) name.  No LangChain tool is created at runtime
and ToolNode stays static.

Two things shape the code more than anything else.

The gates come first.  require_retrieved is on: a call name that tool_search did
not return in this run is refused before the DB is touched, because a model that
guessed cf__read_file must not turn guesswork into a capability.  The DB read is
owner-scoped and callability is the store's derived gate, so a disabled,
unreachable or unowned row is simply not found.

The fallback is per error class (design 9.1), and most errors have none.  Only
two things justify a refresh: the tool is missing (refresh, then retry exactly
once) and a schema/argument mismatch, local or upstream (refresh, answer with
the new schema, never retry the old arguments - the model has to reorganise
them).  A timeout, a 5xx, a transport failure or any other execution error may
have already run upstream, so this tool neither refreshes nor retries there.
The refresh is the runtime's awaited refresh_now("call_error"), bounded by
sync.tool_error_refresh_timeout_seconds: request_refresh may hand the sync to
the debounced worker, which would race the re-read that follows it.

The local-validation branch also carries origin=local_validation in the reply and
myflow_tool_meta, and contextforge.retry.classify_tool_error treats that marker as
not refreshable: the fallback above already ran, so a middleware must not refresh
a second time and re-run the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
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
from agentkit.contextforge.models import ContextForgeTimeout
from agentkit.contextforge.retry import RefreshableToolError, classify_tool_error
from agentkit.contextforge.runtime import get_contextforge_runtime
from agentkit.contextforge.store import (
    CALL_NAME_PREFIX,
    ContextForgeToolRecord,
    schema_hash_for,
)
from agentkit.runtime.user_context import resolve_runtime_user_id

logger = logging.getLogger(__name__)

TOOL_NAME = "tool_execute"
#: The refresh reason recorded by the sync status for an execution fallback.
REFRESH_REASON = "call_error"
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
ORIGIN_UNAVAILABLE = "contextforge_unavailable"

#: Origins where a corrected call can succeed, so the model should adjust and
#: try again. A timeout/5xx/transport failure is not one of them: the tool may
#: already have run.
_RECOVERABLE_ORIGINS = frozenset(
    {
        ORIGIN_NOT_RETRIEVED,
        ORIGIN_LOCAL_VALIDATION,
        ORIGIN_TOOL_NOT_FOUND,
        ORIGIN_SCHEMA_MISMATCH,
        ORIGIN_INVALID_ARGUMENTS,
    }
)

_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded", "read timeout")

NOT_RETRIEVED_HINT = (
    "Call tool_search first and pass the call_name from the card it returned; "
    "tool_execute only accepts names retrieved in this run."
)
UNAVAILABLE_MESSAGE = (
    "ContextForge execution is unavailable in this deployment: the process has "
    "no tool catalog or MCP client. Do not retry; tell the user."
)
CATALOG_UNAVAILABLE_MESSAGE = (
    "The ContextForge tool catalog could not be read. Do not retry; tell the user."
)
EMPTY_RESULT = "The tool returned no content."

try:  # jsonschema >= 4.18 resolves $ref through the referencing package
    from referencing import Registry as _Registry
except ImportError:  # pragma: no cover - jsonschema ships its resolver
    _Registry = None  # type: ignore[assignment,misc]

#: An empty registry: an in-schema $ref still resolves against the schema
#: itself, while a remote $ref has nowhere to come from.  Retrieving one would
#: fetch a URL an upstream tool author chose, from inside a tool call.
_EMPTY_REGISTRY = _Registry() if _Registry is not None else None


class ToolExecuteInput(BaseModel):
    tool_name: str = Field(
        description=(
            "The call_name tool_search returned, verbatim (cf__ prefixed). "
            "Never the bare ContextForge name and never a guessed name."
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
    """Run one tool returned by tool_search, through ContextForge.

    Pass its call_name and arguments matching the input_schema from the card.
    The reply carries the tool result, or the failure origin and - when the
    stored schema changed - the new input_schema to reorganise the arguments
    against. Failures that may already have run upstream are never retried.
    """
    call_name = (tool_name or "").strip()
    args = dict(arguments) if isinstance(arguments, Mapping) else {}
    if call_name not in _retrieved_names(runtime):
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_NOT_RETRIEVED,
            message=(
                f"tool_execute refused {call_name or 'an empty tool name'}: it was "
                f"not returned by tool_search in this run. {NOT_RETRIEVED_HINT}"
            ),
        )

    execution = get_contextforge_runtime()
    store = getattr(execution, "store", None) if execution is not None else None
    mcp = getattr(execution, "mcp_client", None) if execution is not None else None
    if execution is None or store is None or mcp is None:
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_UNAVAILABLE,
            message=UNAVAILABLE_MESSAGE,
        )

    owner = resolve_runtime_user_id(_context(runtime), allow_default=True)
    try:
        record = await store.get_by_call_name(call_name, owner)
    except Exception:  # noqa: BLE001 - a tool result, not a run failure
        logger.warning("ContextForge catalog read failed", exc_info=True)
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_UNAVAILABLE,
            message=CATALOG_UNAVAILABLE_MESSAGE,
        )

    record, retried = await _resolved_record(execution, store, record, call_name, owner)
    if record is None:
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_TOOL_NOT_FOUND,
            message=_not_found_message(call_name, retried),
        )

    problem = _validation_problem(record, args)
    if problem is not None:
        return await _validation_reply(
            execution, store, owner, call_name, record, problem, tool_call_id
        )

    outcome = await _call_tool(mcp, record, args)
    if outcome.origin is not None:
        if retried:
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
            execution, store, mcp, owner, call_name, record, args, outcome, tool_call_id
        )
    if retried:
        _record_retry(execution, call_name, True)
    return _success_reply(tool_call_id, record, outcome.result)


async def _resolved_record(
    execution: Any,
    store: Any,
    record: ContextForgeToolRecord | None,
    call_name: str,
    owner: str | None,
) -> tuple[ContextForgeToolRecord | None, bool]:
    """The row if it exists, else one refresh and a single re-read (design 9.1 B)."""
    if record is not None:
        return record, False
    fresh, refreshed = await _reread_after_refresh(execution, store, call_name, owner)
    return fresh, refreshed


async def _validation_reply(
    execution: Any,
    store: Any,
    owner: str | None,
    call_name: str,
    record: ContextForgeToolRecord,
    problem: str,
    tool_call_id: str,
) -> ToolMessage:
    """Design 9.1 A: refresh, then answer with the new schema - no retry, no MCP."""
    fresh, _refreshed = await _reread_after_refresh(execution, store, call_name, owner)
    if fresh is not None and _schema_changed(record, fresh):
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_LOCAL_VALIDATION,
            message=problem,
            record=fresh,
            schema=fresh.input_schema,
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
    store: Any,
    mcp: Any,
    owner: str | None,
    call_name: str,
    record: ContextForgeToolRecord,
    args: dict[str, Any],
    outcome: _CallOutcome,
    tool_call_id: str,
) -> ToolMessage:
    """Design 9.1 B/C: what ContextForge answering isError entitles us to."""
    text = _result_text(outcome.result)
    kind = classify_tool_error(text)
    if kind is RefreshableToolError.NOT_FOUND:
        fresh, refreshed = await _reread_after_refresh(
            execution, store, call_name, owner
        )
        if fresh is None:
            return _error_reply(
                tool_call_id,
                call_name=call_name,
                origin=ORIGIN_TOOL_NOT_FOUND,
                message=_not_found_message(call_name, refreshed),
                record=record,
            )
        retry = await _call_tool(mcp, fresh, args)
        if retry.origin is not None:
            _record_retry(execution, call_name, False)
            return _error_reply(
                tool_call_id,
                call_name=call_name,
                origin=retry.origin,
                message=retry.message,
                record=fresh,
            )
        if bool(getattr(retry.result, "isError", False)):
            _record_retry(execution, call_name, False)
            retry_text = _result_text(retry.result)
            return _error_reply(
                tool_call_id,
                call_name=call_name,
                origin=_upstream_origin(retry_text),
                message=retry_text[:MAX_ERROR_CHARS],
                record=fresh,
            )
        _record_retry(execution, call_name, True)
        return _success_reply(tool_call_id, fresh, retry.result)

    if kind is RefreshableToolError.INVALID_ARGUMENTS:
        fresh, _refreshed = await _reread_after_refresh(
            execution, store, call_name, owner
        )
        if fresh is not None and _schema_changed(record, fresh):
            return _error_reply(
                tool_call_id,
                call_name=call_name,
                origin=ORIGIN_SCHEMA_MISMATCH,
                message=text,
                record=fresh,
                schema=fresh.input_schema,
            )
        return _error_reply(
            tool_call_id,
            call_name=call_name,
            origin=ORIGIN_INVALID_ARGUMENTS,
            message=text,
            record=record,
        )

    # Timeout, 5xx, transport, or any other execution error: the call may have
    # run upstream, so this is reported as-is and never repeated.
    return _error_reply(
        tool_call_id,
        call_name=call_name,
        origin=_failure_origin(text),
        message=text,
        record=record,
    )


# --------------------------------------------------------------------------- #
# The MCP call
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    """Either a CallToolResult, or the origin and text of a transport failure."""

    result: Any = None
    origin: str | None = None
    message: str = ""


async def _call_tool(
    mcp: Any,
    record: ContextForgeToolRecord,
    args: dict[str, Any],
) -> _CallOutcome:
    """One tools/call with the original ContextForge name, never the cf__ one."""
    try:
        result = await mcp.call_tool(record.name, dict(args), None)
    except Exception as exc:  # noqa: BLE001 - classified, then reported as a result
        origin = _exception_origin(exc)
        logger.warning("ContextForge tools/call failed for %s (%s)", record.name, origin)
        return _CallOutcome(
            origin=origin,
            message=f"ContextForge call failed ({origin}): {_exception_text(exc)}",
        )
    return _CallOutcome(result=result)


def _exception_origin(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ContextForgeTimeout)):
        return ORIGIN_TIMEOUT
    name = type(exc).__name__.casefold()
    if any(marker in name for marker in ("timeout", "timedout", "deadline")):
        return ORIGIN_TIMEOUT
    if any(marker in _exception_text(exc).casefold() for marker in _TIMEOUT_MARKERS):
        return ORIGIN_TIMEOUT
    return ORIGIN_TRANSPORT_ERROR


def _exception_text(exc: BaseException) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail[:300]}" if detail else type(exc).__name__


def _failure_origin(text: str) -> str:
    """An isError we neither refresh nor retry, named for the audit trail."""
    folded = text.casefold()
    if any(marker in folded for marker in _TIMEOUT_MARKERS):
        return ORIGIN_TIMEOUT
    return ORIGIN_TOOL_ERROR


def _upstream_origin(text: str) -> str:
    """Name an upstream failure that was already retried once."""
    kind = classify_tool_error(text)
    if kind is RefreshableToolError.NOT_FOUND:
        return ORIGIN_TOOL_NOT_FOUND
    if kind is RefreshableToolError.INVALID_ARGUMENTS:
        return ORIGIN_INVALID_ARGUMENTS
    return _failure_origin(text)


# --------------------------------------------------------------------------- #
# Refresh + re-read, and the schema-change comparison
# --------------------------------------------------------------------------- #


async def _reread_after_refresh(
    execution: Any,
    store: Any,
    call_name: str,
    owner: str | None,
) -> tuple[ContextForgeToolRecord | None, bool]:
    if not await _refresh_catalog(execution):
        return None, False
    try:
        return await store.get_by_call_name(call_name, owner), True
    except Exception:  # noqa: BLE001 - the caller keeps its original error
        logger.warning("ContextForge catalog re-read failed", exc_info=True)
        return None, True


async def _refresh_catalog(execution: Any) -> bool:
    """One awaited, bounded refresh; False means the caller keeps its error."""
    refresh = getattr(execution, "refresh_now", None)
    if refresh is not None:
        call = refresh(REFRESH_REASON)
    else:
        request = getattr(execution, "request_refresh", None)
        if request is None:
            return False
        call = request(REFRESH_REASON)
    try:
        stats = await asyncio.wait_for(call, timeout=_refresh_timeout(execution))
    except Exception:  # noqa: BLE001 - a failed fallback is not a run failure
        logger.warning(
            "ContextForge refresh after %s failed", REFRESH_REASON, exc_info=True
        )
        return False
    if _refresh_failed(stats):
        # refresh_now reports an upstream/database failure as stats.error instead
        # of raising, so returning normally is not evidence the snapshot changed.
        logger.warning(
            "ContextForge refresh after %s failed: %s",
            REFRESH_REASON,
            getattr(stats, "error", None),
        )
        return False
    logger.info("ContextForge catalog refreshed after %s", REFRESH_REASON)
    return True


def _refresh_failed(stats: Any) -> bool:
    """Did this sync leave the stored snapshot in place?

    ``ContextForgeSyncStats.error`` means the snapshot was not replaced, so the
    re-read that follows would only see the stale row.  An ``index_failed``
    error is the exception: the DB snapshot was committed and only the derived
    Milvus index failed, so the catalog tool_execute reads is already fresh.
    """
    error = getattr(stats, "error", None)
    if not error:
        return False
    return not bool(getattr(stats, "index_failed", False))


def _refresh_timeout(execution: Any) -> float:
    config = getattr(execution, "config", None)
    sync = getattr(config, "sync", None)
    value = getattr(sync, "tool_error_refresh_timeout_seconds", None)
    try:
        timeout = (
            float(value) if value is not None else DEFAULT_REFRESH_TIMEOUT_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_REFRESH_TIMEOUT_SECONDS


def _schema_changed(
    before: ContextForgeToolRecord, after: ContextForgeToolRecord
) -> bool:
    if before.schema_hash and after.schema_hash:
        return before.schema_hash != after.schema_hash
    return schema_hash_for(before.input_schema) != schema_hash_for(after.input_schema)


def _record_retry(execution: Any, call_name: str, ok: bool) -> None:
    record = getattr(execution, "record_retry", None)
    if not callable(record):
        return
    try:
        record(tool_name=call_name, result="success" if ok else "failed")
    except Exception:  # noqa: BLE001 - status bookkeeping must not fail the call
        logger.debug("record_retry failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Local validation
# --------------------------------------------------------------------------- #


def _validation_problem(
    record: ContextForgeToolRecord, args: dict[str, Any]
) -> str | None:
    """The local argument/schema mismatch, or None when we cannot decide.

    A schema we cannot compile, or one whose $ref cannot be resolved without
    fetching a URL, is not the model's fault: it is handed to ContextForge,
    which validates the same arguments on its side anyway.
    """
    schema = record.input_schema if isinstance(record.input_schema, dict) else {}
    if not schema:
        return None
    try:
        validator = (
            Draft202012Validator(schema, registry=_EMPTY_REGISTRY)
            if _EMPTY_REGISTRY is not None
            else Draft202012Validator(schema)
        )
        errors = sorted(validator.iter_errors(args), key=_error_sort_key)
    except Exception as exc:  # noqa: BLE001 - defer to ContextForge's validation
        logger.info(
            "cannot validate %s locally (%s); deferring to ContextForge",
            record.call_name,
            type(exc).__name__,
        )
        return None
    if not errors:
        return None
    return _describe_errors(errors)


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
    record: ContextForgeToolRecord,
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
    record: ContextForgeToolRecord | None = None,
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
    record: ContextForgeToolRecord | None,
    origin: str | None,
    call_name: str = "",
) -> ToolMessage:
    """myflow_tool_meta must name the tool that ran, not tool_execute.

    The status/error classification is the middleware's own function, reused
    here so a tool_execute reply and a normal tool reply describe failures the
    same way; the identity fields ride along in the same dict.
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


def _not_found_message(call_name: str, refreshed: bool) -> str:
    if refreshed:
        return (
            f"{call_name} is not in the ContextForge catalog for this user: it "
            "does not exist, or it is disabled, unreachable, unowned or otherwise "
            "not callable. The catalog was just refreshed. Do not repeat this "
            "call; search again with tool_search or tell the user."
        )
    return (
        f"{call_name} is not in the ContextForge catalog for this user and the "
        "catalog refresh did not complete. Do not guess another call name; "
        "search again with tool_search or tell the user."
    )


def _inner_name(call_name: str) -> str:
    if call_name.startswith(CALL_NAME_PREFIX):
        return call_name[len(CALL_NAME_PREFIX) :]
    return call_name


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
