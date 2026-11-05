"""SQL store for user-private skills, scans and explicit activation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.persistence.schema import (
    Base,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillRow,
    SkillSecurityScanRow,
)
from agentkit.skills.domain import (
    PackageManifest,
    SkillConflict,
    SkillScope,
    StagedFile,
)
from agentkit.skills.package import build_package
from agentkit.skills.store.sql import SqlSkillStore

LIMITS = {"max_files": 20, "max_file_bytes": 4096, "max_package_bytes": 16384}
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProjectionHit:
    projection_key: str
    scope: str
    owner_user_id: str | None
    skill_id: str
    version_id: str
    score: float


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


def skill_md(name: str, description: str = "Does things", body: str = "# Body\n") -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}".encode()


def manifest(
    name: str = "pdf-tools",
    *,
    description: str = "Does things",
    body: str = "# Body\n",
    extra: Mapping[str, StagedFile] | None = None,
) -> PackageManifest:
    files: dict[str, StagedFile] = {
        "SKILL.md": StagedFile("SKILL.md", skill_md(name, description, body), "text")
    }
    files.update(extra or {})
    return build_package(files, **LIMITS)


async def save_version(
    harness: Harness,
    *,
    owner: str = "u1",
    name: str = "pdf-tools",
    description: str = "Does things",
    commit: str = "a" * 40,
):
    skill = await harness.store.create_skill(owner_user_id=owner, private_name=name)
    version = await harness.store.record_saved_version(
        skill_id=skill.id,
        owner_user_id=owner,
        manifest=manifest(name, description=description),
        draft_ref=f"refs/heads/myflow/drafts/{owner}",
        draft_commit=commit,
        skill_path=f"users/{owner}/{skill.id}/SKILL.md",
    )
    return skill, version


async def scan_version(
    harness: Harness,
    *,
    skill,
    version,
    owner: str = "u1",
    safe: bool = True,
    status: str = "completed",
    severity: str | None = None,
    findings: list[dict] | None = None,
):
    return await harness.store.record_security_scan(
        skill_id=skill.id,
        owner_user_id=owner,
        version_id=version.id,
        status=status,
        is_safe=safe,
        max_severity=severity or ("SAFE" if safe else "HIGH"),
        findings=findings or [],
        scanner="cisco-ai-skill-scanner",
        scanner_version="2.3.2",
        duration_ms=12,
        error="boom" if status == "failed" else None,
    )


def test_create_skill_is_scoped_to_owner_and_rejects_duplicate(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="My-Skill")
        assert skill.owner_user_id == "u1"
        assert skill.deleted_at is None
        assert (await harness.store.get_skill(skill.id)).private_name == "My-Skill"

        with pytest.raises(ValueError):
            await harness.store.create_skill(owner_user_id="u1", private_name="")
        with pytest.raises(SkillConflict):
            await harness.store.create_skill(owner_user_id="u1", private_name="My-Skill")

        other = await harness.store.create_skill(owner_user_id="u2", private_name="My-Skill")
        assert [item.id for item in await harness.store.list_owned("u1")] == [skill.id]
        assert [item.id for item in await harness.store.list_owned("u2")] == [other.id]
        assert await harness.store.get_skill("missing") is None

    harness.run(scenario)


def test_new_skill_defaults_to_enabled(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="pdf-tools")
        assert skill.enabled is True

    harness.run(scenario)


def test_toggle_updates_visibility_and_enqueues_projection(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        scan = await scan_version(harness, skill=skill, version=version)
        await harness.store.activate_version(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            scan_id=scan.id,
            actor="u1",
        )

        disabled = await harness.store.set_skill_enabled(
            skill_id=skill.id,
            owner_user_id="u1",
            enabled=False,
            index_enabled=True,
        )
        assert disabled.enabled is False
        assert await harness.store.visible_for_user("u1") == []
        assert (await harness.store.get_version(skill.id, version.id)).index_status == "pending"
        async with harness.session_factory() as session:
            events = (
                await session.scalars(
                    select(SkillIndexOutboxRow)
                    .where(SkillIndexOutboxRow.skill_id == skill.id)
                    .order_by(SkillIndexOutboxRow.created_at.asc())
                )
            ).all()
            assert events[-1].payload_json["enabled"] is False

        enabled = await harness.store.set_skill_enabled(
            skill_id=skill.id,
            owner_user_id="u1",
            enabled=True,
            index_enabled=False,
        )
        assert enabled.enabled is True
        assert [row.skill.skill_id for row in await harness.store.visible_for_user("u1")] == [
            skill.id
        ]

    harness.run(scenario)


def test_saving_a_version_is_inert_and_reuses_identical_content(harness: Harness) -> None:
    async def scenario() -> None:
        skill, first = await save_version(harness)
        assert first.skill_version == "v1"
        assert first.index_status == "unindexed"
        assert first.draft_commit == "a" * 40

        same = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest(description="Does things"),
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="b" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        assert same.id == first.id
        assert same.draft_commit == "a" * 40

        again = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest(description="Changed"),
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="c" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        assert again.skill_version == "v2"
        assert again.draft_commit == "c" * 40
        assert await harness.store.latest_version(skill.id) == again

        async with harness.session_factory() as session:
            assert await session.get(SkillPrivateActiveRow, skill.id) is None
            outbox = (
                await session.scalars(
                    select(SkillIndexOutboxRow).where(
                        SkillIndexOutboxRow.skill_id == skill.id
                    )
                )
            ).all()
            assert outbox == []
        assert await harness.store.visible_for_user("u1") == []

    harness.run(scenario)


def test_scan_is_persisted_without_activating(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        scan = await scan_version(
            harness,
            skill=skill,
            version=version,
            findings=[
                {
                    "rule_id": "R1",
                    "title": "Issue",
                    "severity": "MEDIUM",
                    "category": "policy_violation",
                    "file_path": "SKILL.md",
                    "line_number": 3,
                    "remediation": "Do better",
                }
            ],
        )
        assert scan.version_id == version.id
        assert scan.package_sha256 == version.package_sha256
        assert scan.is_safe is True
        assert scan.findings_count == 1
        assert scan.findings[0]["rule_id"] == "R1"
        assert await harness.store.latest_security_scan(skill.id, version.id) == scan
        assert len(await harness.store.list_security_scans(skill.id)) == 1

        async with harness.session_factory() as session:
            assert await session.get(SkillPrivateActiveRow, skill.id) is None
            row = await session.get(SkillSecurityScanRow, scan.id)
            assert row is not None and row.scanner_version == "2.3.2"
        assert await harness.store.visible_for_user("u1") == []

    harness.run(scenario)


def test_activation_requires_a_passing_scan_for_the_latest_version(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        scan = await scan_version(harness, skill=skill, version=version)
        active = await harness.store.activate_version(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            scan_id=scan.id,
            actor="u1",
        )
        assert active.id == version.id
        assert active.index_status == "pending"
        assert await harness.store.private_active(skill.id) == active
        assert [row.skill.skill_id for row in await harness.store.visible_for_user("u1")] == [
            skill.id
        ]
        assert await harness.store.visible_for_user("u2") == []

        async with harness.session_factory() as session:
            pointer = await session.get(SkillPrivateActiveRow, skill.id)
            assert pointer is not None and pointer.version_id == version.id
            outbox = (
                await session.scalars(
                    select(SkillIndexOutboxRow).where(
                        SkillIndexOutboxRow.skill_id == skill.id
                    )
                )
            ).all()
            assert len(outbox) == 1
            assert outbox[0].op == "upsert_active"
            assert outbox[0].projection_key == f"private:u1:{skill.id}"
            assert outbox[0].version_id == version.id

    harness.run(scenario)


def test_activation_rejects_missing_failed_or_stale_scan(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        with pytest.raises(SkillConflict, match="scan"):
            await harness.store.activate_version(
                skill_id=skill.id,
                owner_user_id="u1",
                version_id=version.id,
                scan_id="missing",
                actor="u1",
            )

        failed = await scan_version(
            harness, skill=skill, version=version, safe=False, severity="CRITICAL"
        )
        with pytest.raises(SkillConflict, match="did not pass"):
            await harness.store.activate_version(
                skill_id=skill.id,
                owner_user_id="u1",
                version_id=version.id,
                scan_id=failed.id,
                actor="u1",
            )

        newer = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest(description="v2"),
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="d" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        passed = await scan_version(harness, skill=skill, version=newer)
        with pytest.raises(SkillConflict, match="latest"):
            await harness.store.activate_version(
                skill_id=skill.id,
                owner_user_id="u1",
                version_id=version.id,
                scan_id=passed.id,
                actor="u1",
            )

        with pytest.raises(SkillConflict, match="does not exist"):
            await harness.store.activate_version(
                skill_id=skill.id,
                owner_user_id="u2",
                version_id=newer.id,
                scan_id=passed.id,
                actor="u2",
            )

    harness.run(scenario)


def test_confirm_hits_accepts_only_the_owners_active_version(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        scan = await scan_version(harness, skill=skill, version=version)
        await harness.store.activate_version(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            scan_id=scan.id,
            actor="u1",
        )
        good = ProjectionHit(
            projection_key=f"private:u1:{skill.id}",
            scope="private",
            owner_user_id="u1",
            skill_id=skill.id,
            version_id=version.id,
            score=0.9,
        )
        stale = replace(good, version_id="other")
        foreign = replace(good, owner_user_id="u2")
        public = replace(good, scope="public")

        assert [row.version.id for row in await harness.store.confirm_hits("u1", [good])] == [
            version.id
        ]
        assert await harness.store.confirm_hits("u1", [stale, foreign, public]) == []
        assert await harness.store.confirm_hits("u2", [good]) == []

    harness.run(scenario)


def test_delete_removes_active_pointer_and_enqueues_projection_delete(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        scan = await scan_version(harness, skill=skill, version=version)
        await harness.store.activate_version(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            scan_id=scan.id,
            actor="u1",
        )
        assert await harness.store.delete_private_draft(
            skill_id=skill.id, owner_user_id="u1", actor="u1"
        )
        assert await harness.store.visible_for_user("u1") == []
        async with harness.session_factory() as session:
            row = await session.get(SkillRow, skill.id)
            assert row is not None and row.deleted_at is not None
            assert await session.get(SkillPrivateActiveRow, skill.id) is None
            event = await session.scalar(
                select(SkillIndexOutboxRow).where(
                    SkillIndexOutboxRow.projection_key == f"private:u1:{skill.id}",
                    SkillIndexOutboxRow.op == "delete_active",
                )
            )
            assert event is not None and event.version_id is None

    harness.run(scenario)


def test_outbox_claim_and_finish_are_owner_neutral(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        scan = await scan_version(harness, skill=skill, version=version)
        await harness.store.activate_version(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            scan_id=scan.id,
            actor="u1",
        )
        events = await harness.store.claim_outbox()
        assert len(events) == 1
        assert events[0].payload["name"] == "pdf-tools"
        await harness.store.finish_outbox(events[0].id)
        assert await harness.store.pending_outbox_count() == 0

    harness.run(scenario)


def test_a_successful_load_increments_the_persisted_count(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await save_version(harness)
        scan = await scan_version(harness, skill=skill, version=version)
        await harness.store.activate_version(
            skill_id=skill.id,
            owner_user_id="u1",
            version_id=version.id,
            scan_id=scan.id,
            actor="u1",
        )

        assert await harness.store.increment_skill_load_count(skill.id) == 1
        assert await harness.store.increment_skill_load_count(skill.id) == 2
        # 播种只认"真的被加载过"的技能：0 次的行不进 Redis。
        assert await harness.store.list_skill_load_counts() == [("u1", skill.id, 2)]
        active = await harness.store.get_active_visible(
            scope=SkillScope.PRIVATE, skill_id=skill.id, owner_user_id="u1"
        )
        assert active is not None and active.skill.load_count == 2

    harness.run(scenario)


def test_load_counts_ignore_unknown_and_deleted_skills(harness: Harness) -> None:
    async def scenario() -> None:
        skill, _version = await save_version(harness)
        assert await harness.store.list_skill_load_counts() == []
        assert await harness.store.increment_skill_load_count("missing") == 0

        await harness.store.delete_private_draft(
            skill_id=skill.id, owner_user_id="u1", actor="u1"
        )
        assert await harness.store.increment_skill_load_count(skill.id) == 0
        assert await harness.store.list_skill_load_counts() == []

    harness.run(scenario)
