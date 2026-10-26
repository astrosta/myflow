"""Task 7: profile direct write, manual edits, and field tombstones."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import MemoryConflictError
from agentkit.agents.memory.canonical.fakes import FakeOperators
from agentkit.agents.memory.canonical.profile import ProfileService
from agentkit.agents.memory.canonical.proposals import ProfileProposal, ProfileValue
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


async def profile_stack(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "p.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    return repo, ProfileService(repo, FakeOperators(), auto_write_threshold=0.85)


def profile_proposal(
    *,
    field: str,
    value: str | list[str],
    tier: str,
    quote: str,
) -> ProfileProposal:
    return ProfileProposal(
        fields={
            field: ProfileValue(
                value=value,
                evidence_tier=tier,
                evidence_quote=quote,
            )
        }
    )


def test_manual_edit_has_no_pin_and_high_evidence_overwrites(tmp_path) -> None:
    async def scenario():
        repo, service = await profile_stack(tmp_path)
        first = await service.patch_manual(
            "u1", {"communication.language": "zh-CN"}, expected_revision=0
        )
        assert first.revision == 1
        assert "pinned" not in first.fields["communication.language"].model_dump()

        result = await service.apply_extraction(
            "u1",
            current_message="以后都用英文回答我",
            proposal=profile_proposal(
                field="communication.language",
                value="en-US",
                tier="EXPLICIT_MANDATE",
                quote="以后都用英文回答我",
            ),
        )
        assert result.applied is True
        assert result.snapshot.fields["communication.language"].value == "en-US"
        assert result.snapshot.revision == 2
        ledger = await repo.last_ledger("u1")
        assert ledger is not None
        assert ledger.inverse_json["profile"]["communication"]["language"]["value"] == "zh-CN"

    run(scenario())


def test_low_evidence_or_foreign_quote_holds(tmp_path) -> None:
    async def scenario():
        _repo, service = await profile_stack(tmp_path)
        low = await service.apply_extraction(
            "u1",
            current_message="我可能喜欢英文",
            proposal=profile_proposal(
                field="communication.language",
                value="en-US",
                tier="BEHAVIORAL_OBSERVED",
                quote="我可能喜欢英文",
            ),
        )
        assert low.applied is False and low.conflict_id

        foreign = await service.apply_extraction(
            "u1",
            current_message="今天天气不错",
            proposal=profile_proposal(
                field="communication.language",
                value="en-US",
                tier="EXPLICIT_MANDATE",
                quote="以后都用英文回答我",
            ),
        )
        assert foreign.applied is False
        assert foreign.conflict_id is None

    run(scenario())


def test_stale_revision_is_conflict_and_clear_is_tombstone(tmp_path) -> None:
    async def scenario():
        _repo, service = await profile_stack(tmp_path)
        await service.patch_manual(
            "u1", {"identity.preferred_name": "小王"}, expected_revision=0
        )
        with pytest.raises(MemoryConflictError, match="revision"):
            await service.patch_manual(
                "u1", {"identity.preferred_name": "小李"}, expected_revision=0
            )
        cleared = await service.clear_field("u1", "identity.preferred_name")
        assert "identity.preferred_name" not in cleared.fields

    run(scenario())
