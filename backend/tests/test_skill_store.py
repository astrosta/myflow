"""Task 3: SQL store for catalog identity, versions, reviews, and active pointers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.persistence.schema import (
    Base,
    SkillAuditEventRow,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillPublicActiveRow,
    SkillRow,
    SkillSettingsRow,
)
from agentkit.skills.domain import (
    PackageManifest,
    ReviewDetail,
    ReviewItem,
    SkillConflict,
    SkillScope,
    SkillSubmissionRecord,
    StagedFile,
)
from agentkit.skills.package import build_package
from agentkit.skills.store.sql import SqlSkillStore

LIMITS = {"max_files": 20, "max_file_bytes": 4096, "max_package_bytes": 16384}
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProjectionHit:
    """The Task 10 shape, so this test pins the contract without Milvus."""

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


async def create_owned(
    harness: Harness,
    *,
    owner: str,
    name: str,
    description: str = "Does things",
) -> tuple[object, object]:
    skill = await harness.store.create_skill(owner_user_id=owner, private_name=name)
    version = await harness.store.record_saved_version(
        skill_id=skill.id,
        owner_user_id=owner,
        manifest=manifest(name, description=description),
        draft_ref=f"refs/heads/myflow/drafts/{owner}",
        draft_commit=("a" if owner == "u1" else "b") * 40,
        skill_path=f"users/{owner}/{skill.id}/SKILL.md",
    )
    return skill, version


async def approve(
    harness: Harness,
    *,
    owner: str,
    skill,
    version,
    public_name: str | None = None,
    commit: str = "c" * 40,
):
    submission = await harness.store.create_submission(
        skill_id=skill.id,
        version=version,
        owner_user_id=owner,
        submitted_commit=version.draft_commit or commit,
    )
    return await harness.store.record_public_release(
        submission=submission,
        public_name=public_name or skill.private_name,
        git_repository="https://example.invalid/skills.git",
        git_commit=commit,
        skill_path=f"public/{public_name or skill.private_name}/SKILL.md",
        source_commit=submission.submitted_commit,
        actor="admin",
    )


def test_create_skill_preserves_case_rejects_blank_and_duplicate(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="My-Skill")
        assert skill.private_name == "My-Skill"
        assert skill.owner_user_id == "u1"
        assert skill.public_name is None
        assert skill.deleted_at is None
        assert (await harness.store.get_skill(skill.id)).private_name == "My-Skill"

        with pytest.raises(ValueError):
            await harness.store.create_skill(owner_user_id="u1", private_name="")
        with pytest.raises(ValueError):
            await harness.store.create_skill(owner_user_id="u1", private_name="   ")

        with pytest.raises(SkillConflict) as duplicate:
            await harness.store.create_skill(owner_user_id="u1", private_name="My-Skill")
        assert duplicate.value.code == "skill_conflict"

        other_owner = await harness.store.create_skill(
            owner_user_id="u2", private_name="My-Skill"
        )
        assert [item.id for item in await harness.store.list_owned("u1")] == [skill.id]
        assert [item.id for item in await harness.store.list_owned("u2")] == [other_owner.id]
        assert await harness.store.get_skill("missing") is None

    harness.run(scenario)


def test_record_saved_version_reuses_hash_and_allocates_monotonic_versions(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill = await harness.store.create_skill(owner_user_id="u1", private_name="pdf-tools")
        first = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest(description="first"),
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="a" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        assert first.skill_version == "v1"
        assert first.version_seq == 1
        assert first.index_status == "pending"
        assert first.metadata["name"] == "pdf-tools"
        assert first.metadata["description"] == "first"

        same = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest(description="first"),
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="b" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        assert same.id == first.id
        assert same.skill_version == "v1"
        assert same.draft_commit == "a" * 40
        assert await harness.store.next_version_seq(skill.id) == 2
        found = await harness.store.find_version_by_hash(skill.id, first.package_sha256)
        assert found is not None and found.id == first.id

        second = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest(description="second"),
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="c" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        assert second.skill_version == "v2"
        assert second.version_seq == 2
        assert await harness.store.next_version_seq(skill.id) == 3
        assert [item.skill_version for item in await harness.store.list_versions(skill.id)] == [
            "v1",
            "v2",
        ]

        async with harness.session_factory() as session:
            active = await session.get(SkillPrivateActiveRow, skill.id)
            assert active is not None
            assert active.owner_user_id == "u1"
            assert active.version_id == second.id
            outbox = (
                await session.scalars(
                    select(SkillIndexOutboxRow).where(
                        SkillIndexOutboxRow.projection_key
                        == f"private:u1:{skill.id}"
                    )
                )
            ).all()
            assert len(outbox) == 3
            assert all(event.op == "upsert_active" for event in outbox)
            assert outbox[-1].version_id == second.id
            audits = (
                await session.scalars(
                    select(SkillAuditEventRow).where(
                        SkillAuditEventRow.skill_id == skill.id,
                        SkillAuditEventRow.action == "skill.save",
                    )
                )
            ).all()
            assert len(audits) == 3

    harness.run(scenario)


def test_only_one_pending_submission_and_status_transitions(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await create_owned(harness, owner="u1", name="pdf-tools")
        submission = await harness.store.create_submission(
            skill_id=skill.id,
            version=version,
            owner_user_id="u1",
            submitted_commit=version.draft_commit,
        )
        assert isinstance(submission, SkillSubmissionRecord)
        assert submission.status == "pending"
        assert (await harness.store.pending_submission(skill.id)).id == submission.id
        assert (await harness.store.submission(submission.id)).id == submission.id
        assert await harness.store.submission("missing") is None

        with pytest.raises(SkillConflict) as duplicate:
            await harness.store.create_submission(
                skill_id=skill.id,
                version=version,
                owner_user_id="u1",
                submitted_commit=version.draft_commit,
            )
        assert duplicate.value.code == "skill_conflict"

        reviewed = await harness.store.set_submission_status(
            submission.id,
            status="rejected",
            reviewed_by="admin",
            reject_reason="needs tests",
        )
        assert reviewed.status == "rejected"
        assert reviewed.reviewed_by == "admin"
        assert reviewed.reviewed_at is not None
        assert reviewed.reject_reason == "needs tests"
        assert await harness.store.pending_submission(skill.id) is None
        latest = await harness.store.latest_submission(skill.id)
        assert latest is not None
        assert latest.status == "rejected"
        assert latest.reject_reason == "needs tests"

        again = await harness.store.create_submission(
            skill_id=skill.id,
            version=version,
            owner_user_id="u1",
            submitted_commit=version.draft_commit,
        )
        assert again.status == "pending"
        newest = await harness.store.latest_submission(skill.id)
        assert newest is not None and newest.id == again.id

        with pytest.raises(SkillConflict):
            await harness.store.set_submission_status("missing", status="approved")

        async with harness.session_factory() as session:
            actions = (
                await session.scalars(
                    select(SkillAuditEventRow.action).where(
                        SkillAuditEventRow.submission_id == submission.id
                    )
                )
            ).all()
            assert "skill.submit" in actions
            assert "skill.reject" in actions

    harness.run(scenario)


def test_visible_for_user_is_public_plus_only_the_callers_private_rows(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        public_skill, public_version = await create_owned(
            harness, owner="u2", name="shared-pdf", description="public"
        )
        release = await approve(
            harness,
            owner="u2",
            skill=public_skill,
            version=public_version,
            public_name="shared-pdf",
        )
        own_skill, own_version = await create_owned(
            harness, owner="u1", name="my-private", description="mine"
        )
        their_skill, _ = await create_owned(
            harness, owner="u2", name="their-private", description="theirs"
        )

        visible = await harness.store.visible_for_user("u1")
        assert {(item.skill.skill_id, item.scope) for item in visible} == {
            (public_skill.id, SkillScope.PUBLIC),
            (own_skill.id, SkillScope.PRIVATE),
        }
        public = next(item for item in visible if item.scope is SkillScope.PUBLIC)
        assert public.version.id == public_version.id
        assert public.release is not None and public.release.id == release.id
        assert public.skill.name == "shared-pdf"
        assert public.skill.enabled is True
        private = next(item for item in visible if item.scope is SkillScope.PRIVATE)
        assert private.version.id == own_version.id
        assert private.release is None
        assert private.skill.owner_user_id == "u1"

        assert await harness.store.private_skill_ids("u1") == (own_skill.id,)
        other = await harness.store.visible_for_user("u2")
        assert {item.skill.skill_id for item in other} == {public_skill.id, their_skill.id}
        nobody = await harness.store.visible_for_user("nobody")
        assert [item.scope for item in nobody] == [SkillScope.PUBLIC]

    harness.run(scenario)


def test_confirm_hits_rejects_another_users_private_hit_and_stale_rows(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        public_skill, public_version = await create_owned(
            harness, owner="u2", name="shared-pdf"
        )
        release = await approve(harness, owner="u2", skill=public_skill, version=public_version)
        own_skill, own_version = await create_owned(harness, owner="u1", name="my-private")
        other_skill, other_version = await create_owned(
            harness, owner="u2", name="their-private"
        )

        other_hit = ProjectionHit(
            projection_key=f"private:u2:{other_skill.id}",
            scope="private",
            owner_user_id="u2",
            skill_id=other_skill.id,
            version_id=other_version.id,
            score=0.9,
        )
        assert await harness.store.confirm_hits("u1", [other_hit]) == []

        own_hit = ProjectionHit(
            projection_key=f"private:u1:{own_skill.id}",
            scope="private",
            owner_user_id="u1",
            skill_id=own_skill.id,
            version_id=own_version.id,
            score=0.8,
        )
        stale_hit = ProjectionHit(
            projection_key=f"private:u1:{own_skill.id}",
            scope="private",
            owner_user_id="u1",
            skill_id=own_skill.id,
            version_id="stale-version",
            score=0.7,
        )
        public_hit = ProjectionHit(
            projection_key=f"public:{public_skill.id}",
            scope="public",
            owner_user_id=None,
            skill_id=public_skill.id,
            version_id=public_version.id,
            score=0.6,
        )
        confirmed = await harness.store.confirm_hits(
            "u1", [other_hit, own_hit, stale_hit, public_hit, public_hit]
        )
        assert [(item.scope, item.skill.skill_id) for item in confirmed] == [
            (SkillScope.PRIVATE, own_skill.id),
            (SkillScope.PUBLIC, public_skill.id),
        ]
        assert confirmed[1].release is not None and confirmed[1].release.id == release.id

        await harness.store.set_global_enabled(public_skill.id, False, actor="admin")
        assert await harness.store.confirm_hits("u1", [public_hit]) == []
        await harness.store.set_global_enabled(public_skill.id, True, actor="admin")
        assert len(await harness.store.confirm_hits("u1", [public_hit])) == 1

    harness.run(scenario)


def test_public_release_pending_queue_and_review_detail(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await create_owned(harness, owner="u1", name="pdf-tools")
        submission = await harness.store.create_submission(
            skill_id=skill.id,
            version=version,
            owner_user_id="u1",
            submitted_commit=version.draft_commit,
        )

        queue = await harness.store.list_pending_submissions()
        assert len(queue) == 1
        assert isinstance(queue[0], ReviewItem)
        assert queue[0].submission_id == submission.id
        assert queue[0].skill_id == skill.id
        assert queue[0].name == "pdf-tools"
        assert queue[0].version == "v1"
        assert queue[0].owner_user_id == "u1"
        assert queue[0].submitted_commit == version.draft_commit

        detail = await harness.store.review_detail(submission.id)
        assert isinstance(detail, ReviewDetail)
        assert detail.item.submission_id == submission.id
        assert detail.metadata["description"] == "Does things"
        assert detail.files == []
        assert detail.skill_md == ""
        assert detail.diff == ""
        assert detail.package_bytes == 0
        assert await harness.store.review_detail("missing") is None

        release = await harness.store.record_public_release(
            submission=submission,
            public_name="pdf-tools",
            git_repository="https://example.invalid/skills.git",
            git_commit="d" * 40,
            skill_path="public/pdf-tools/SKILL.md",
            source_commit=submission.submitted_commit,
            actor="admin",
        )
        assert release.skill_id == skill.id
        assert release.version_id == version.id
        assert release.skill_version == "v1"
        assert release.public_name == "pdf-tools"
        assert release.git_repository == "https://example.invalid/skills.git"
        assert release.git_commit == "d" * 40
        assert release.skill_path == "public/pdf-tools/SKILL.md"
        assert release.source_commit == submission.submitted_commit
        assert release.approved_by == "admin"

        approved = await harness.store.submission(submission.id)
        assert approved.status == "approved"
        assert approved.public_release_id == release.id
        assert approved.reviewed_by == "admin"
        assert approved.reviewed_at is not None
        assert await harness.store.list_pending_submissions() == []
        assert (await harness.store.get_skill(skill.id)).public_name == "pdf-tools"

        async with harness.session_factory() as session:
            active = await session.get(SkillPublicActiveRow, skill.id)
            assert active is not None and active.release_id == release.id
            settings = await session.get(SkillSettingsRow, skill.id)
            assert settings is not None and settings.enabled is True
            outbox = await session.scalar(
                select(SkillIndexOutboxRow).where(
                    SkillIndexOutboxRow.projection_key == f"public:{skill.id}"
                )
            )
            assert outbox is not None
            assert outbox.op == "upsert_active"
            assert outbox.version_id == version.id
            assert outbox.payload_json["enabled"] is True
            assert outbox.payload_json["package_sha256"] == version.package_sha256
            audits = (
                await session.scalars(
                    select(SkillAuditEventRow.action).where(
                        SkillAuditEventRow.skill_id == skill.id
                    )
                )
            ).all()
            assert "skill.approve" in audits

        with pytest.raises(SkillConflict):
            await harness.store.record_public_release(
                submission=submission,
                public_name="pdf-tools",
                git_repository="https://example.invalid/skills.git",
                git_commit="e" * 40,
                skill_path="public/pdf-tools/SKILL.md",
                source_commit=submission.submitted_commit,
                actor="admin",
            )

    harness.run(scenario)


def test_active_pointer_rollback_clear_and_global_setting(harness: Harness) -> None:
    async def scenario() -> None:
        skill, first = await create_owned(harness, owner="u1", name="pdf-tools")
        first_release = await approve(
            harness, owner="u1", skill=skill, version=first, commit="a" * 40
        )

        second = await harness.store.record_saved_version(
            skill_id=skill.id,
            owner_user_id="u1",
            manifest=manifest(description="v2"),
            draft_ref="refs/heads/myflow/drafts/u1",
            draft_commit="f" * 40,
            skill_path=f"users/u1/{skill.id}/SKILL.md",
        )
        second_release = await approve(
            harness, owner="u1", skill=skill, version=second, commit="b" * 40
        )

        await harness.store.set_public_active(skill.id, first_release.id, actor="admin")
        visible = await harness.store.visible_for_user("u2")
        public = next(item for item in visible if item.scope is SkillScope.PUBLIC)
        assert public.version.id == first.id
        assert public.release.id == first_release.id

        await harness.store.set_public_active(skill.id, second_release.id, actor="admin")
        visible = await harness.store.visible_for_user("u2")
        public = next(item for item in visible if item.scope is SkillScope.PUBLIC)
        assert public.version.id == second.id
        assert public.release.id == second_release.id

        with pytest.raises(SkillConflict):
            await harness.store.set_public_active(skill.id, "missing", actor="admin")

        await harness.store.set_global_enabled(skill.id, False, actor="admin")
        assert (await harness.store.visible_for_user("u2"))[0].skill.enabled is False
        async with harness.session_factory() as session:
            latest = await session.scalar(
                select(SkillIndexOutboxRow)
                .where(SkillIndexOutboxRow.projection_key == f"public:{skill.id}")
                .order_by(SkillIndexOutboxRow.created_at.desc())
            )
            assert latest is not None
            assert latest.op == "upsert_active"
            assert latest.payload_json["enabled"] is False

        await harness.store.clear_public_active(skill.id)
        assert await harness.store.visible_for_user("u2") == []
        async with harness.session_factory() as session:
            assert await session.get(SkillPublicActiveRow, skill.id) is None
            settings = await session.get(SkillSettingsRow, skill.id)
            assert settings is not None
            latest = await session.scalar(
                select(SkillIndexOutboxRow)
                .where(SkillIndexOutboxRow.projection_key == f"public:{skill.id}")
                .order_by(SkillIndexOutboxRow.created_at.desc())
            )
            assert latest is not None and latest.op == "delete_active"
            assert latest.version_id is None

        await harness.store.audit(
            actor="admin",
            action="skill.manual",
            skill_id=skill.id,
            detail={"note": "kept"},
        )
        async with harness.session_factory() as session:
            event = await session.scalar(
                select(SkillAuditEventRow).where(
                    SkillAuditEventRow.action == "skill.manual"
                )
            )
            assert event is not None and event.detail_json == {"note": "kept"}

    harness.run(scenario)


def test_counts_are_visible_for_transaction_assertions(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await create_owned(harness, owner="u1", name="pdf-tools")
        async with harness.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(SkillIndexOutboxRow)) == 1
            assert await session.scalar(select(func.count()).select_from(SkillAuditEventRow)) == 1
            active = await session.get(SkillPrivateActiveRow, skill.id)
            assert active is not None and active.version_id == version.id

    harness.run(scenario)


def test_deleting_the_private_draft_keeps_the_public_release_visible(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version = await create_owned(harness, owner="u1", name="pdf-tools")
        await approve(harness, owner="u1", skill=skill, version=version)

        assert await harness.store.delete_private_draft(
            skill_id=skill.id, owner_user_id="u1", actor="u1"
        )

        # 删除的是自己的私有草稿指针；公共版本对所有人都还在（§2.2）。
        active = await harness.store.all_active()
        assert [item.skill.skill_id for item in active if item.scope is SkillScope.PUBLIC] == [
            skill.id
        ]
        assert [item for item in active if item.scope is SkillScope.PRIVATE] == []

        for user in ("u1", "u2"):
            visible = await harness.store.visible_for_user(user)
            assert [item.scope for item in visible] == [SkillScope.PUBLIC]
            assert visible[0].release is not None

        # owner 的私有 active 指针已删，私有名唯一性不再拦着它。
        assert await harness.store.private_skill_ids("u1") == ()

        # 检索确认与快照一致：公共命中还在，私有命中随草稿消失。
        confirmed = await harness.store.confirm_hits(
            "u2",
            [
                ProjectionHit(
                    projection_key=f"public:{skill.id}",
                    scope="public",
                    owner_user_id=None,
                    skill_id=skill.id,
                    version_id=version.id,
                    score=1.0,
                )
            ],
        )
        assert [item.skill.skill_id for item in confirmed] == [skill.id]
        assert (
            await harness.store.confirm_hits(
                "u1",
                [
                    ProjectionHit(
                        projection_key=f"private:u1:{skill.id}",
                        scope="private",
                        owner_user_id="u1",
                        skill_id=skill.id,
                        version_id=version.id,
                        score=1.0,
                    )
                ],
            )
            == []
        )

        # 投影重读（outbox 的 `get_active_visible`）也必须还能看到公共指针：
        # 否则删除草稿后的一次公共重投影会判定"公共行已下架"，把 Milvus 里的
        # 公共行删掉，所有人按向量检索都找不到它。
        public_target = await harness.store.get_active_visible(
            scope="public", skill_id=skill.id
        )
        assert public_target is not None
        assert public_target.release is not None
        assert public_target.version.id == version.id
        # 私有指针已经不存在了：重投影该删的就是它。
        assert (
            await harness.store.get_active_visible(
                scope="private", skill_id=skill.id, owner_user_id="u1"
            )
            is None
        )

        # 默认仍是草稿级语义：软删的记录不能编辑/保存；发布级调用方
        # 必须显式 include_deleted=True 才能看见它。
        assert await harness.store.get_skill(skill.id) is None
        published = await harness.store.get_skill(skill.id, include_deleted=True)
        assert published is not None and published.id == skill.id
        async with harness.session_factory() as session:
            row = await session.get(SkillRow, skill.id)
            assert row is not None and row.deleted_at is not None

    harness.run(scenario)
