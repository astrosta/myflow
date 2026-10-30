"""Single-worker append path and dedup behavior."""

from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.adapter import MemoryMessageAdapter
from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from agentkit.agents.memory.openviking.contracts import CapturedMessage
from agentkit.agents.memory.openviking.session import SessionManager
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.agents.memory.openviking.stream import InMemoryCaptureStream, MemoryMessagePublisher
from agentkit.agents.memory.openviking.worker import TurnCaptureWorker, WorkerSupervisor
from agentkit.persistence.base import Base


class FakeSessionClient:
    def __init__(self, config: OpenVikingMemoryConfig) -> None:
        self.config = config
        self.sessions: dict[str, dict] = {}
        self.appended: list[dict] = []

    def seed(self, session_id: str) -> None:
        self.sessions[session_id] = {
            "session_id": session_id,
            "memory_policy": self.config.session.memory_policy(),
            "auto_commit_policy": self.config.session_create_options()["auto_commit_policy"],
        }

    async def get_session(self, session_id: str, *, auto_create: bool = False) -> dict:
        if session_id not in self.sessions:
            from agentkit.agents.memory.openviking.session import SessionNotFound

            raise SessionNotFound(session_id)
        return self.sessions[session_id]

    async def create_session(self, session_id: str, *, options: dict) -> dict:
        self.sessions[session_id] = {
            "session_id": session_id,
            "memory_policy": options["memory_policy"],
            "auto_commit_policy": options["auto_commit_policy"],
        }
        return self.sessions[session_id]

    async def update_session_config(self, session_id: str, *, options: dict) -> dict:
        return self.sessions[session_id]

    async def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    async def batch_add_messages(self, session_id: str, messages: list[dict]) -> dict:
        self.appended.extend(messages)
        return {"messages": [{"id": "ov-1"}]}


@pytest_asyncio.fixture
async def stack():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables["memory_sync_state"],
        Base.metadata.tables["memory_capture_dedup"],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    config = OpenVikingMemoryConfig(user_id_salt="s" * 32)
    client = FakeSessionClient(config)
    sync_store = MemorySyncStore(factory)
    dedup = CaptureDedupStore(factory)
    sessions = SessionManager(sync_store, client, config)
    stream = InMemoryCaptureStream()
    publisher = MemoryMessagePublisher(stream)
    worker = TurnCaptureWorker(MemoryMessageAdapter(), stream, dedup, sessions, sync_store)
    yield client, sync_store, dedup, sessions, stream, publisher, worker
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_appends_and_marks_appended(stack) -> None:
    client, _sync_store, dedup, sessions, stream, publisher, worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    await sessions.ensure_session(ref)

    captured = await publisher.publish_human(ref, "r1", HumanMessage(content="我喜欢 pytest"), sequence=0)
    assert await worker.process(captured) == "appended"

    assert client.appended[0]["message_kind"] == "user_query"
    assert await dedup.state(captured) == "appended"
    assert captured.stream_id in stream.acked


@pytest.mark.asyncio
async def test_duplicate_delivery_is_skipped(stack) -> None:
    client, _sync_store, _dedup, sessions, _stream, publisher, worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    captured = await publisher.publish_human(ref, "r1", HumanMessage(content="hi"), sequence=0)

    await worker.process(captured)
    await worker.process(captured)

    assert len(client.appended) == 1


@pytest.mark.asyncio
async def test_control_event_is_acked_without_append(stack) -> None:
    client, _sync_store, _dedup, sessions, stream, _publisher, worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    captured = CapturedMessage(
        user_id="u1",
        thread_id="t1",
        turn_id="r1",
        message_id="control-1",
        session_epoch=ref.session_epoch,
        session_incarnation=ref.session_incarnation,
        sequence=0,
        kind="control",
        role=None,
        message={},
        stream_id="1-0",
        stream_shard=ref.stream_shard,
    )

    assert await worker.process(captured) == "skipped"
    assert client.appended == []
    assert "1-0" in stream.acked


@pytest.mark.asyncio
async def test_epoch_mismatch_is_acked_without_append(stack) -> None:
    client, sync_store, _dedup, sessions, stream, publisher, worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    captured = await publisher.publish_human(ref, "r1", HumanMessage(content="hi"), sequence=0)
    await sync_store.bump_epoch("u1", "t1")

    assert await worker.process(captured) == "skipped"
    assert client.appended == []
    assert captured.stream_id in stream.acked


@pytest.mark.asyncio
async def test_stale_appending_row_is_retried(stack) -> None:
    client, _sync_store, dedup, sessions, _stream, publisher, worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    captured = await publisher.publish_human(ref, "r1", HumanMessage(content="hi"), sequence=0)
    await dedup.reserve(captured)
    await dedup.mark_appending(captured, lease_seconds=0)

    assert await worker.process(captured) == "appended"
    assert len(client.appended) == 1


class ReplayStream:
    """Reads the same entry twice, like a PEL replay after a failed attempt."""

    def __init__(self, captured: CapturedMessage) -> None:
        self.captured = captured
        self.acks: list[str] = []

    async def read(self, shard: int, *, block_ms: int = 0) -> list[CapturedMessage]:
        return [self.captured]

    async def claim(self, shard: int) -> list[CapturedMessage]:
        return [self.captured]

    async def ack(self, stream_id: str, *, shard: int | None = None) -> None:
        self.acks.append(stream_id)


class RetryThenAppendWorker:
    def __init__(self) -> None:
        self.calls = 0

    async def process(self, captured: CapturedMessage) -> str:
        self.calls += 1
        return "retry" if self.calls == 1 else "appended"


@pytest.mark.asyncio
async def test_buffered_messages_append_only_after_turn_end(stack) -> None:
    client, _sync_store, _dedup, sessions, stream, publisher, worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    human = await publisher.publish_human(ref, "r1", HumanMessage(content="我住杭州"), sequence=0)

    assert await worker.handle(human) == "buffered"
    assert client.appended == []
    assert human.stream_id not in stream.acked

    control = await publisher.publish_turn_end(ref, "r1", run_status="interrupted")
    assert await worker.handle(control) == "flushed"
    assert len(client.appended) == 1
    assert human.stream_id in stream.acked
    assert control.stream_id in stream.acked
    assert control.run_status == "interrupted"


@pytest.mark.asyncio
async def test_buffered_messages_without_turn_end_are_not_appended(stack) -> None:
    client, _sync_store, _dedup, sessions, stream, publisher, worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    human = await publisher.publish_human(ref, "r1", HumanMessage(content="我住杭州"), sequence=0)

    assert await worker.handle(human) == "buffered"
    assert client.appended == []
    assert human.stream_id not in stream.acked


@pytest.mark.asyncio
async def test_missing_turn_end_timeout_appends_without_committing(stack) -> None:
    client, sync_store, dedup, sessions, stream, publisher, _worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    client.seed(ref.ov_session_id)
    worker = TurnCaptureWorker(
        MemoryMessageAdapter(),
        stream,
        dedup,
        sessions,
        sync_store,
        missing_turn_end_timeout_seconds=1,
    )
    captured = await publisher.publish_human(
        ref,
        "r1",
        HumanMessage(content="我住杭州"),
        sequence=0,
    )
    captured = replace(captured, created_at="2000-01-01T00:00:00+00:00")

    assert await worker.handle(captured) == "buffered"
    assert await worker.sweep_expired() == ["flushed"]
    assert len(client.appended) == 1
    assert captured.stream_id in stream.acked


@pytest.mark.asyncio
async def test_supervisor_does_not_remember_a_retry_forever(stack) -> None:
    _client, _sync_store, _dedup, sessions, _stream, publisher, _worker = stack
    ref = await sessions.resolve_capture_context("u1", "t1")
    captured = await publisher.publish_human(ref, "r1", HumanMessage(content="hi"), sequence=0)
    stream = ReplayStream(captured)
    worker = RetryThenAppendWorker()
    supervisor = WorkerSupervisor(stream, worker, shards=1, poll_seconds=0.01)

    await supervisor.run_once(0)
    await supervisor.run_once(0)

    assert worker.calls >= 2
