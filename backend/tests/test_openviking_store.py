"""State and dedup repositories for the OpenViking worker."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.contracts import CapturedMessage
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.persistence.base import Base


def captured(message_id: str = "m1") -> CapturedMessage:
    return CapturedMessage(
        user_id="u1",
        thread_id="t1",
        turn_id="r1",
        message_id=message_id,
        session_epoch=1,
        session_incarnation="i1",
        sequence=0,
        kind="human",
        role="user",
        message={"type": "human", "content": "hi"},
    )


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables["memory_capture_dedup"],
        Base.metadata.tables["memory_sync_state"],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_dedup_lifecycle_and_uncertain_scope(factory) -> None:
    store = CaptureDedupStore(factory)
    item = captured()

    assert await store.reserve(item) == "pending"
    assert await store.mark_appending(item, lease_seconds=60) is True
    assert await store.state(item) == "appending"
    assert await store.mark_appending(item, lease_seconds=60) is False

    await store.mark_appended(item, ov_message_ids=["ov1"])

    assert await store.state(item) == "appended"
    assert await store.uncertain() == []


@pytest.mark.asyncio
async def test_uncertain_contains_pending_and_invalid_appended(factory) -> None:
    store = CaptureDedupStore(factory)
    pending = captured("pending")
    await store.reserve(pending)

    rows = await store.uncertain()
    assert [row.message_id for row in rows] == ["pending"]


@pytest.mark.asyncio
async def test_sync_store_get_or_create_and_delete(factory) -> None:
    store = MemorySyncStore(factory)

    row = await store.get_or_create(
        user_id="u1",
        thread_id="t1",
        ov_user="u_hash",
        ov_session_id="mf-s-1",
        session_incarnation="inc1",
        stream_shard=7,
    )
    assert row.session_epoch == 1
    await store.set_state("u1", "t1", "appended")
    assert (await store.get("u1", "t1")).state == "appended"
    await store.mark_deleted("u1", "t1")
    assert (await store.get("u1", "t1")).state == "deleted"
