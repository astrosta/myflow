"""Single-worker append path for captured messages."""

from __future__ import annotations

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
    ) -> None:
        self._adapter = adapter
        self._stream = stream
        self._dedup = dedup
        self._sessions = sessions
        self._sync_store = sync_store
        self._lease_seconds = lease_seconds

    async def process(self, captured: CapturedMessage) -> str:
        sync = await self._sync_store.get(captured.user_id, captured.thread_id)
        if sync is None or sync.state == "deleted":
            await self._stream.ack(captured.stream_id)
            return "skipped"

        if (
            sync.session_epoch != captured.session_epoch
            or sync.session_incarnation != captured.session_incarnation
        ):
            await self._stream.ack(captured.stream_id)
            return "skipped"

        if captured.kind == "control":
            await self._stream.ack(captured.stream_id)
            return "skipped"

        message = _message_from_snapshot(captured.message)
        if message is None or not self._adapter.is_recordable(message):
            await self._stream.ack(captured.stream_id)
            return "skipped"

        payloads = self._adapter.to_payloads(message)
        if not payloads:
            await self._stream.ack(captured.stream_id)
            return "skipped"

        for payload in payloads:
            payload["turn_id"] = captured.turn_id
            payload["source_message_ids"] = [captured.message_id]

        state = await self._dedup.reserve(captured)
        if state == "appended":
            await self._stream.ack(captured.stream_id)
            return "skipped"
        if state == "appending":
            # A live lease means another attempt is in flight. Do not ack;
            # the PEL owner will retry after the lease expires.
            return "retry"
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
        await self._sync_store.set_state(captured.user_id, captured.thread_id, "appended")
        await self._stream.ack(captured.stream_id)
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


def _message_ids(response: dict[str, Any]) -> list[str]:
    messages = response.get("messages")
    if messages is None and isinstance(response.get("result"), dict):
        messages = response["result"].get("messages")
    if not isinstance(messages, list):
        return []
    return [str(item["id"]) for item in messages if isinstance(item, dict) and item.get("id")]


__all__ = ["TurnCaptureWorker"]
