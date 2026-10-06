import http
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agentkit.utils.messages import message_content_to_text

TOOL_META_KEY = "myflow_tool_meta"
_ERROR_PREFIX = "Error:"
_SEMANTIC_ZERO_ERROR_STRINGS = frozenset({"none", "null", "false", "no", "ok", "success", "n/a", ""})
_PARTIAL_MARKERS = (
    "partial results",
    "limited results",
    "truncated",
    "results may be incomplete",
    "no results found",
    "no content found",
    "no images found",
)
_PAGE_CONTENT_TOOL_NAMES = frozenset({"web_fetch"})
_SUBAGENT_FAILURES = frozenset({"failed", "cancelled", "timed_out", "polling_timed_out"})
_TITLE_FILLERS = frozenset({"http", "https", "error", "page", "file"})
_TITLE_TOKENS = re.compile(r"[A-Za-z0-9]+")

_NextAction = Literal["continue", "rewrite_query", "try_alternative", "summarize", "stop"]
_Source = Literal["exception", "tool_return", "content_analysis", "progress_middleware"]
_Status = Literal["success", "error", "partial_success"]

_ERROR_RULES: tuple[tuple[tuple[str, ...], str, bool, _NextAction], ...] = (
    (("401", "403", "unauthorized", "invalid api key"), "auth", False, "stop"),
    (("rate limit",), "rate_limited", False, "summarize"),
    (("timeout", "network", "502", "503", "504"), "transient", False, "try_alternative"),
    (("not configured", "missing required", "no api key"), "config", False, "stop"),
    (("permission denied", "path traversal"), "permission", True, "try_alternative"),
    (("no results found",), "no_results", True, "rewrite_query"),
    (("not found", "no such file", "404"), "not_found", True, "rewrite_query"),
    (("internal error", "500"), "internal", False, "stop"),
)


@dataclass(frozen=True, slots=True)
class ToolResultMeta:
    status: _Status
    error_type: str | None
    recoverable_by_model: bool
    recommended_next_action: _NextAction
    source: _Source


def _compiled_rules() -> tuple[tuple[tuple[re.Pattern[str], ...], tuple[str, ...], str, bool, _NextAction], ...]:
    compiled = []
    for keywords, error_type, recoverable, action in _ERROR_RULES:
        numeric = tuple(re.compile(rf"\b{keyword}\b") for keyword in keywords if keyword.isdigit())
        phrases = tuple(keyword.casefold() for keyword in keywords if not keyword.isdigit())
        compiled.append((numeric, phrases, error_type, recoverable, action))
    return tuple(compiled)


_COMPILED_RULES = _compiled_rules()


def _normalized_http_title(line: str) -> str:
    tokens = _TITLE_TOKENS.findall(line)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.isdigit() or token.casefold() in _TITLE_FILLERS:
            index += 1
            continue
        break
    return " ".join(token.casefold() for token in tokens[index:])


_HTTP_ERROR_TITLES = frozenset(
    normalized
    for status in http.HTTPStatus
    if 400 <= status.value < 600 and (normalized := _normalized_http_title(status.phrase))
)

_SUCCESS = ToolResultMeta(
    status="success",
    error_type=None,
    recoverable_by_model=True,
    recommended_next_action="continue",
    source="content_analysis",
)
_PARTIAL = ToolResultMeta(
    status="partial_success",
    error_type=None,
    recoverable_by_model=True,
    recommended_next_action="rewrite_query",
    source="content_analysis",
)


def normalize_tool_result(
    result: ToolMessage | Command,
    *,
    tool_call_id: str = "",
) -> ToolMessage | Command:
    """已有 TOOL_META_KEY 的 ToolMessage 原样返回。Command 只盖 tool_call_id 对得上的那条。"""
    if isinstance(result, ToolMessage):
        return normalize_tool_message(result)
    if isinstance(result, Command):
        return _normalize_command(result, tool_call_id)
    return result


def stamp_exception_meta(msg: ToolMessage, exc_info: str) -> ToolMessage:
    """异常路径。总是覆盖已有的 TOOL_META_KEY，source='exception'。"""
    return _with_meta(msg, _error_meta(exc_info, source="exception"))


def normalize_tool_message(message: ToolMessage) -> ToolMessage:
    kwargs = message.additional_kwargs
    if isinstance(kwargs, dict) and TOOL_META_KEY in kwargs:
        return message
    if isinstance(kwargs, dict) and kwargs.get("subagent_status") in _SUBAGENT_FAILURES:
        detail = kwargs.get("subagent_error")
        text = detail if isinstance(detail, str) and detail.strip() else message_content_to_text(message.content)
        return _with_meta(message, _error_meta(text, source="tool_return"))
    return _with_meta(message, _decide(message))


def _decide(message: ToolMessage) -> ToolResultMeta:
    text = message_content_to_text(message.content)
    if message.status == "error" and not text.startswith(_ERROR_PREFIX):
        parsed = _json_error(text)
        if parsed is None:
            return _error_meta(text, source="tool_return")
        kind, extracted = parsed
        if kind == "text":
            return _error_meta(extracted, source="tool_return")
        if kind != "zero":
            return _unknown_error("tool_return")
    if text.startswith(_ERROR_PREFIX):
        return _error_meta(text[len(_ERROR_PREFIX) :].strip(), source="tool_return")
    parsed = _json_error(text)
    if parsed is not None and parsed[0] == "text":
        return _error_meta(parsed[1], source="tool_return")
    if message.name in _PAGE_CONTENT_TOOL_NAMES and _is_http_error_title(text):
        return _error_meta(text.split("\n", 1)[0], source="content_analysis")
    if any(marker in text.casefold() for marker in _PARTIAL_MARKERS):
        return _PARTIAL
    return _SUCCESS


def _normalize_command(command: Command, tool_call_id: str) -> Command:
    update = command.update
    if not isinstance(update, dict):
        return command
    messages = update.get("messages")
    if isinstance(messages, ToolMessage):
        items: list[object] = [messages]
        single = True
    elif isinstance(messages, (list, tuple)):
        items = list(messages)
        single = False
    else:
        return command

    stamped: list[object] = []
    changed = False
    for item in items:
        if isinstance(item, ToolMessage) and (not tool_call_id or item.tool_call_id == tool_call_id):
            updated = normalize_tool_message(item)
            stamped.append(updated)
            changed = changed or updated is not item
        else:
            stamped.append(item)
    if not changed:
        return command

    if single:
        new_messages: object = stamped[0]
    elif isinstance(messages, tuple):
        new_messages = tuple(stamped)
    else:
        new_messages = stamped
    return Command(
        update={**update, "messages": new_messages},
        goto=command.goto,
        graph=command.graph,
        resume=command.resume,
    )


def _with_meta(message: ToolMessage, meta: ToolResultMeta) -> ToolMessage:
    kwargs = dict(message.additional_kwargs)
    kwargs[TOOL_META_KEY] = asdict(meta)
    return message.model_copy(update={"additional_kwargs": kwargs})


def _classify(text: str) -> tuple[str, bool, _NextAction]:
    folded = text.casefold()
    for patterns, phrases, error_type, recoverable, action in _COMPILED_RULES:
        if any(pattern.search(text) for pattern in patterns) or any(phrase in folded for phrase in phrases):
            return error_type, recoverable, action
    return "unknown", True, "try_alternative"


def _error_meta(text: str, *, source: _Source) -> ToolResultMeta:
    error_type, recoverable, action = _classify(text)
    return ToolResultMeta(
        status="error",
        error_type=error_type,
        recoverable_by_model=recoverable,
        recommended_next_action=action,
        source=source,
    )


def _unknown_error(source: _Source) -> ToolResultMeta:
    return ToolResultMeta(
        status="error",
        error_type="unknown",
        recoverable_by_model=True,
        recommended_next_action="try_alternative",
        source=source,
    )


def _json_error(content: str) -> tuple[str, str] | None:
    stripped = content.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return ("not_object", "")
    if "error" not in payload:
        return ("missing", "")
    value = payload["error"]
    if value is None or value is False:
        return ("zero", "")
    if isinstance(value, str):
        if value.strip().casefold() in _SEMANTIC_ZERO_ERROR_STRINGS:
            return ("zero", "")
        return ("text", value)
    if isinstance(value, bool):
        return ("text", "true")
    if isinstance(value, int | float):
        return ("text", str(value))
    return ("unusable", "")


def _is_http_error_title(content: str) -> bool:
    first_line = content.split("\n", 1)[0]
    return _normalized_http_title(first_line) in _HTTP_ERROR_TITLES
