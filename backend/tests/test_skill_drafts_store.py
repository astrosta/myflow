"""Task 4: staging draft files and the projection outbox in SqlSkillStore."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.persistence.schema import (
    Base,
    SkillDraftFileRow,
    SkillIndexOutboxRow,
    SkillVersionRow,
)
from agentkit.skills.domain import StagedFile, StagedFileInfo
from agentkit.skills.package import build_package
from agentkit.skills.store.sql import OutboxEvent, SqlSkillStore

T = TypeVar("T")


@dataclass
class Harness:
    store: SqlSkillStore
    session_factory: async_sessionmaker[AsyncSession]

    def run(self, scenario: Callable[[], Awaitable[T]]) -> T:
        return asyncio.run(scenario())


@pytest.fixture(name="harness")
def harness_fixture(tmp_path: Path):
    async def _build() -> tuple[Harness, object]:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "skills.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return Harness(SqlSkillStore(factory), factory), engine

    built, engine = asyncio.run(_build())
    yield built
    asyncio.run(engine.dispose())


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _enqueue(
    harness: Harness,
    *,
    skill_id: str,
    key: str,
    payload: dict | None = None,
) -> None:
    await harness.store.enqueue_outbox(
        op="upsert_active",
        projection_key=f"private:u1:{key}",
        scope="private",
        owner_user_id="u1",
        skill_id=skill_id,
        version_id=f"version-{key}",
        payload=payload or {"enabled": True, "name": key},
    )


def test_put_draft_file_upserts_bytes_sha_size_and_mode(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="pdf-tools")
        first_content = b"# first\n"
        first = await harness.store.put_draft_file(skill.id, "SKILL.md", first_content)
        assert isinstance(first, StagedFileInfo)
        assert first.path == "SKILL.md"
        assert first.size_bytes == len(first_content)
        assert first.mode == "text"
        assert first.sha256 == hashlib.sha256(first_content).hexdigest()

        second_content = b"# second, longer body\n"
        second = await harness.store.put_draft_file(
            skill.id, "SKILL.md", second_content, mode="binary"
        )
        assert second.path == "SKILL.md"
        assert second.size_bytes == len(second_content)
        assert second.mode == "binary"
        assert second.sha256 == hashlib.sha256(second_content).hexdigest()
        assert second.sha256 != first.sha256

        await harness.store.put_draft_file(skill.id, "scripts/run.py", b"print('hi')\n")
        files = await harness.store.list_draft_files(skill.id)
        assert [file.path for file in files] == ["SKILL.md", "scripts/run.py"]
        assert all(isinstance(file, StagedFile) for file in files)
        assert files[0].content == second_content
        assert files[0].mode == "binary"
        assert files[1].content == b"print('hi')\n"

        async with harness.session_factory() as session:
            rows = (
                await session.scalars(
                    select(SkillDraftFileRow).where(SkillDraftFileRow.skill_id == skill.id)
                )
            ).all()
            assert len(rows) == 2
            stored = next(row for row in rows if row.path == "SKILL.md")
            assert stored.content == second_content
            assert stored.sha256 == second.sha256
            assert stored.size_bytes == len(second_content)

        assert await harness.store.delete_draft_file(skill.id, "SKILL.md") is True
        assert await harness.store.delete_draft_file(skill.id, "SKILL.md") is False
        assert [file.path for file in await harness.store.list_draft_files(skill.id)] == [
            "scripts/run.py"
        ]

        # Deleting the final staged file is allowed: staging is allowed to be empty.
        assert await harness.store.delete_draft_file(skill.id, "scripts/run.py") is True
        assert await harness.store.list_draft_files(skill.id) == []

    harness.run(scenario)


def test_delete_all_draft_files_keeps_versions_and_outbox(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="pdf-tools")
        manifest = build_package(
            {
                "SKILL.md": StagedFile(
                    "SKILL.md",
                    b"---\nname: pdf-tools\ndescription: Does things\n---\n\n# Body\n",
                    "text",
                )
            },
            max_files=20,
            max_file_bytes=4096,
            max_package_bytes=16384,
        )
        version = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest,
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="a" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        scan = await harness.store.record_security_scan(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            status="completed",
            is_safe=True,
            max_severity="SAFE",
            findings=[],
            scanner="cisco-ai-skill-scanner",
            scanner_version="2.3.2",
            duration_ms=1,
        )
        await harness.store.activate_version(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            scan_id=scan.id,
            actor="u1",
        )
        await harness.store.put_draft_file(skill.id, "SKILL.md", b"# staged\n")

        async with harness.session_factory() as session:
            versions_before = await session.scalar(
                select(func.count())
                .select_from(SkillVersionRow)
                .where(SkillVersionRow.skill_id == skill.id)
            )
            outbox_before = await session.scalar(
                select(func.count())
                .select_from(SkillIndexOutboxRow)
                .where(SkillIndexOutboxRow.skill_id == skill.id)
            )
        assert versions_before == 1
        assert outbox_before >= 1

        await harness.store.delete_all_draft_files(skill.id)
        assert await harness.store.list_draft_files(skill.id) == []
        async with harness.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(SkillDraftFileRow)
                    .where(SkillDraftFileRow.skill_id == skill.id)
                )
                == 0
            )
            assert await session.scalar(
                select(func.count())
                .select_from(SkillVersionRow)
                .where(SkillVersionRow.skill_id == skill.id)
            ) == versions_before
            assert await session.scalar(
                select(func.count())
                .select_from(SkillIndexOutboxRow)
                .where(SkillIndexOutboxRow.skill_id == skill.id)
            ) == outbox_before

    harness.run(scenario)


def test_claim_outbox_never_returns_an_event_to_two_sequential_callers(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="pdf-tools")
        await _enqueue(harness, skill_id=skill.id, key="one")
        await _enqueue(harness, skill_id=skill.id, key="two")
        assert await harness.store.pending_outbox_count() == 2

        first = await harness.store.claim_outbox(limit=1)
        second = await harness.store.claim_outbox(limit=1)
        third = await harness.store.claim_outbox(limit=1)
        assert len(first) == len(second) == 1
        assert third == []
        assert first[0].id != second[0].id
        assert {first[0].projection_key, second[0].projection_key} == {
            "private:u1:one",
            "private:u1:two",
        }
        events = {event.projection_key: event for event in (*first, *second)}
        event = events["private:u1:one"]
        assert isinstance(event, OutboxEvent)
        assert event.op == "upsert_active"
        assert event.scope == "private"
        assert event.owner_user_id == "u1"
        assert event.skill_id == skill.id
        assert event.version_id == "version-one"
        assert event.payload == {"enabled": True, "name": "one"}
        assert event.attempts == 1

        async with harness.session_factory() as session:
            rows = (await session.scalars(select(SkillIndexOutboxRow))).all()
            assert {row.status for row in rows} == {"running"}
            assert {row.attempts for row in rows} == {1}

        assert await harness.store.pending_outbox_count() == 0
        await harness.store.finish_outbox(event.id)
        async with harness.session_factory() as session:
            done = await session.get(SkillIndexOutboxRow, event.id)
            assert done is not None and done.status == "done"

    harness.run(scenario)


def test_retry_outbox_returns_to_pending_and_schedules_a_future_attempt(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="pdf-tools")
        await _enqueue(harness, skill_id=skill.id, key="retry-me")
        [event] = await harness.store.claim_outbox()
        assert event.attempts == 1

        before = datetime.now(UTC)
        await harness.store.retry_outbox(event.id, "embedding unavailable", 30)

        async with harness.session_factory() as session:
            row = await session.get(SkillIndexOutboxRow, event.id)
            assert row is not None
            assert row.status == "pending"
            assert row.attempts == 1
            assert row.last_error == "embedding unavailable"
            assert _aware(row.next_attempt_at) >= before + timedelta(seconds=25)

        assert await harness.store.pending_outbox_count() == 1
        # The delay is real: an immediate claim does not take the event early.
        assert await harness.store.claim_outbox() == []

        async with harness.session_factory() as session:
            row = await session.get(SkillIndexOutboxRow, event.id)
            assert row is not None
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        [retried] = await harness.store.claim_outbox()
        assert retried.id == event.id
        assert retried.attempts == 2
        await harness.store.finish_outbox(event.id)
        assert await harness.store.pending_outbox_count() == 0

    harness.run(scenario)


def test_enqueue_outbox_caps_payload_at_64_kib(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="pdf-tools")
        await _enqueue(
            harness,
            skill_id=skill.id,
            key="fits",
            payload={"blob": "x" * (64 * 1024 - 100)},
        )
        assert await harness.store.pending_outbox_count() == 1

        with pytest.raises(ValueError):
            await _enqueue(
                harness,
                skill_id=skill.id,
                key="too-big",
                payload={"blob": "x" * (64 * 1024)},
            )
        assert await harness.store.pending_outbox_count() == 1

    harness.run(scenario)