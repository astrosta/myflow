"""Capture middleware for the OpenViking adapter.

Capture is deliberately best-effort: Redis or the OpenViking runtime may be
unavailable, and a memory outage must never fail the user's turn. Every side
effect is guarded and logged; the gate decision is still stored so AI/tool
hooks can follow the same turn policy.

User profile rendering is intentionally not here: it happens while the lead
agent graph is assembled and goes into the system prompt's <user_profile> tag.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.openviking.contracts import TurnDecision
from agentkit.runtime.run_finalizers import (
    register_run_finalizer,
    unregister_run_finalizer,
)

logger = logging.getLogger(__name__)

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
        self._seen: set[tuple[str, int | str]] = set()
        self._ended_turns: set[str] = set()
        self._registered_turns: set[str] = set()
        self._finalizer_callbacks: dict[str, Any] = {}

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        context = _context(runtime)
        if context.get("subagent_name"):
            return None

        user_id = _text(context.get("user_id")) or self._user_id
        thread_id = _text(context.get("thread_id"))
        if not user_id or not thread_id:
            return None

        message = _latest_user(state.get("messages") or [])
        if message is None:
            return None
        decision = self._facade.decide_capture(
            _text(message.content) or "",
            has_attachments=bool(state.get("uploaded_files")),
        )
        _put(context, "capture_decision", decision)
        if decision.disposition != "capture":
            return None
        self._register_finalizer(context, decision)

        try:
            ref = await self._facade.resolve_capture_context(user_id, thread_id)
            _put(context, "capture_ref", ref)
            await self._facade.publish_human(ref, _turn_id(context), message)
        except Exception:
            logger.warning("OpenViking human capture failed; continuing the turn", exc_info=True)
        return None

    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> None:
        context = _context(runtime)
        if context.get("subagent_name"):
            return
        decision = context.get("capture_decision")
        if not isinstance(decision, TurnDecision) or decision.disposition != "capture":
            return
        message = _latest_ai(state.get("messages") or [])
        if message is None or not self._mark_seen(context, message):
            return
        try:
            ref = await self._capture_ref(context)
            await self._facade.publish_ai(ref, _turn_id(context), message)
        except Exception:
            logger.warning("OpenViking AI capture failed; continuing the turn", exc_info=True)

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
        if not isinstance(decision, TurnDecision) or decision.disposition != "capture":
            return result
        if not self._mark_seen(context, result):
            return result
        try:
            ref = await self._capture_ref(context)
            await self._facade.publish_tool(ref, _turn_id(context), result)
        except Exception:
            logger.warning("OpenViking tool capture failed; continuing the turn", exc_info=True)
        return result

    async def aafter_agent(self, state: dict[str, Any], runtime: Any) -> None:
        context = _context(runtime)
        if context.get("subagent_name"):
            return
        decision = context.get("capture_decision")
        if not isinstance(decision, TurnDecision):
            return
        turn_id = _turn_id(context)
        await self._finish_turn(context, turn_id, decision, "success")

    def _register_finalizer(
        self, context: MutableMapping[str, Any], decision: TurnDecision
    ) -> None:
        turn_id = _turn_id(context)
        key = f"{context.get('thread_id') or ''}:{turn_id}"
        if key in self._registered_turns:
            return
        self._registered_turns.add(key)

        async def finalize(status: str) -> None:
            await self._finish_turn(context, turn_id, decision, status)

        self._finalizer_callbacks[turn_id] = finalize
        register_run_finalizer(turn_id, finalize)

    async def _finish_turn(
        self,
        context: MutableMapping[str, Any],
        turn_id: str,
        decision: TurnDecision,
        run_status: str,
    ) -> None:
        key = f"{context.get('thread_id') or ''}:{turn_id}"
        if key in self._ended_turns:
            return
        if decision.disposition != "capture":
            return
        try:
            ref = await self._capture_ref(context)
            await self._facade.publish_turn_end(
                ref,
                turn_id,
                decision,
                run_status=run_status,
            )
        except Exception:
            logger.warning("OpenViking turn-end capture failed; continuing", exc_info=True)
            return
        self._ended_turns.add(key)
        callback = self._finalizer_callbacks.pop(turn_id, None)
        if callback is not None:
            unregister_run_finalizer(turn_id, callback)
        if len(self._ended_turns) > 5_000:
            self._ended_turns.clear()

    async def _capture_ref(self, context: MutableMapping[str, Any]) -> Any:
        ref = context.get("capture_ref")
        if ref is not None:
            return ref
        user_id = _text(context.get("user_id")) or self._user_id or ""
        thread_id = _text(context.get("thread_id")) or ""
        ref = await self._facade.resolve_capture_context(user_id, thread_id)
        _put(context, "capture_ref", ref)
        return ref

    def _mark_seen(self, context: MutableMapping[str, Any], message: Any) -> bool:
        turn_id = _turn_id(context)
        key: tuple[str, int | str] = (
            turn_id,
            getattr(message, "id", None) or id(message),
        )
        if key in self._seen:
            return False
        self._seen.add(key)
        if len(self._seen) > 5_000:
            self._seen.clear()
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


__all__ = [
    "OpenVikingMemoryMiddleware",
]
