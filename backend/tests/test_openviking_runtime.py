"""Composition-root smoke test for the OpenViking runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from agentkit.agents.memory.openviking.runtime import OpenVikingMemoryRuntime
from agentkit.persistence.base import Base


class Client:
    async def get_session(self, session_id, *, auto_create=False):
        return {
            "session_id": session_id,
            "memory_policy": {"memory_types": ["preferences", "entities", "events", "cases", "trajectories", "experiences"]},
            "auto_commit_policy": {
                "pending_token_threshold": 0,
                "message_count_threshold": 0,
                "idle_timeout_seconds": 86400,
                "keep_recent_count": 0,
                "min_commit_interval_seconds": 0,
            },
        }

    async def create_session(self, session_id, *, options):
        return await self.get_session(session_id)

    async def update_session_config(self, session_id, *, options):
        return await self.get_session(session_id)

    async def delete_session(self, session_id):
        return None

    async def batch_add_messages(self, session_id, messages):
        return {"messages": []}

    async def find(self, **kwargs):
        return {"memories": []}

    async def read(self, uri):
        return "body"

    async def overview(self, uri):
        return "overview"


class Resolver:
    def __init__(self):
        self.client = Client()
        self.closed = False

    async def client_for(self, ov_user):
        return self.client

    async def close(self):
        self.closed = True


@pytest_asyncio.fixture
async def stack(tmp_path: Path):
    seed = tmp_path / "profile.md"
    seed.write_text("---\npreferred_name: 小王\n---\n", encoding="utf-8")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables["memory_sync_state"],
        Base.metadata.tables["memory_capture_dedup"],
        Base.metadata.tables["memory_profile_documents"],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    resolver = Resolver()
    runtime = OpenVikingMemoryRuntime.build(
        OpenVikingMemoryConfig(enabled=True, user_id_salt="s" * 32),
        factory,
        resolver,
        seed_path=seed,
    )
    yield runtime, resolver
    await engine.dispose()


def test_runtime_requires_a_strong_user_id_salt(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="user_id_salt"):
            OpenVikingMemoryRuntime.build(
                OpenVikingMemoryConfig(enabled=True, user_id_salt="short"),
                async_sessionmaker(engine, expire_on_commit=False),
                Resolver(),
            )
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.asyncio
async def test_runtime_builds_facade_tools_and_profile(stack) -> None:
    runtime, resolver = stack
    assert runtime.facade is not None
    assert len(runtime.tools) == 3
    assert (await runtime.profile.get_snapshot("u1")).fields["identity.preferred_name"] == "小王"

    await runtime.start()
    await runtime.stop()
    assert resolver.closed is True
