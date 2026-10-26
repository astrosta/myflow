"""Task 15: idempotent migration from the legacy BaseStore."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.legacy import LegacyMigrator
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.types import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    ScopePage,
)
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


class LegacyStore:
    def __init__(self, profile=None, records=None):
        self.profile = profile
        self.records = records or []

    def list_scopes(self, limit=100):
        del limit
        return ScopePage(items=[MemoryScope("u1")])

    def get_record(self, scope, kind, record_id):
        del scope, kind, record_id
        return self.profile

    def search_records(self, scope, query):
        del scope, query
        return MemorySearchPage(items=[{"record": record, "score": None} for record in self.records])


def test_legacy_migration_preserves_ids_and_is_idempotent(tmp_path) -> None:
    profile = MemoryRecord(
        id="profile",
        user_id="u1",
        kind=MemoryKind.PROFILE,
        source="user_manual",
        structured={"identity": {"preferred_name": {"value": "小王", "source": "user_manual"}}},
    )
    record = MemoryRecord(
        id="m1",
        user_id="u1",
        kind=MemoryKind.SEMANTIC,
        subtype="fact",
        content="用户偏好 pytest",
        structured={"content": "用户偏好 pytest"},
        topic_key="python.test",
        status="active",
        confidence=0.85,
        provenance={"message_id": "m1"},
    )

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "m.db").as_posix())
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
            migrator = LegacyMigrator(LegacyStore(profile, [record]), repo)
            first = await migrator.migrate(user_id="u1", dry_run=False)
            second = await migrator.migrate(user_id="u1", dry_run=False)
            assert first.migrated == 2
            assert second.migrated == 0
            assert await repo.get_record("u1", "m1") is not None
            assert (await repo.get_profile("u1")).revision == 1
        finally:
            await engine.dispose()

    run(scenario())
