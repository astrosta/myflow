"""Capture stream abstraction and publisher.

The in-memory stream is the test backend. A Redis Streams backend can implement
the same small protocol later without changing the worker.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Protocol

from langchain_core.messages import BaseMessage

from agentkit.agents.memory.openviking.contracts import (
    CaptureResult,
    CapturedMessage,
    SessionRef,
)


class CaptureStream(Protocol):
    async def publish(self, captured: CapturedMessage) -> CaptureResult: ...

    async def claim(self, *, limit: int = 100) -> list[CapturedMessage]: ...

    async def ack(self, stream_id: str) -> None: ...


class InMemoryCaptureStream:
    def __init__(self) -> None:
        self._pending: dict[str, CapturedMessage] = {}
        self._next = 0
        self.acked: list[str] = []

    async def publish(self, captured: CapturedMessage) -> CaptureResult:
        self._next += 1
        stream_id = captured.stream_id or f"{self._next}-0"
        item = replace(captured, stream_id=stream_id)
        self._pending[stream_id] = item
        return CaptureResult(
            status="published",
            stream_id=stream_id,
            stream_shard=captured.stream_shard,
        )

    async def claim(self, *, limit: int = 100) -> list[CapturedMessage]:
        return list(self._pending.values())[:limit]

    async def ack(self, stream_id: str) -> None:
        self.acked.append(stream_id)
        self._pending.pop(stream_id, None)

    @property
    def pending(self) -> list[CapturedMessage]:
        return list(self._pending.values())


class MemoryMessagePublisher:
    def __init__(self, stream: CaptureStream) -> None:
        self._stream = stream

    async def publish_human(
        self,
        session: SessionRef,
        turn_id: str,
        message: BaseMessage,
        *,
        sequence: int = 0,
    ) -> CapturedMessage:
        return await self._publish(session, turn_id, "human", "user", message, sequence)

    async def publish_ai(
        self,
        session: SessionRef,
        turn_id: str,
        message: BaseMessage,
        *,
        sequence: int = 0,
    ) -> CapturedMessage:
        return await self._publish(session, turn_id, "ai", "assistant", message, sequence)

    async def publish_tool(
        self,
        session: SessionRef,
        turn_id: str,
        message: BaseMessage,
        *,
        sequence: int = 0,
    ) -> CapturedMessage:
        return await self._publish(session, turn_id, "tool", "assistant", message, sequence)

    async def _publish(
        self,
        session: SessionRef,
        turn_id: str,
        kind: str,
        role: str,
        message: BaseMessage,
        sequence: int,
    ) -> CapturedMessage:
        snapshot = message.model_dump()
        message_id = str(getattr(message, "id", None) or _fallback_id(message))
        captured = CapturedMessage(
            user_id=session.user_id,
            thread_id=session.thread_id,
            turn_id=turn_id,
            message_id=message_id,
            session_epoch=session.session_epoch,
            session_incarnation=session.session_incarnation,
            sequence=sequence,
            kind=kind,  # type: ignore[arg-type]
            role=role,  # type: ignore[arg-type]
            message=snapshot,
            stream_shard=session.stream_shard,
        )
        result = await self._stream.publish(captured)
        if result.stream_id is None:
            raise RuntimeError("capture stream did not return a stream id")
        return replace(captured, stream_id=result.stream_id)


def _fallback_id(message: BaseMessage) -> str:
    payload = f"{type(message).__name__}:{message.content}:{getattr(message, 'tool_call_id', '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["CaptureStream", "InMemoryCaptureStream", "MemoryMessagePublisher"]
