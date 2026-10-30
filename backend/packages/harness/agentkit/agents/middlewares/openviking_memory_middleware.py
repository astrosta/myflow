"""Capture middleware for the additive OpenViking adapter.

This middleware is intentionally not wired into the default chain yet. It is
small enough to test independently and can be enabled once the runtime
configuration and worker lifecycle are cut over.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.openviking.contracts import TurnDecision


class OpenVikingMemoryMiddleware(AgentMiddleware):
    def __init__(
        self,
        facade: Any,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        super().__init__()
        self._facade = facade
        self._user_id = user_id
        self._agent_name = agent_name
        self._seen: set[int | str] = set()

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> None:
        context = _context(runtime)
        if context.get("subagent_name"):
            return
        user_id = context.get("user_id") or self._user_id
        thread_id = context.get("thread_id")
        if not user_id or not thread_id:
            return

        message = _latest_user(state.get("messages") or [])
        if message is None:
            return
        text = _text(message.content)
        decision = self._facade.decide_capture(text, has_attachments=bool(state.get("uploaded_files")))
        _put(context, "capture_decision", decision)
        if decision.disposition != "capture":
            return

        ref = await self._facade.resolve_capture_context(user_id, thread_id)
        await self._facade.publish_human(ref, _turn_id(context), message)

    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> None:
        context = _context(runtime)
        if context.get("subagent_name"):
            return
        decision = context.get("capture_decision")
        if not isinstance(decision, TurnDecision) or decision.disposition != "capture":
            return
        message = _latest_ai(state.get("messages") or [])
        if message is None or not self._mark_seen(message):
            return
        ref = await self._facade.resolve_capture_context(
            context.get("user_id") or self._user_id or "",
            context.get("thread_id") or "",
        )
        await self._facade.publish_ai(ref, _turn_id(context), message)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result
        context = _context(getattr(request, "runtime", None))
        if context.get("subagent_name"):
            return result
        decision = context.get("capture_decision")
        if isinstance(decision, TurnDecision) and decision.disposition != "capture":
            return result
        if not self._mark_seen(result):
            return result
        ref = await self._facade.resolve_capture_context(
            context.get("user_id") or self._user_id or "",
            context.get("thread_id") or "",
        )
        await self._facade.publish_tool(ref, _turn_id(context), result)
        return result

    def _mark_seen(self, message: Any) -> bool:
        key: int | str = getattr(message, "id", None) or id(message)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


def _latest_user(messages: list[Any]) -> HumanMessage | None:
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        kwargs = getattr(message, "additional_kwargs", None) or {}
        if kwargs.get("hide_from_ui") or kwargs.get("memory_profile"):
            continue
        if getattr(message, "name", None) == "summary":
            continue
        return message
    return None


def _latest_ai(messages: list[Any]) -> AIMessage | None:
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        kwargs = getattr(message, "additional_kwargs", None) or {}
        if kwargs.get("hide_from_ui"):
            continue
        if message.tool_calls or _text(message.content).strip():
            return message
    return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "text_block"}:
                if isinstance(block.get("text"), str):
                    chunks.append(block["text"])
        return "\n".join(chunks)
    return "" if content is None else str(content)


def _context(runtime: Any) -> MutableMapping[str, Any]:
    context = getattr(runtime, "context", None) if runtime is not None else None
    if isinstance(context, MutableMapping):
        return context
    return {}


def _put(context: MutableMapping[str, Any], key: str, value: Any) -> None:
    try:
        context[key] = value
    except TypeError:
        pass


def _turn_id(context: MutableMapping[str, Any]) -> str:
    value = context.get("run_id")
    return str(value) if value else "run"


__all__ = ["OpenVikingMemoryMiddleware"]
