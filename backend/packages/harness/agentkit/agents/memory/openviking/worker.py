"""Single-worker append path for captured messages."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.openviking.adapter import MemoryMessageAdapter
from agentkit.agents.memory.openviking.config import SessionSkillsConfig
from agentkit.agents.memory.openviking.contracts import (
    CapturedMessage,
    SessionRef,
    SignalContext,
    SignalKind,
)
from agentkit.agents.memory.openviking.session import SessionManager
from agentkit.agents.memory.openviking.signals import (
    CommitSignalDetector,
    commit_reason,
    needs_commit,
)
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.agents.memory.openviking.stream import CaptureStream
from agentkit.skills.session_import import SessionSkillImporter

logger = logging.getLogger(__name__)


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
        detector: CommitSignalDetector | None = None,
        session_skills: SessionSkillsConfig | None = None,
        session_skill_importer: SessionSkillImporter | None = None,
    ) -> None:
        self._adapter = adapter
        self._stream = stream
        self._dedup = dedup
        self._sessions = sessions
        self._sync_store = sync_store
        self._lease_seconds = lease_seconds
        self._missing_turn_end_timeout = max(0, int(missing_turn_end_timeout_seconds))
        # The worker is the only place allowed to use the model-backed fallback:
        # detection runs after the turn is appended, off the reply path.
        self._detector = detector or CommitSignalDetector()
        self._session_skills = session_skills or SessionSkillsConfig()
        self._session_skill_importer = session_skill_importer
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

        pending = list(self._buffers.get(key, ()))
        result = await self._flush(key)
        if result == "retry":
            return "retry"

        if not captured.signals and pending:
            # The detector runs in the worker, after the turn has been
            # appended. Existing signals (notably EXPLICIT_REMEMBER from the
            # built-in tool) win; the detector never overrides them.
            detected = await self._detect_signals(captured, pending)
            if detected:
                captured = replace(captured, signals=detected)

        if needs_commit(captured.signals):
            commit = await self._commit_if_needed(captured)
            if commit == "retry":
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

    async def _detect_signals(
        self,
        control: CapturedMessage,
        pending: list[CapturedMessage],
    ) -> tuple[SignalKind, ...]:
        try:
            return await self._detector.detect(
                SignalContext(
                    user_text=_latest_text(pending, "human"),
                    assistant_text=_latest_text(pending, "ai"),
                    signals=control.signals,
                    has_tool_error=control.has_tool_error,
                    has_recovery=control.has_recovery,
                    run_status=control.run_status,
                )
            )
        except Exception:
            logger.warning("OpenViking commit-signal detection failed; continuing", exc_info=True)
            return ()

    async def _commit_if_needed(self, captured: CapturedMessage) -> str:
        reason = commit_reason(captured.signals)
        if reason is None:
            return "skipped"

        sync = await self._sync_store.get(captured.user_id, captured.thread_id)
        if sync is None or sync.state == "deleted":
            return "skipped"
        if (
            sync.session_epoch != captured.session_epoch
            or sync.session_incarnation != captured.session_incarnation
        ):
            return "skipped"
        # A SESSION_END turn is a SKIP turn: it has no messages of its own and
        # must not create an empty OpenViking session just to commit it.
        if sync.last_message_at is None:
            return "skipped"

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
            response = await self._sessions.commit(ref, reason=reason)
        except Exception:
            # The control event stays in the PEL, so the next claim retries
            # the commit after the current turn has already been appended.
            logger.warning(
                "OpenViking commit failed for turn %s (%s); will retry",
                captured.turn_id,
                reason,
                exc_info=True,
            )
            return "retry"
        # The commit is already durable. Session-skill import is best-effort and
        # must never turn a successful commit into a retry (which would append
        # and commit the same turn twice).
        await self._after_commit_task(captured, ref, response)
        return "committed"

    async def _after_commit_task(
        self,
        captured: CapturedMessage,
        ref: SessionRef,
        response: dict[str, Any],
    ) -> None:
        importer = self._session_skill_importer
        if importer is None or not self._session_skills.enabled:
            return
        task_id = response.get("task_id") if isinstance(response, dict) else None
        if not isinstance(task_id, str) or not task_id:
            return
        task = await self._wait_for_commit_task(ref, task_id)
        if task is None:
            return
        status = str(task.get("status") or "").casefold()
        if status not in {"completed", "succeeded", "success", "done"}:
            logger.warning(
                "OpenViking commit task %s for turn %s ended with status %s: %s",
                task_id,
                captured.turn_id,
                status or "unknown",
                str(task.get("error") or "")[:500],
            )
            return
        result = task.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                result = {}
        if not isinstance(result, dict):
            result = {}
        raw_uris = result.get("session_skill_uris") or task.get("session_skill_uris") or []
        if not isinstance(raw_uris, list) or not raw_uris:
            return
        try:
            imported = await importer.import_skills(
                owner_user_id=captured.user_id,
                ov_user=ref.ov_user,
                task_id=task_id,
                skill_uris=raw_uris,
            )
        except Exception:
            logger.warning(
                "OpenViking session-skill import failed for turn %s task %s",
                captured.turn_id,
                task_id,
                exc_info=True,
            )
            return
        logger.info(
            "Imported %s OpenViking session skill(s) for %s/%s from task %s",
            len(imported),
            captured.user_id,
            captured.thread_id,
            task_id,
        )

    async def _wait_for_commit_task(
        self, ref: SessionRef, task_id: str
    ) -> dict[str, Any] | None:
        get_task = getattr(self._sessions, "get_task", None)
        if not callable(get_task):
            logger.warning(
                "SessionManager does not expose get_task; skipping session-skill import"
            )
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._session_skills.task_timeout_seconds
        final = {
            "completed",
            "succeeded",
            "success",
            "done",
            "failed",
            "cancelled",
            "canceled",
            "error",
        }
        last_error: Exception | None = None
        while True:
            try:
                task = await get_task(ref, task_id)
            except Exception as exc:
                last_error = exc
            else:
                if isinstance(task, dict):
                    status = str(task.get("status") or "").casefold()
                    if status in final:
                        return task
            if loop.time() >= deadline:
                logger.warning(
                    "OpenViking commit task %s did not finish within %.1fs; "
                    "skipping session-skill import for this commit (last error: %s)",
                    task_id,
                    self._session_skills.task_timeout_seconds,
                    type(last_error).__name__ if last_error is not None else "none",
                )
                return None
            await asyncio.sleep(self._session_skills.poll_interval_seconds)

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
            commit = await self._commit_if_needed(captured)
            if commit == "retry":
                return "retry"
            await self._stream.ack(captured.stream_id, shard=captured.stream_shard)
            return "committed" if commit == "committed" else "skipped"

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
        except Exception as exc:
            # Leave the row appending. A restart reconciles it by source ids;
            # the message is not acked, so Redis can redeliver it. The log is
            # deliberately bounded: session creation / policy failures used to
            # vanish here and look like "memory silently does nothing".
            logger.warning(
                "OpenViking append failed for %s/%s turn %s session %s: %s: %s",
                captured.user_id,
                captured.thread_id,
                captured.turn_id,
                ref.ov_session_id,
                type(exc).__name__,
                str(exc)[:500],
                exc_info=True,
            )
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


def _latest_text(pending: list[CapturedMessage], kind: str) -> str:
    for item in reversed(pending):
        if item.kind != kind:
            continue
        text = _snapshot_text(item.message)
        if text:
            return text
    return ""


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    content = snapshot.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "\n".join(chunks)
    return "" if content is None else str(content)


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
