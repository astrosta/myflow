from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.leader_agent import (
    ASSEMBLY_INPUT_KEYS,
    AgentAssembly,
    AgentCacheKey,
    agent_cache_key,
    assemble_agent,
)
from agentkit.config import AppConfig, get_app_config
from agentkit.runtime.checkpointer import get_checkpointer

logger = logging.getLogger(__name__)

StreamEventType = Literal["values", "messages-tuple", "custom", "end"]

# LangGraph 内部 mode 名。对外事件把 messages 写成 messages-tuple。
LANGGRAPH_STREAM_MODES = ["values", "messages", "custom"]

_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


@dataclass
class StreamEvent:
    type: StreamEventType
    data: dict[str, Any] = field(default_factory=dict)


def translate_stream(agent_items: Iterable[Any]) -> Iterator[StreamEvent]:
    """Translate LangGraph stream items into the public stream contract.

    Subscribe with ``stream_mode=["values", "messages", "custom"]``.
    Emitted message deltas use ``messages-tuple``, not the internal mode name.
    """
    yield from _StreamTranslator().translate(agent_items)


class _StreamTranslator:
    def __init__(self) -> None:
        self.seen_ids: set[str] = set()
        self.streamed_ids: set[str] = set()
        self.counted_usage_ids: set[str] = set()
        self.sent_additional_kwargs_by_id: dict[str, dict[str, Any]] = {}
        self._emitted_tool_call_ids: set[str] = set()
        self._usage = {key: 0 for key in _USAGE_KEYS}

    def translate(self, agent_items: Iterable[Any]) -> Iterator[StreamEvent]:
        for item in agent_items:
            yield from self._translate_item(item)
        yield StreamEvent(type="end", data={"usage": dict(self._usage)})

    def _translate_item(self, item: Any) -> Iterator[StreamEvent]:
        mode, chunk = _unpack_stream_item(item)
        if mode == "custom":
            if not _has_custom_event_type(chunk):
                logger.warning(
                    "Discarding custom stream chunk without a non-empty string type: %r",
                    chunk,
                )
                return
            yield StreamEvent(type="custom", data=dict(chunk))
            return
        if mode == "messages":
            message, _metadata = _unpack_message_chunk(chunk)
            yield from self._translate_message_mode(message)
            return
        # if mode == "values":
        yield from self._translate_values(chunk if isinstance(chunk, dict) else {})

    def _translate_message_mode(self, message: Any) -> Iterator[StreamEvent]:
        if _is_ai(message):
            yield from self._emit_ai(message, tool_calls_first=False)
            return
        if _is_tool(message):
            yield from self._emit_tool(message)

    def _translate_values(self, chunk: dict[str, Any]) -> Iterator[StreamEvent]:
        messages = chunk.get("messages") or []
        for message in messages:
            message_id = _message_id(message)
            if message_id is not None and message_id in self.seen_ids:
                continue
            if message_id is not None:
                self.seen_ids.add(message_id)
            if message_id is not None and message_id in self.streamed_ids:
                self._account_usage(message_id, _message_usage(message))
                continue
            yield from self._synthesize(message)
        yield StreamEvent(
            type="values",
            data={
                "title": chunk.get("title"),
                "summary_text": chunk.get("summary_text"),
                "messages": [_serialize_message(message) for message in messages],
                "artifacts": chunk.get("artifacts") or [],
            },
        )

    def _synthesize(self, message: Any) -> Iterator[StreamEvent]:
        if _is_ai(message):
            yield from self._emit_ai(message, tool_calls_first=True)
            return
        if _is_tool(message):
            yield from self._emit_tool(message)
            return
        message_id = _message_id(message)
        if message_id is not None:
            self.streamed_ids.add(message_id)
        yield StreamEvent(
            type="messages-tuple",
            data={
                "id": message_id,
                "type": _message_type(message),
                "content": _message_content(message),
            },
        )

    def _emit_ai(self, message: Any, *, tool_calls_first: bool) -> Iterator[StreamEvent]:
        message_id = _message_id(message)
        text = extract_text(_message_content(message))
        usage = self._account_usage(message_id, _message_usage(message))
        pending_kwargs = self._unsent_kwargs(message_id, _message_kwargs(message))
        tool_calls = _message_tool_calls(message)
        if message_id is not None and message_id in self._emitted_tool_call_ids:
            tool_calls = []

        steps: list[str] = []
        if tool_calls_first:
            if tool_calls:
                steps.append("tools")
            if text:
                steps.append("text")
        else:
            if text:
                steps.append("text")
            if tool_calls:
                steps.append("tools")
        if not steps and pending_kwargs:
            steps.append("meta")

        for step in steps:
            data: dict[str, Any] = {
                "id": message_id,
                "type": "ai",
                "content": text if step == "text" else "",
            }
            if step == "tools":
                data["tool_calls"] = tool_calls
                if message_id is not None:
                    self._emitted_tool_call_ids.add(message_id)
            if pending_kwargs:
                data["additional_kwargs"] = pending_kwargs
                self._mark_kwargs_sent(message_id, pending_kwargs)
                pending_kwargs = {}
            if usage is not None:
                data["usage"] = usage
                usage = None
            if step != "meta" and message_id is not None:
                self.streamed_ids.add(message_id)
                self.seen_ids.add(message_id)
            yield StreamEvent(type="messages-tuple", data=data)

    def _emit_tool(self, message: Any) -> Iterator[StreamEvent]:
        message_id = _message_id(message)
        if message_id is not None and message_id in self.streamed_ids:
            return
        if message_id is not None:
            self.streamed_ids.add(message_id)
            self.seen_ids.add(message_id)
        yield StreamEvent(
            type="messages-tuple",
            data={
                "id": message_id,
                "type": "tool",
                "content": _message_content(message),
                "name": _message_attr(message, "name"),
                "tool_call_id": _message_attr(message, "tool_call_id"),
            },
        )

    def _account_usage(self, message_id: str | None, usage: Any) -> dict[str, int] | None:
        mapped = _usage_mapping(usage)
        if message_id is None or mapped is None or message_id in self.counted_usage_ids:
            return None
        self.counted_usage_ids.add(message_id)
        for key in _USAGE_KEYS:
            self._usage[key] += mapped[key]
        return mapped

    def _unsent_kwargs(self, message_id: str | None, raw: dict[str, Any]) -> dict[str, Any]:
        if not message_id or not raw:
            return {}
        sent = self.sent_additional_kwargs_by_id.get(message_id, {})
        return {
            key: value
            for key, value in raw.items()
            if key not in sent or sent[key] != value
        }

    def _mark_kwargs_sent(self, message_id: str | None, fresh: dict[str, Any]) -> None:
        if not message_id or not fresh:
            return
        self.sent_additional_kwargs_by_id.setdefault(message_id, {}).update(fresh)


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _has_custom_event_type(chunk: Any) -> bool:
    if not isinstance(chunk, dict):
        return False
    event_type = chunk.get("type")
    return isinstance(event_type, str) and bool(event_type)


def _unpack_stream_item(item: Any) -> tuple[str, Any]:
    """把 LangGraph stream 的一条输出拆成 (mode, payload)。

    v2（``stream(version="v2")``）每条都是字典 ``{"type": mode, "ns": ..., "data": payload}``。
    mode 在 ``type`` 里，真正的状态或消息在 ``data`` 里。``messages`` 的 data 仍是
    ``(message, metadata)``，后面的 ``_unpack_message_chunk`` 再拆第二层。

    v1 多 mode 时是二元组 ``(mode, payload)``。只订一个 mode 时没有 mode 前缀，
    那条输出就是 payload 本身；LangGraph 默认 mode 是 values，所以按状态快照处理。
    """
    if (
        isinstance(item, dict)
        and isinstance(item.get("type"), str)
        and item["type"] in LANGGRAPH_STREAM_MODES
        and "data" in item
    ):
        return item["type"], item["data"]
    if (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and item[0] in LANGGRAPH_STREAM_MODES
    ):
        return item
    return "values", item


def _unpack_message_chunk(chunk: Any) -> tuple[Any, Any]:
    if isinstance(chunk, tuple) and len(chunk) == 2:
        return chunk
    return chunk, None


def _serialize_message(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return dict(message)
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return {
        "id": _message_id(message),
        "type": _message_type(message),
        "content": _message_content(message),
    }


def _is_ai(message: Any) -> bool:
    return isinstance(message, AIMessage) or (
        isinstance(message, dict) and message.get("type") in {"ai", "AIMessage", "AIMessageChunk"}
    )


def _is_tool(message: Any) -> bool:
    return isinstance(message, ToolMessage) or (
        isinstance(message, dict) and message.get("type") in {"tool", "ToolMessage", "ToolMessageChunk"}
    )


def _message_id(message: Any) -> str | None:
    if isinstance(message, dict):
        message_id = message.get("id")
    else:
        message_id = getattr(message, "id", None)
    return message_id if isinstance(message_id, str) else None


def _message_type(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or "unknown")
    return str(getattr(message, "type", None) or "unknown")


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _message_usage(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("usage_metadata")
    return getattr(message, "usage_metadata", None)


def _message_kwargs(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        raw = message.get("additional_kwargs") or {}
    else:
        raw = getattr(message, "additional_kwargs", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _message_tool_calls(message: Any) -> list[Any]:
    if isinstance(message, dict):
        raw = message.get("tool_calls") or []
    else:
        raw = getattr(message, "tool_calls", None) or []
    return list(raw)


def _message_attr(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _usage_mapping(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return None
    return {key: int(usage.get(key) or 0) for key in _USAGE_KEYS}


DEFAULT_USER_ID = "default"


class AgentClient:
    """In-process client. Compiled graphs are reused until an assembly input changes."""

    def __init__(self, *, app_config: AppConfig | None = None, checkpointer: Any = None) -> None:
        self._app_config = app_config
        self._checkpointer = checkpointer
        self._cached: tuple[AgentCacheKey, AgentAssembly] | None = None

    def stream(self, message: str, *, thread_id: str, **overrides: Any) -> Iterator[StreamEvent]:
        app_config = self._app_config if self._app_config is not None else get_app_config()
        checkpointer = self._checkpointer if self._checkpointer is not None else get_checkpointer()
        user_id = self._resolve_user_id()
        config = self._build_config(thread_id, overrides)
        context = {"thread_id": thread_id, "user_id": user_id}
        assembly = self._ensure_agent(
            config,
            app_config=app_config,
            checkpointer=checkpointer,
            user_id=user_id,
        )
        items = assembly.graph.stream(
            self._input_state(message),
            config=config,
            context=context,
            stream_mode=list(LANGGRAPH_STREAM_MODES),
        )
        return translate_stream(items)

    def reset_agent(self) -> None:
        """丢掉缓存的图，下一次调用重新装配。不动 checkpointer，也不动任何 thread 的对话历史。"""
        self._cached = None

    def _resolve_user_id(self) -> str:
        return DEFAULT_USER_ID

    def _build_config(self, thread_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
        configurable = {key: overrides[key] for key in ASSEMBLY_INPUT_KEYS if key in overrides}
        configurable["thread_id"] = thread_id
        configurable["checkpoint_ns"] = overrides.get("checkpoint_ns", "")
        config: dict[str, Any] = {"configurable": configurable}
        # 身份也要进 config：工具/中间件读 `config["context"]`，图装配读的是**同一处**
        # （M9 §2.1）。少了这一行，装配侧只能落到 ContextVar → None（skills prefs 变成
        # `__default__` 桶），而运行期那一侧是 `default`——"图构建和中间件落进不同用户桶"
        # 正是这条线上最典型的 bug，而且单用户测试完全看不出来。
        config["context"] = {"thread_id": thread_id, "user_id": self._resolve_user_id()}
        if "recursion_limit" in overrides:
            config["recursion_limit"] = overrides["recursion_limit"]
        return config

    def _input_state(self, message: str) -> dict[str, Any]:
        return {
            "messages": [
                HumanMessage(content=f"--- BEGIN USER INPUT ---\n{message}\n--- END USER INPUT ---")
            ]
        }

    def _ensure_agent(
        self,
        config: dict[str, Any],
        *,
        app_config: AppConfig,
        checkpointer: Any,
        user_id: str,
    ) -> AgentAssembly:
        key = agent_cache_key(
            config["configurable"],
            app_config=app_config,
            checkpointer=checkpointer,
            user_id=user_id,
        )
        cached = self._cached
        if cached is not None and cached[0] == key:
            return cached[1]
        assembly = assemble_agent(config, app_config=app_config, checkpointer=checkpointer)
        self._cached = (key, assembly)
        return assembly
