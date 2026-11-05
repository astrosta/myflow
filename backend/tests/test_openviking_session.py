"""Explicit OpenViking session creation and policy validation."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.config import (
    DEFAULT_SESSION_MEMORY_TYPES,
    OpenVikingMemoryConfig,
)
from agentkit.agents.memory.openviking.session import (
    SessionAlreadyExists,
    SessionManager,
    SessionNotFound,
    SessionPolicyMismatch,
    SessionTombstoned,
)
from agentkit.agents.memory.openviking.store import MemorySyncStore
from agentkit.persistence.base import Base


class FakeClient:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple] = []

    async def get_session(self, session_id: str, *, auto_create: bool = False) -> dict:
        self.calls.append(("get", session_id, auto_create))
        if session_id not in self.sessions:
            raise SessionNotFound(session_id)
        return self.sessions[session_id]

    async def create_session(self, session_id: str, *, options: dict) -> dict:
        self.calls.append(("create", session_id, options))
        if session_id in self.sessions:
            raise SessionAlreadyExists(session_id)
        self.sessions[session_id] = {
            "session_id": session_id,
            "memory_policy": options["memory_policy"],
            "auto_commit_policy": options["auto_commit_policy"],
        }
        # OpenViking 0.5.2 create_session does not echo memory_policy; the
        # effective policy is only visible from a subsequent get_session.
        return {
            "session_id": session_id,
            "auto_commit_policy": options["auto_commit_policy"],
        }

    async def update_session_config(self, session_id: str, *, options: dict) -> dict:
        self.calls.append(("update", session_id, options))
        self.sessions[session_id].update(options)
        return self.sessions[session_id]

    async def delete_session(self, session_id: str) -> None:
        self.calls.append(("delete", session_id))
        self.sessions.pop(session_id, None)

    async def commit_session(self, session_id: str, *, keep_recent_count: int = 0) -> dict:
        self.calls.append(("commit", session_id, keep_recent_count))
        return {"session_id": session_id}


@pytest_asyncio.fixture
async def manager():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Base.metadata.tables["memory_sync_state"]]
            )
        )
    store = MemorySyncStore(async_sessionmaker(engine, expire_on_commit=False))
    client = FakeClient()
    config = OpenVikingMemoryConfig(user_id_salt="s" * 32)
    yield SessionManager(store, client, config), store, client, config
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_does_not_create_openviking_session(manager) -> None:
    session_manager, _store, client, _config = manager
    ref = await session_manager.resolve_capture_context("u1", "t1")

    assert ref.ov_session_id.startswith("mf-s-")
    assert ref.ov_user.startswith("u_")
    assert client.calls == []


@pytest.mark.asyncio
async def test_concurrent_first_resolution_uses_one_incarnation(manager) -> None:
    session_manager, _store, _client, _config = manager

    refs = await asyncio.gather(
        *(session_manager.resolve_capture_context("u1", "t1") for _ in range(8))
    )

    assert len({ref.ov_session_id for ref in refs}) == 1
    assert len({ref.session_incarnation for ref in refs}) == 1


@pytest.mark.asyncio
async def test_ensure_session_creates_with_explicit_policy(manager) -> None:
    session_manager, _store, client, _config = manager
    ref = await session_manager.resolve_capture_context("u1", "t1")
    await session_manager.ensure_session(ref)

    assert [call[0] for call in client.calls] == ["get", "create", "get"]
    assert client.calls[0] == ("get", ref.ov_session_id, False)
    options = client.calls[1][2]
    assert options["auto_commit_policy"]["idle_timeout_seconds"] == 86400
    assert options["auto_commit_policy"]["pending_token_threshold"] == 0
    assert options["memory_policy"]["memory_types"] == list(
        DEFAULT_SESSION_MEMORY_TYPES
    )


@pytest.mark.asyncio
async def test_existing_session_is_reused(manager) -> None:
    session_manager, _store, client, _config = manager
    ref = await session_manager.resolve_capture_context("u1", "t1")
    client.sessions[ref.ov_session_id] = {
        "session_id": ref.ov_session_id,
        "memory_policy": _config.session.memory_policy(),
        "auto_commit_policy": _config.session_create_options()["auto_commit_policy"],
    }
    await session_manager.ensure_session(ref)

    assert [call[0] for call in client.calls] == ["get"]


@pytest.mark.asyncio
async def test_memory_policy_mismatch_fails_closed(manager) -> None:
    session_manager, _store, client, _config = manager
    ref = await session_manager.resolve_capture_context("u1", "t1")
    client.sessions[ref.ov_session_id] = {
        "session_id": ref.ov_session_id,
        "memory_policy": {"memory_types": ["profile"]},
        "auto_commit_policy": {},
    }
    with pytest.raises(SessionPolicyMismatch):
        await session_manager.ensure_session(ref)


@pytest.mark.asyncio
async def test_missing_memory_type_allowlist_fails_closed(manager) -> None:
    session_manager, _store, client, config = manager
    ref = await session_manager.resolve_capture_context("u1", "t1")
    policy = config.session.memory_policy()
    policy.pop("memory_types")
    client.sessions[ref.ov_session_id] = {
        "session_id": ref.ov_session_id,
        "memory_policy": policy,
        "auto_commit_policy": config.session_create_options()["auto_commit_policy"],
    }

    with pytest.raises(SessionPolicyMismatch):
        await session_manager.ensure_session(ref)


@pytest.mark.asyncio
async def test_auto_policy_mismatch_is_updated(manager) -> None:
    session_manager, _store, client, config = manager
    ref = await session_manager.resolve_capture_context("u1", "t1")
    client.sessions[ref.ov_session_id] = {
        "session_id": ref.ov_session_id,
        "memory_policy": config.session.memory_policy(),
        "auto_commit_policy": {},
    }

    await session_manager.ensure_session(ref)

    assert [call[0] for call in client.calls] == ["get", "update"]
    assert client.sessions[ref.ov_session_id]["auto_commit_policy"] == config.session_create_options()["auto_commit_policy"]


@pytest.mark.asyncio
async def test_commit_marks_the_sync_state_done(manager) -> None:
    session_manager, store, client, _config = manager
    ref = await session_manager.resolve_capture_context("u1", "t1")

    await session_manager.commit(ref)

    assert client.calls[-1] == ("commit", ref.ov_session_id, 0)
    assert (await store.get("u1", "t1")).state == "done"


@pytest.mark.asyncio
async def test_tombstoned_thread_is_rejected(manager) -> None:
    session_manager, store, _client, _config = manager
    await session_manager.resolve_capture_context("u1", "t1")
    await store.mark_deleted("u1", "t1")
    with pytest.raises(SessionTombstoned):
        await session_manager.resolve_capture_context("u1", "t1")
