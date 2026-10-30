"""Single-worker append path for captured messages."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.openviking.adapter import MemoryMessageAdapter
from agentkit.agents.memory.openviking.contracts import CapturedMessage, SessionRef
from agentkit.agents.memory.openviking.session import SessionManager
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.agents.memory.openviking.stream import CaptureStream


class TurnCaptureWorker:
    def __init__(
        self,
        adapter: MemoryMessageAdapter,
        stream: CaptureStream,
        dedup: CaptureDedupStore,
        sessions: SessionManager,
        sync_store: MemorySyncStore,
        *,
        lease_seconds: int = 60,
        missing_turn_end_timeout_seconds: int = 86_400,
    ) -> None:
        self._adapter = adapter
        self._stream = stream
        self._dedup = dedup
        self._sessions = sessions
        self._sync_store = sync_store
        self._lease_seconds = lease_seconds
        self._missing_turn_end_timeout = max(0, int(missing_turn_end_timeout_seconds))
        self._buffers: dict[tuple[str, str, int, str, str], list[CapturedMessage]] = {}

    async def handle(self, captured: CapturedMessage) -> str:
        """Turn-aware entry point used by the shard supervisor.

        Messages wait in a per-turn buffer while their Redis entries stay in
        the PEL. The turn-end control event flushes the buffer in stream order;
        a crash before that redelivers the un-acked entries after restart.
        Direct `process` remains the message-level primitive for tests and
        one-off appends.
        """

        if captured.kind != "control":
            key = self._buffer_key(captured)
            buffer = self._buffers.setdefault(key, [])
            if not any(item.message_id == captured.message_id for item in buffer):
                buffer.append(captured)
            return "buffered"

        key = self._buffer_key(captured)
        if captured.run_status == "interrupted":
            # A cancelled turn is not part of the committed conversation
            # record: acknowledge and drop its buffered messages without
            # touching OpenViking or the sync state.
            pending = self._buffers.pop(key, [])
            for item in pending:
                await self._stream.ack(item.stream_id, shard=item.stream_shard)
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "discarded"

        result = await self._flush(key)
        if result == "retry":
            return "retry"
        await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
        return "flushed"

    async def sweep_expired(self) -> list[str]:
        """Append turns whose turn-end never arrived within the configured bound.

        This is a last-resort append only; it never commits. A normal turn is
        still flushed by its turn-end control event.
        """

        results: list[str] = []
        if self._missing_turn_end_timeout <= 0:
            return results
        for key, pending in list(self._buffers.items()):
            if not pending or not _expired(
                pending[0].created_at, self._missing_turn_end_timeout
            ):
                continue
            result = await self._flush(key)
            results.append(result)
        return results

    async def _flush(self, key: tuple[str, str, int, str, str]) -> str:
        pending = self._buffers.get(key, [])
        while pending:
            result = await self.process(pending[0])
            if result == "retry":
                self._buffers[key] = pending
                return "retry"
            pending.pop(0)
        self._buffers.pop(key, None)
        return "flushed"

    @staticmethod
    def _buffer_key(captured: CapturedMessage) -> tuple[str, str, int, str, str]:
        return (
            captured.user_id,
            captured.thread_id,
            captured.session_epoch,
            captured.session_incarnation,
            captured.turn_id,
        )

    async def process(self, captured: CapturedMessage) -> str:
        sync = await self._sync_store.get(captured.user_id, captured.thread_id)
        if sync is None or sync.state == "deleted":
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "skipped"

        if (
            sync.session_epoch != captured.session_epoch
            or sync.session_incarnation != captured.session_incarnation
        ):
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "skipped"

        if captured.kind == "control":
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "skipped"

        message = _message_from_snapshot(captured.message)
        if message is None or not self._adapter.is_recordable(message):
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "skipped"

        payloads = self._adapter.to_payloads(message)
        if not payloads:
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "skipped"

        for payload in payloads:
            payload["turn_id"] = captured.turn_id
            payload["source_message_ids"] = [captured.message_id]

        state = await self._dedup.reserve(captured)
        if state == "appended":
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "skipped"
        # pending and expired appending both reach this point. mark_appending
        # refuses only a still-live lease, which means another attempt is in
        # flight and the PEL owner will retry after it expires.
        if not await self._dedup.mark_appending(captured, lease_seconds=self._lease_seconds):
            return "retry"

        ref = SessionRef(
            user_id=captured.user_id,
            thread_id=captured.thread_id,
            ov_user=sync.ov_user,
            ov_session_id=sync.ov_session_id,
            stream_shard=sync.stream_shard,
            session_epoch=sync.session_epoch,
            session_incarnation=sync.session_incarnation,
        )
        try:
            await self._sessions.ensure_session(ref)
            response = await self._sessions.append_messages(ref, payloads)
        except Exception:
            # Leave the row appending. A restart reconciles it by source ids;
            # the message is not acked, so Redis can redeliver it.
            return "retry"

        await self._dedup.mark_appended(captured, ov_message_ids=_message_ids(response))
        await self._sync_store.record_append(
            captured.user_id,
            captured.thread_id,
            message_id=captured.message_id,
            stream_id=captured.stream_id,
        )
        await self._sync_store.set_state(captured.user_id, captured.thread_id, "appended")
        await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
        return "appended"


def _message_from_snapshot(snapshot: dict[str, Any]) -> HumanMessage | AIMessage | ToolMessage | None:
    message_type = snapshot.get("type")
    data = {key: value for key, value in snapshot.items() if key != "type"}
    try:
        if message_type == "human":
            return HumanMessage(**data)
        if message_type == "ai":
            return AIMessage(**data)
        if message_type == "tool":
            return ToolMessage(**data)
    except (TypeError, ValueError):
        return None
    return None


def _expired(created_at: str, timeout_seconds: int) -> bool:
    if not created_at:
        return False
    try:
        value = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - value).total_seconds() >= timeout_seconds


def _message_ids(response: dict[str, Any]) -> list[str]:
    messages = response.get("messages")
    if messages is None and isinstance(response.get("result"), dict):
        messages = response["result"].get("messages")
    if not isinstance(messages, list):
        return []
    return [str(item["id"]) for item in messages if isinstance(item, dict) and item.get("id")]


class WorkerSupervisor:
    """Run one consumer loop per shard under a process-wide Redis lease."""

    def __init__(
        self,
        stream: Any,
        worker: TurnCaptureWorker,
        *,
        shards: int,
        redis: Any = None,
        singleton_key: str = "myflow:memory:worker:singleton",
        owner: str = "memory-worker",
        lease_seconds: int = 60,
        poll_seconds: float = 0.05,
    ) -> None:
        self._stream = stream
        self._worker = worker
        self._shards = max(1, shards)
        self._redis = redis
        self._singleton_key = singleton_key
        self._owner = owner
        self._lease_seconds = max(5, lease_seconds)
        self._poll_seconds = max(0.01, poll_seconds)
        self._tasks: list[Any] = []
        self._heartbeat: Any = None
        self._stopping = False

    async def start(self) -> None:
        if self._redis is not None:
            acquired = await self._redis.set(
                self._singleton_key,
                self._owner,
                nx=True,
                ex=self._lease_seconds,
            )
            if not acquired:
                raise RuntimeError("another memory worker already owns the singleton lease")
            self._heartbeat = asyncio.create_task(self._renew_lease())
        self._tasks = [
            asyncio.create_task(self._run_shard(shard))
            for shard in range(self._shards)
        ]

    async def stop(self) -> None:
        self._stopping = True
        tasks = [*self._tasks, *([self._heartbeat] if self._heartbeat else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._heartbeat = None
        if self._redis is not None:
            current = await self._redis.get(self._singleton_key)
            if current == self._owner or (
                isinstance(current, bytes) and current.decode() == self._owner
            ):
                await self._redis.delete(self._singleton_key)

    async def run_once(self, shard: int) -> list[str]:
        """Process one read/claim round for a shard.

        Redis may return the same entry through both `XREADGROUP` and
        `XAUTOCLAIM`; the persistent dedup lease, not an in-memory set, makes
        that safe. A retry must remain replayable, so a stream id is never
        remembered across rounds.
        """

        results: list[str] = []
        sweep = getattr(self._worker, "sweep_expired", None)
        if callable(sweep):
            results.extend(await sweep())
        handle = getattr(self._worker, "handle", None)
        for message in await self._read_messages(shard):
            if callable(handle):
                results.append(await handle(message))
            else:
                results.append(await self._worker.process(message))
        return results

    async def _run_shard(self, shard: int) -> None:
        while not self._stopping:
            try:
                await self.run_once(shard)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(min(1.0, self._poll_seconds * 10))
            else:
                await asyncio.sleep(self._poll_seconds)

    async def _read_messages(self, shard: int) -> list[CapturedMessage]:
        messages: list[CapturedMessage] = []
        read = getattr(self._stream, "read", None)
        if callable(read):
            messages.extend(await read(shard, block_ms=1_000))
        claim = getattr(self._stream, "claim", None)
        if callable(claim):
            messages.extend(await claim(shard))
        return messages

    async def _renew_lease(self) -> None:
        # Never extend a lease now owned by someone else. A plain EXPIRE after
        # a pause could keep a stale worker's lock alive and starve the real
        # owner.
        script = (
            "if redis.call('GET', KEYS[1]) == ARGV[1] "
            "then return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end"
        )
        while not self._stopping:
            await asyncio.sleep(max(1.0, self._lease_seconds / 2))
            try:
                eval_ = getattr(self._redis, "eval", None)
                if eval_ is not None:
                    renewed = await eval_(
                        script,
                        1,
                        self._singleton_key,
                        self._owner,
                        self._lease_seconds,
                    )
                else:  # pragma: no cover - compatibility for minimal fakes
                    current = await self._redis.get(self._singleton_key)
                    if isinstance(current, bytes):
                        current = current.decode()
                    renewed = current == self._owner
                    if renewed:
                        await self._redis.expire(self._singleton_key, self._lease_seconds)
                if not renewed:
                    self._stopping = True
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)


__all__ = ["TurnCaptureWorker", "WorkerSupervisor"]
