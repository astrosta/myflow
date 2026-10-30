"""Capture stream abstraction and publisher.

The in-memory stream is the test backend. A Redis Streams backend can implement
the same small protocol later without changing the worker.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
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

    async def ack(self, stream_id: str, *, shard: int | None = None) -> None: ...


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

    async def ack(self, stream_id: str, *, shard: int | None = None) -> None:
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

    async def publish_turn_end(
        self,
        session: SessionRef,
        turn_id: str,
        *,
        signals: tuple[str, ...] = (),
        run_status: str | None = None,
        has_tool_error: bool = False,
        has_recovery: bool = False,
    ) -> CapturedMessage:
        captured = CapturedMessage(
            user_id=session.user_id,
            thread_id=session.thread_id,
            turn_id=turn_id,
            message_id=f"turn_end:{turn_id}",
            session_epoch=session.session_epoch,
            session_incarnation=session.session_incarnation,
            sequence=0,
            kind="control",
            role=None,
            message={},
            stream_shard=session.stream_shard,
            signals=tuple(signals),  # type: ignore[arg-type]
            run_status=run_status,  # type: ignore[arg-type]
            has_tool_error=has_tool_error,
            has_recovery=has_recovery,
            created_at=datetime.now(UTC).isoformat(),
        )
        result = await self._stream.publish(captured)
        if result.stream_id is None:
            raise RuntimeError("capture stream did not return a stream id")
        return replace(captured, stream_id=result.stream_id)

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
            created_at=datetime.now(UTC).isoformat(),
        )
        result = await self._stream.publish(captured)
        if result.stream_id is None:
            raise RuntimeError("capture stream did not return a stream id")
        return replace(captured, stream_id=result.stream_id)


def _fallback_id(message: BaseMessage) -> str:
    payload = f"{type(message).__name__}:{message.content}:{getattr(message, 'tool_call_id', '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RedisCaptureStream:
    """Redis Streams transport.

    The caller owns the Redis client and must serialize access to one consumer
    per shard. The stream keeps a PEL, so read is not destructive and a crash
    can redeliver the message.
    """

    def __init__(
        self,
        redis: Any,
        *,
        key_prefix: str,
        group: str,
        consumer: str,
        maxlen: int = 100_000,
        claim_idle_ms: int = 60_000,
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._group = group
        self._consumer = consumer
        self._maxlen = maxlen
        self._claim_idle_ms = max(1, int(claim_idle_ms))

    def key(self, shard: int) -> str:
        return f"{self._key_prefix}:{shard}"

    async def publish(self, captured: CapturedMessage) -> CaptureResult:
        shard = captured.stream_shard
        await self._ensure_group(shard)
        stream_id = await self._redis.xadd(
            self.key(shard),
            {"payload": json.dumps(asdict(captured), ensure_ascii=False, default=str)},
            maxlen=self._maxlen,
            approximate=True,
        )
        stream_id = stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
        return CaptureResult(status="published", stream_id=stream_id, stream_shard=shard)

    async def read(
        self,
        shard: int,
        *,
        block_ms: int = 5_000,
        count: int = 100,
    ) -> list[CapturedMessage]:
        await self._ensure_group(shard)
        result = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {self.key(shard): ">"},
            count=count,
            block=block_ms,
        )
        return _decode_entries(result)

    async def claim(
        self,
        shard: int,
        *,
        min_idle_ms: int | None = None,
        count: int = 100,
    ) -> list[CapturedMessage]:
        await self._ensure_group(shard)
        result = await self._redis.xautoclaim(
            self.key(shard),
            self._group,
            self._consumer,
            min_idle_time=self._claim_idle_ms if min_idle_ms is None else min_idle_ms,
            start_id="0-0",
            count=count,
        )
        # redis-py returns [next_id, messages, deleted_ids]
        entries = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
        return _decode_entries([[self.key(shard), entries]])

    async def ack(self, stream_id: str, *, shard: int | None = None) -> None:
        if shard is None:
            raise ValueError("RedisCaptureStream.ack requires shard")
        await self._redis.xack(self.key(shard), self._group, stream_id)

    async def pending_count(self, shard: int) -> int:
        result = await self._redis.xpending(self.key(shard), self._group)
        if isinstance(result, dict):
            return int(result.get("pending", 0))
        if isinstance(result, (list, tuple)) and result:
            return int(result[0])
        return 0

    async def _ensure_group(self, shard: int) -> None:
        try:
            await self._redis.xgroup_create(
                self.key(shard),
                self._group,
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise


def _decode_entries(result: Any) -> list[CapturedMessage]:
    messages: list[CapturedMessage] = []
    if not result:
        return messages
    for _stream_name, entries in result:
        for stream_id, fields in entries:
            payload = fields.get("payload", fields.get(b"payload"))
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            if not payload:
                continue
            data = json.loads(payload)
            data["stream_id"] = stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
            data["signals"] = tuple(data.get("signals") or ())
            messages.append(CapturedMessage(**data))
    return messages


__all__ = [
    "CaptureStream",
    "InMemoryCaptureStream",
    "MemoryMessagePublisher",
    "RedisCaptureStream",
]
