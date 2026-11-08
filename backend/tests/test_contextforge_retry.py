"""The refresh-error vocabulary tool_execute reports its failures in.

Task 7 moved the one-retry fallback into tool_execute, and Task 8 deleted the
middleware that used to wrap it.  The executor refreshes on a missing tool,
answers a schema/argument mismatch with the new schema and never retries the old
arguments, and leaves every error that may already have run upstream alone
(design 9.1).  What is left for this file is the vocabulary itself plus its one
exception: an error that already carries ``origin=local_validation`` went through
the executor's own fallback, so it must not be classified as refreshable even
though its wording says "invalid arguments".

``owns_retry_fallback`` is the caller-side half of the same rule: a tool whose
replies use this vocabulary is self-healing, so a wrapper must not refresh a
second time and re-run tools/call for the same request (9.1 C.6).  Task 8 removed
the last production wrapper, so the predicate is currently unused - its contract
stays pinned because the next wrapper needs exactly this check, and re-deriving
"which tools are special" is how the duplicate upstream call comes back.

The signatures the executor actually calls on the real ``ContextForgeRuntime``
are pinned at the bottom.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json

from langchain_core.messages import ToolMessage

from agentkit.config.contextforge_config import ContextForgeSyncConfig
from agentkit.contextforge.retry import (
    RefreshableToolError,
    classify_tool_error,
    owns_retry_fallback,
)
from agentkit.contextforge.runtime import ContextForgeRuntime
from agentkit.contextforge.sync import ContextForgeSyncStats


def _local_validation_message() -> ToolMessage:
    """The reply tool_execute produces for a locally invalid argument set."""
    payload = {
        "error": "arguments do not match the stored input_schema: invalid arguments",
        "origin": "local_validation",
        "retryable": True,
    }
    return ToolMessage(
        content=json.dumps(payload),
        tool_call_id="call-1",
        name="tool_execute",
        status="error",
    )


def test_classifier_only_retries_not_found_and_invalid_args() -> None:
    assert classify_tool_error(RuntimeError("tool not found")) is RefreshableToolError.NOT_FOUND
    assert classify_tool_error(RuntimeError("invalid arguments")) is RefreshableToolError.INVALID_ARGUMENTS
    assert classify_tool_error(RuntimeError("request timeout")) is RefreshableToolError.NONE
    assert classify_tool_error(RuntimeError("unauthorized")) is RefreshableToolError.NONE
    assert classify_tool_error(ToolMessage(content="参数校验失败", tool_call_id="c")) is RefreshableToolError.INVALID_ARGUMENTS


def test_a_local_validation_error_is_not_refreshable() -> None:
    assert (
        classify_tool_error(_local_validation_message()) is RefreshableToolError.NONE
    ), "the executor already ran its fallback; a second refresh is not a fix"
    assert classify_tool_error("invalid arguments") is RefreshableToolError.INVALID_ARGUMENTS


def test_only_the_executor_is_a_self_healing_tool() -> None:
    assert owns_retry_fallback("tool_execute")
    assert owns_retry_fallback(" Tool_Execute "), "the check is normalized"
    assert not owns_retry_fallback("cf__github_search")
    assert not owns_retry_fallback("tool_search")
    assert not owns_retry_fallback(None)


# --------------------------------------------------------------------------- #
# The real runtime, not a double: the executor's calls must keep existing
# --------------------------------------------------------------------------- #


def test_the_real_runtime_exposes_the_signatures_the_executor_calls() -> None:
    """tool_execute calls ContextForgeRuntime by name.

    A double cannot drift; the production class can.  These assertions read the
    real class, so renaming ``refresh_now``, losing the ``store``/``mcp_client``
    properties or making ``record_retry`` positional fails here instead of turning
    into a silent getattr fallback at runtime.
    """
    refresh = inspect.signature(ContextForgeRuntime.refresh_now).parameters
    assert asyncio.iscoroutinefunction(ContextForgeRuntime.refresh_now)
    assert list(refresh) == ["self", "reason"]
    assert (
        refresh["reason"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ), "the executor passes the reason positionally: refresh_now(REFRESH_REASON)"

    request = inspect.signature(ContextForgeRuntime.request_refresh).parameters
    assert asyncio.iscoroutinefunction(ContextForgeRuntime.request_refresh)
    assert list(request) == ["self", "reason"], (
        "request_refresh is the documented fallback, called positionally too"
    )
    assert request["reason"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    retry = inspect.signature(ContextForgeRuntime.record_retry).parameters
    assert list(retry) == ["self", "tool_name", "result"]
    assert retry["tool_name"].kind is inspect.Parameter.KEYWORD_ONLY, (
        "the executor calls record_retry(tool_name=..., result=...)"
    )
    assert retry["result"].kind is inspect.Parameter.KEYWORD_ONLY

    for attribute in ("store", "mcp_client"):
        holder = inspect.getattr_static(ContextForgeRuntime, attribute, None)
        assert isinstance(holder, property), (
            f"tool_execute reads runtime.{attribute}; it must stay a property"
        )

    fields = {field.name for field in dataclasses.fields(ContextForgeSyncStats)}
    assert {"error", "index_failed"} <= fields, (
        "the executor decides whether a refresh replaced the snapshot from these"
    )

    timeout_field = "tool_error_refresh_timeout_seconds"
    assert timeout_field in ContextForgeSyncConfig.model_fields, (
        "the refresh budget the executor reads from config.sync must exist"
    )
