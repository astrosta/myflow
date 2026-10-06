"""Repair dangling tool calls and drop orphan tool results before the provider sees them."""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

logger = logging.getLogger(__name__)

_MAX_RECOVERY_ERROR_DETAIL_LEN = 500
_UNKNOWN_TOOL_NAME = "unknown_tool"
_EMPTY_TOOL_NAME_ERROR = "Tool call could not be executed because its name was missing or empty."
_SYNTHETIC_TOOL_CALL_ID_PREFIX = "deerflow_synthetic_tool_call_"


def synthetic_tool_call_id(msg_index: int, source: str, position: int) -> str:
    """source 取 call | invalid | raw。同一三元组两次 wrap 得到同一个 id。"""
    return f"{_SYNTHETIC_TOOL_CALL_ID_PREFIX}{msg_index}_{source}_{position}"


def _valid_tool_name(name: object) -> bool:
    return isinstance(name, str) and bool(name.strip())


def _valid_tool_call_id(tool_call_id: object) -> bool:
    return isinstance(tool_call_id, str) and bool(tool_call_id.strip())


def _tool_call_name(tool_call: dict) -> object:
    name = tool_call.get("name")
    if _valid_tool_name(name):
        return name
    function = tool_call.get("function")
    return function.get("name") if isinstance(function, dict) else name


def _names_can_pair(call_name: object, result_name: object) -> bool:
    if not _valid_tool_name(call_name) or not _valid_tool_name(result_name):
        return True
    return call_name.strip() == result_name.strip()


def _relabel_tool_call_ids(tool_calls: list, msg_index: int, source: str) -> tuple[list, list[dict], bool]:
    relabeled: list = []
    assigned: list[dict] = []
    changed = False
    for position, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict) or _valid_tool_call_id(tool_call.get("id")):
            relabeled.append(tool_call)
            continue
        synthetic = synthetic_tool_call_id(msg_index, source, position)
        relabeled.append({**tool_call, "id": synthetic})
        changed = True
        assigned.append({"original": tool_call.get("id"), "synthetic": synthetic, "name": _tool_call_name(tool_call)})
    return relabeled, assigned, changed


def _turn_malformed_result_count(messages: list, start: int) -> int:
    count = 0
    for message in messages[start + 1 :]:
        if getattr(message, "type", None) == "ai":
            break
        if isinstance(message, ToolMessage) and not _valid_tool_call_id(message.tool_call_id):
            count += 1
    return count


def _claim_synthetic_id(open_calls: list[dict], result: ToolMessage, positional: bool) -> str | None:
    candidates = [
        position
        for position, entry in enumerate(open_calls)
        if entry["original"] == result.tool_call_id and _names_can_pair(entry["name"], result.name)
    ]
    if not candidates or (len(candidates) > 1 and not positional):
        return None
    return open_calls.pop(candidates[0])["synthetic"]


def _normalize_tool_name(name: object) -> str:
    return name.strip() if _valid_tool_name(name) else _UNKNOWN_TOOL_NAME


def _has_invalid_tool_name(name: object) -> bool:
    return not _valid_tool_name(name)


def _parse_json_object(value: object) -> dict | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_tool_arguments(arguments: object) -> str:
    if isinstance(arguments, dict):
        try:
            return json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return "{}"
    return arguments if _parse_json_object(arguments) is not None else "{}"


class DanglingToolCallMiddleware(AgentMiddleware[AgentState]):
    """无构造参数。占位紧跟对应的 AIMessage，并用整表替换写回 state。"""

    @staticmethod
    def _message_tool_calls(message) -> list[dict]:
        normalized: list[dict] = []
        tool_calls = getattr(message, "tool_calls", None) or []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            original_name = tool_call.get("name")
            normalized_call = dict(tool_call)
            normalized_call["name"] = _normalize_tool_name(original_name)
            if _has_invalid_tool_name(original_name):
                normalized_call["invalid_tool_name"] = True
            normalized.append(normalized_call)

        raw_tool_calls = (getattr(message, "additional_kwargs", None) or {}).get("tool_calls") or []
        if not tool_calls and not getattr(message, "invalid_tool_calls", None):
            for raw_tool_call in raw_tool_calls:
                if not isinstance(raw_tool_call, dict):
                    continue
                function = raw_tool_call.get("function")
                name = raw_tool_call.get("name")
                if not name and isinstance(function, dict):
                    name = function.get("name")
                args = raw_tool_call.get("args", {})
                if not args and isinstance(function, dict):
                    parsed_args = _parse_json_object(function.get("arguments"))
                    args = parsed_args if parsed_args is not None else {}
                normalized_call = {
                    "id": raw_tool_call.get("id"),
                    "name": _normalize_tool_name(name),
                    "args": args if isinstance(args, dict) else {},
                }
                if _has_invalid_tool_name(name):
                    normalized_call["invalid_tool_name"] = True
                normalized.append(normalized_call)

        for invalid_tool_call in getattr(message, "invalid_tool_calls", None) or []:
            if not isinstance(invalid_tool_call, dict):
                continue
            original_name = invalid_tool_call.get("name")
            normalized_call = {
                "id": invalid_tool_call.get("id"),
                "name": _normalize_tool_name(original_name),
                "args": {},
                "invalid": True,
                "error": invalid_tool_call.get("error"),
            }
            if _has_invalid_tool_name(original_name):
                normalized_call["invalid_tool_name"] = True
            normalized.append(normalized_call)
        return normalized

    @staticmethod
    def _synthetic_tool_message_content(tool_call: dict) -> str:
        if tool_call.get("invalid_tool_name"):
            return f"[{_EMPTY_TOOL_NAME_ERROR} Use one of the available tool names when retrying.]"
        if tool_call.get("invalid"):
            name = tool_call.get("name")
            error = tool_call.get("error")
            error_text = error[:_MAX_RECOVERY_ERROR_DETAIL_LEN] if isinstance(error, str) and error else ""
            if name == "write_file":
                details = f" Parser error: {error_text}" if error_text else ""
                return (
                    "[write_file failed before execution: the tool-call arguments were not valid JSON, "
                    "so no file was written. This often happens when the model tries to write a very "
                    "large Markdown file in a single tool call, especially when `content` contains "
                    "unescaped quotes, inline JSON, backslashes, or code fences. Do not retry the same "
                    "large `write_file` payload for this artifact; provide the report/content directly "
                    "as normal assistant text in your next response. If a file write is still needed "
                    f"later, split the file into smaller sections instead of one large payload.{details}]"
                )
            if error_text:
                return f"[Tool call could not be executed because its arguments were invalid: {error_text}]"
            return "[Tool call could not be executed because its arguments were invalid.]"
        return "[Tool call was interrupted and did not return a result.]"

    @staticmethod
    def _sanitize_ai_message_tool_calls(message):
        if getattr(message, "type", None) != "ai":
            return message

        changed = False
        update: dict = {}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            structured_changed = False
            sanitized_tool_calls = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    sanitized_tool_calls.append(tool_call)
                    continue
                sanitized = dict(tool_call)
                normalized_name = _normalize_tool_name(sanitized.get("name"))
                if sanitized.get("name") != normalized_name:
                    sanitized["name"] = normalized_name
                    structured_changed = True
                sanitized_tool_calls.append(sanitized)
            if structured_changed:
                update["tool_calls"] = sanitized_tool_calls
                changed = True

        invalid_tool_calls = getattr(message, "invalid_tool_calls", None)
        if invalid_tool_calls:
            invalid_changed = False
            sanitized_invalid_tool_calls = []
            for invalid_tool_call in invalid_tool_calls:
                if not isinstance(invalid_tool_call, dict):
                    sanitized_invalid_tool_calls.append(invalid_tool_call)
                    continue
                sanitized = dict(invalid_tool_call)
                normalized_name = _normalize_tool_name(sanitized.get("name"))
                normalized_arguments = _normalize_tool_arguments(sanitized.get("args"))
                if sanitized.get("name") != normalized_name:
                    sanitized["name"] = normalized_name
                    invalid_changed = True
                if sanitized.get("args") != normalized_arguments:
                    sanitized["args"] = normalized_arguments
                    invalid_changed = True
                sanitized_invalid_tool_calls.append(sanitized)
            if invalid_changed:
                update["invalid_tool_calls"] = sanitized_invalid_tool_calls
                changed = True

        additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
        raw_tool_calls = additional_kwargs.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            raw_changed = False
            sanitized_raw_tool_calls = []
            for raw_tool_call in raw_tool_calls:
                if not isinstance(raw_tool_call, dict):
                    sanitized_raw_tool_calls.append(raw_tool_call)
                    continue
                sanitized_raw = dict(raw_tool_call)
                function = sanitized_raw.get("function")
                if isinstance(function, dict):
                    sanitized_function = dict(function)
                    normalized_name = _normalize_tool_name(sanitized_function.get("name"))
                    normalized_arguments = _normalize_tool_arguments(sanitized_function.get("arguments"))
                    if sanitized_function.get("name") != normalized_name:
                        sanitized_function["name"] = normalized_name
                        raw_changed = True
                    if sanitized_function.get("arguments") != normalized_arguments:
                        sanitized_function["arguments"] = normalized_arguments
                        raw_changed = True
                    if sanitized_function != function:
                        sanitized_raw["function"] = sanitized_function
                else:
                    normalized_name = _normalize_tool_name(sanitized_raw.get("name"))
                    if sanitized_raw.get("name") != normalized_name:
                        sanitized_raw["name"] = normalized_name
                        raw_changed = True
                sanitized_raw_tool_calls.append(sanitized_raw)
            if raw_changed:
                additional_kwargs["tool_calls"] = sanitized_raw_tool_calls
                update["additional_kwargs"] = additional_kwargs
                changed = True

        if not changed:
            return message
        return message.model_copy(update=update)

    @staticmethod
    def _normalize_tool_call_ids(messages: list) -> list:
        rewritten: dict[int, object] = {}
        open_calls: list[dict] = []
        positional = False

        for index, message in enumerate(messages):
            if getattr(message, "type", None) == "ai":
                update: dict = {}
                assigned: list[dict] = []
                structured = getattr(message, "tool_calls", None) or []
                additional_kwargs = getattr(message, "additional_kwargs", None) or {}
                raw_tool_calls = additional_kwargs.get("tool_calls")
                invalid = getattr(message, "invalid_tool_calls", None) or []
                sources: list[tuple[str, list, str]] = [
                    ("call", structured, "tool_calls"),
                    ("invalid", invalid, "invalid_tool_calls"),
                ]
                if not structured and not invalid and isinstance(raw_tool_calls, list):
                    sources.append(("raw", raw_tool_calls, "additional_kwargs"))

                for source, tool_calls, field in sources:
                    relabeled, source_assigned, changed = _relabel_tool_call_ids(tool_calls, index, source)
                    assigned.extend(source_assigned)
                    if not changed:
                        continue
                    update[field] = {**additional_kwargs, "tool_calls": relabeled} if field == "additional_kwargs" else relabeled

                open_calls = assigned
                positional = _turn_malformed_result_count(messages, index) == len(assigned)
                if update:
                    rewritten[index] = message.model_copy(update=update)
                continue

            if not isinstance(message, ToolMessage) or _valid_tool_call_id(message.tool_call_id):
                continue
            synthetic = _claim_synthetic_id(open_calls, message, positional)
            if synthetic is not None:
                rewritten[index] = message.model_copy(update={"tool_call_id": synthetic})

        if not rewritten:
            return messages
        return [rewritten.get(index, message) for index, message in enumerate(messages)]

    def _patch(self, messages: list) -> tuple[list, int, int]:
        normalized = self._normalize_tool_call_ids(messages)
        tool_messages_by_id: dict[str, deque[ToolMessage]] = defaultdict(deque)
        for message in normalized:
            if isinstance(message, ToolMessage):
                tool_messages_by_id[message.tool_call_id].append(message)

        tool_call_ids: set[str] = set()
        for message in normalized:
            if getattr(message, "type", None) != "ai":
                continue
            for tool_call in self._message_tool_calls(message):
                tool_call_id = tool_call.get("id")
                if tool_call_id:
                    tool_call_ids.add(tool_call_id)

        patched: list = []
        patch_count = 0
        drop_count = 0
        for message in normalized:
            if isinstance(message, ToolMessage):
                if message.tool_call_id in tool_call_ids:
                    continue
                drop_count += 1
                continue

            sanitized = self._sanitize_ai_message_tool_calls(message)
            patched.append(sanitized)
            if getattr(message, "type", None) != "ai":
                continue

            for tool_call in self._message_tool_calls(message):
                tool_call_id = tool_call.get("id")
                if not tool_call_id:
                    continue
                queue = tool_messages_by_id.get(tool_call_id)
                existing = queue.popleft() if queue else None
                if existing is not None:
                    if tool_call.get("invalid_tool_name") and _has_invalid_tool_name(existing.name):
                        existing = existing.model_copy(update={"name": tool_call["name"]})
                    patched.append(existing)
                else:
                    patched.append(
                        ToolMessage(
                            content=self._synthetic_tool_message_content(tool_call),
                            tool_call_id=tool_call_id,
                            name=tool_call.get("name", "unknown"),
                            status="error",
                        )
                    )
                    patch_count += 1
        return patched, patch_count, drop_count

    def _build_patched_messages(self, messages: list) -> list | None:
        patched, patch_count, drop_count = self._patch(messages)
        if patched == messages and not drop_count:
            return None
        if drop_count or patch_count:
            logger.warning(
                "DanglingToolCallMiddleware: %d orphan(s) dropped, %d placeholder(s) injected",
                drop_count,
                patch_count,
            )
        return patched

    def _state_update(self, messages: list) -> dict | None:
        patched, patch_count, drop_count = self._patch(messages)
        if patch_count <= 0 and drop_count <= 0:
            return None
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *patched]}

    def before_model(self, state, runtime) -> dict | None:
        return self._state_update(state.get("messages") or [])

    async def abefore_model(self, state, runtime) -> dict | None:
        return self._state_update(state.get("messages") or [])

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(list(request.messages))
        if patched is not None:
            request = request.override(messages=patched)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(list(request.messages))
        if patched is not None:
            request = request.override(messages=patched)
        return await handler(request)
