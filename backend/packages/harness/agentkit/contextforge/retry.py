"""Strict classification for tool errors that may be fixed by a refresh.

Two callers share this vocabulary.  The tool-error retry middleware uses it to
decide whether an error may be worth one catalog refresh and one retry;
``tool_execute`` uses the same classes to pick its own fallback (design 9.1).

That is why ``origin=local_validation`` is here.  When tool_execute's own local
jsonschema check fails, it has *already* refreshed and answered with the new
schema, and the model has to reorganise the arguments before anything is sent
upstream.  A second refresh-and-retry on top of that would run a fallback that
already ran (and, for a tool that is also invalid upstream, execute it again), so
the marker is classified as not refreshable - it outranks the generic
"invalid arguments" wording of the message it carries.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class RefreshableToolError(Enum):
    NONE = "none"
    NOT_FOUND = "not_found"
    INVALID_ARGUMENTS = "invalid_arguments"


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
#: An error tool_execute produced and already handled: the executor owns its
#: fallback, so a middleware must not refresh or retry on top of it.
_LOCAL_VALIDATION_MARKERS = (
    "local_validation",
    "origin=local_validation",
)
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


def classify_tool_error(value: Any) -> RefreshableToolError:
    text = _text(value).casefold()
    if not text:
        return RefreshableToolError.NONE
    if any(marker in text for marker in _NEVER_RETRY_MARKERS):
        return RefreshableToolError.NONE
    if any(marker in text for marker in _LOCAL_VALIDATION_MARKERS):
        return RefreshableToolError.NONE
    if any(marker in text for marker in _NOT_FOUND_MARKERS):
        return RefreshableToolError.NOT_FOUND
    if any(marker in text for marker in _INVALID_ARGUMENT_MARKERS):
        return RefreshableToolError.INVALID_ARGUMENTS
    return RefreshableToolError.NONE


def _text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return str(content or "")


__all__ = ["RefreshableToolError", "classify_tool_error"]
