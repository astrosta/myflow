"""Vertical slice: policy, capture, append and profile through the facade."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.adapter import MemoryMessageAdapter
from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from agentkit.agents.memory.openviking.facade import MemoryFacade
from agentkit.agents.memory.openviking.profile import PersonalizationService, ProfileService
from agentkit.agents.memory.openviking.recall import RecallService
from agentkit.agents.memory.openviking.session import SessionManager
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.agents.memory.openviking.stream import InMemoryCaptureStream, MemoryMessagePublisher
from agentkit.agents.memory.openviking.worker import TurnCaptureWorker
from agentkit.persistence.base import Base


class Client:
    def __init__(self, config: OpenVikingMemoryConfig) -> None:
        self.config = config
        self.sessions: dict[str, dict] = {}
        self.appended: list[dict] = []
        self.committed: list[tuple[str, int]] = []

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

    async def commit_session(self, session_id: str, *, keep_recent_count: int = 0) -> dict:
        self.committed.append((session_id, keep_recent_count))
        return {"session_id": session_id, "status": "accepted"}

    async def find(self, **kwargs) -> dict:
        return {
            "memories": [
                {
                    "uri": "viking://user/u_hash/memories/preferences/style.md",
                    "abstract": "喜欢简洁",
                    "score": 0.7,
                    "level": 0,
                }
            ]
        }

    async def read(self, uri: str) -> str:
        return "content"

    async def overview(self, uri: str) -> str:
        return "overview"


@pytest_asyncio.fixture
async def facade(tmp_path: Path):
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
    config = OpenVikingMemoryConfig(user_id_salt="s" * 32)
    client = Client(config)
    sync_store = MemorySyncStore(factory)
    dedup = CaptureDedupStore(factory)
    sessions = SessionManager(sync_store, client, config)
    stream = InMemoryCaptureStream()
    publisher = MemoryMessagePublisher(stream)
    worker = TurnCaptureWorker(MemoryMessageAdapter(), stream, dedup, sessions, sync_store)
    profile = ProfileService(factory, seed)
    personalization = PersonalizationService(
        profile,
        identity_path=tmp_path / "identity.md",
        soul_path=tmp_path / "soul.md",
    )
    service = MemoryFacade(
        config=config,
        sync_store=sync_store,
        dedup=dedup,
        sessions=sessions,
        stream=stream,
        publisher=publisher,
        worker=worker,
        profile=profile,
        personalization=personalization,
        recall=RecallService(client),
    )
    yield service, client, stream
    await engine.dispose()


@pytest.mark.asyncio
async def test_facade_capture_and_append(facade) -> None:
    service, client, _stream = facade
    assert service.decide_capture("你好").disposition == "skip"
    assert service.decide_capture("我住杭州").disposition == "capture"

    ref = await service.resolve_capture_context("u1", "t1")
    captured = await service.publish_human(ref, "r1", HumanMessage(content="我住杭州"))
    assert await service.process(captured) == "appended"
    assert client.appended[0]["message_kind"] == "user_query"
    status = await service.status()
    assert status.pending_missing_turn_ends == 0


@pytest.mark.asyncio
async def test_facade_flush_commits_active_sessions(facade) -> None:
    service, client, _stream = facade
    await service.resolve_capture_context("u1", "t1")

    assert await service.flush() == 1
    assert client.committed[0][1] == 0


@pytest.mark.asyncio
async def test_facade_profile(facade) -> None:
    service, _client, _stream = facade
    view = await service.get_profile("u1")
    assert view.fields["identity.preferred_name"] == "小王"
    personalization = await service.get_personalization("u1")
    assert personalization.identity.content == ""

    hits = await service.search("u1", "style", memory_types=["preferences"], limit=5)
    assert hits[0].memory_type == "preferences"
