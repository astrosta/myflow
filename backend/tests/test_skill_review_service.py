"""Task 7: admin review and public-release orchestration."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.config.skills_config import (
    SkillCreationConfig,
    SkillGitConfig,
    SkillReviewConfig,
)
from agentkit.persistence.schema import (
    Base,
    SkillAuditEventRow,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillPublicActiveRow,
    SkillReleaseRow,
    SkillRow,
    SkillSettingsRow,
    SkillSubmissionRow,
)
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SkillConflict,
    StagedFile,
)
from agentkit.skills.git import GitCommit
from agentkit.skills.package import render_skill_md
from agentkit.skills.review import SkillReviewService
from agentkit.skills.store.sql import SqlSkillStore

T = TypeVar("T")


class FakeContent:
    """Review reads immutable commits directly; no active-tree cache is needed."""

    async def ensure(self, commit: str) -> Path:  # pragma: no cover - defensive
        raise AssertionError(f"unexpected content.ensure({commit!r})")


@dataclass
class PublishCall:
    source_commit: str
    public_name: str
    source_path: str
    message: str
    commit: str


@dataclass
class FakeGit:
    commits: dict[str, dict[str, StagedFile]] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)
    save_calls: list[tuple[str, str, list[StagedFile], str]] = field(default_factory=list)
    publish_calls: list[PublishCall] = field(default_factory=list)
    config: SkillGitConfig = field(
        default_factory=lambda: SkillGitConfig(
            repository="https://example.invalid/skills.git", public_branch="main"
        )
    )
    _counter: int = 0

    def _new_commit(self, files: dict[str, StagedFile]) -> str:
        self._counter += 1
        commit = f"{self._counter:040x}"
        self.commits[commit] = dict(files)
        return commit

    async def save_draft(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        files: Sequence[StagedFile],
        message: str,
    ) -> GitCommit:
        copied = [StagedFile(item.path, item.content, item.mode) for item in files]
        self.save_calls.append((owner_user_id, skill_id, copied, message))
        ref = f"refs/heads/myflow/drafts/{owner_user_id}"
        path = f"users/{owner_user_id}/{skill_id}"
        committed = {
            f"{path}/{item.path}": StagedFile(
                f"{path}/{item.path}", item.content, item.mode
            )
            for item in copied
        }
        combined = dict(self.commits.get(self.heads.get(ref, ""), {}))
        combined.update(committed)
        commit = self._new_commit(combined)
        self.heads[ref] = commit
        return GitCommit(commit=commit, ref=ref, path=path)

    async def publish_public(
        self,
        *,
        public_name: str,
        source_commit: str,
        source_path: str,
        message: str,
    ) -> GitCommit:
        directory = (
            source_path[: -len("/SKILL.md")]
            if source_path.endswith("/SKILL.md")
            else source_path.rstrip("/")
        )
        prefix = directory + "/"
        source = self.commits[source_commit]
        public = {
            f"public/{public_name}/{path[len(prefix):]}": StagedFile(
                f"public/{public_name}/{path[len(prefix):]}",
                item.content,
                item.mode,
            )
            for path, item in source.items()
            if path.startswith(prefix)
        }
        if not public:
            raise InvalidSkillPackage(f"source tree has no files: {source_commit}:{directory}")
        commit = self._new_commit(public)
        ref = f"refs/heads/{self.config.public_branch}"
        self.heads[ref] = commit
        self.publish_calls.append(
            PublishCall(
                source_commit=source_commit,
                public_name=public_name,
                source_path=source_path,
                message=message,
                commit=commit,
            )
        )
        return GitCommit(commit=commit, ref=ref, path=f"public/{public_name}")

    async def head(self, ref: str) -> str:
        return self.heads[ref]

    async def list_tree(self, commit: str, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            key[len(prefix):]
            for key in self.commits[commit]
            if key.startswith(prefix)
        )

    async def read_file(self, commit: str, path: str) -> bytes:
        return self.commits[commit][path].content

    async def package_sha256(self, commit: str, path: str) -> str:
        suffix = "/SKILL.md"
        directory = path[: -len(suffix)] if path.endswith(suffix) else path.rstrip("/")
        prefix = directory + "/"
        relative = {
            key[len(prefix):]: staged
            for key, staged in self.commits[commit].items()
            if key.startswith(prefix)
        }
        if not relative:
            raise InvalidSkillPackage(f"source tree has no files: {commit}:{directory}")
        if "SKILL.md" not in relative:
            raise InvalidSkillPackage("package is missing SKILL.md")
        hasher = hashlib.sha256()
        for rel in sorted(relative):
            staged = relative[rel]
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(staged.mode.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(staged.content)
            hasher.update(b"\x00")
        return hasher.hexdigest()

    def rewrite_commit(self, commit: str, path: str, content: bytes) -> None:
        """Model a corrupted or force-updated immutable review object."""

        current = dict(self.commits[commit])
        current[path] = StagedFile(path, content, current[path].mode)
        self.commits[commit] = current


@dataclass
class Harness:
    store: SqlSkillStore
    author: SkillAuthorService
    review: SkillReviewService
    git: FakeGit
    session_factory: async_sessionmaker[AsyncSession]
    creation: SkillCreationConfig
    review_config: SkillReviewConfig

    def run(self, scenario: Callable[[], Awaitable[T]]) -> T:
        return asyncio.run(scenario())


@pytest.fixture(name="harness")
def harness_fixture(tmp_path: Path):
    creation = SkillCreationConfig(
        max_drafts_per_user=5,
        max_pending_submissions_per_user=5,
        max_package_bytes=16_384,
        max_file_bytes=4_096,
        max_files=20,
    )
    review_config = SkillReviewConfig(allow_self_approval=True)

    async def _build() -> tuple[Harness, object]:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "skills.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        store = SqlSkillStore(factory)
        git = FakeGit()
        author = SkillAuthorService(store, git, FakeContent(), creation)  # type: ignore[arg-type]
        review = SkillReviewService(
            store,
            git,  # type: ignore[arg-type]
            FakeContent(),  # type: ignore[arg-type]
            review_config,
        )
        return (
            Harness(store, author, review, git, factory, creation, review_config),
            engine,
        )

    built, engine = asyncio.run(_build())
    yield built
    asyncio.run(engine.dispose())


def skill_md(name: str, body: str = "Body\n") -> bytes:
    return (render_skill_md(name, "Does things") + "\n" + body).encode()


async def _submitted(
    harness: Harness,
    *,
    owner_user_id: str = "u1",
    name: str = "pdf-tools",
    extra_files: Sequence[tuple[str, bytes, str]] = (),
):
    skill = await harness.author.create_draft(
        owner_user_id=owner_user_id, name=name, description="Does things"
    )
    await harness.author.put_file(
        owner_user_id=owner_user_id,
        skill_id=skill.id,
        path="SKILL.md",
        content=skill_md(name),
    )
    for path, content, mode in extra_files:
        await harness.author.put_file(
            owner_user_id=owner_user_id,
            skill_id=skill.id,
            path=path,
            content=content,
            mode=mode,
        )
    version = await harness.author.save(owner_user_id=owner_user_id, skill_id=skill.id)
    submission = await harness.author.submit(
        owner_user_id=owner_user_id, skill_id=skill.id
    )
    return skill, version, submission


async def _outbox(harness: Harness, skill_id: str, op: str):
    async with harness.session_factory() as session:
        return (
            await session.scalars(
                select(SkillIndexOutboxRow)
                .where(
                    SkillIndexOutboxRow.skill_id == skill_id,
                    SkillIndexOutboxRow.op == op,
                )
                .order_by(SkillIndexOutboxRow.created_at.asc())
            )
        ).all()


def test_approve_publishes_the_exact_draft_tree_and_records_one_release(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill, version, submission = await _submitted(
            harness,
            extra_files=[("scripts/run.sh", b"echo ok\n", "executable")],
        )

        release = await harness.review.approve(
            admin_user_id="admin", submission_id=submission.id
        )

        assert len(harness.git.publish_calls) == 1
        call = harness.git.publish_calls[0]
        assert call.source_commit == submission.submitted_commit
        assert call.public_name == "pdf-tools"
        assert call.source_path == version.skill_path
        assert call.message
        assert release.git_commit == call.commit
        assert release.source_commit == submission.submitted_commit
        assert release.skill_path == "public/pdf-tools/SKILL.md"
        assert release.public_name == "pdf-tools"
        assert release.approved_by == "admin"
        assert release.skill_version == version.skill_version

        tree = harness.git.commits[release.git_commit]
        assert tree["public/pdf-tools/SKILL.md"].content == skill_md("pdf-tools")
        assert tree["public/pdf-tools/scripts/run.sh"].content == b"echo ok\n"
        assert tree["public/pdf-tools/scripts/run.sh"].mode == "executable"

        async with harness.session_factory() as session:
            submission_row = await session.get(SkillSubmissionRow, submission.id)
            assert submission_row is not None
            assert submission_row.status == "approved"
            assert submission_row.reviewed_by == "admin"
            assert submission_row.public_release_id == release.id
            active = await session.get(SkillPublicActiveRow, skill.id)
            assert active is not None and active.release_id == release.id
            settings = await session.get(SkillSettingsRow, skill.id)
            assert settings is not None and settings.enabled is True
            skill_row = await session.get(SkillRow, skill.id)
            assert skill_row is not None and skill_row.public_name == "pdf-tools"
            assert await session.get(SkillPrivateActiveRow, skill.id) is not None

        public_events = await _outbox(harness, skill.id, "upsert_active")
        public_events = [
            event for event in public_events if event.scope == "public"
        ]
        assert len(public_events) == 1
        assert public_events[0].projection_key == f"public:{skill.id}"
        assert public_events[0].version_id == version.id

        async with harness.session_factory() as session:
            audit = await session.scalar(
                select(SkillAuditEventRow).where(
                    SkillAuditEventRow.skill_id == skill.id,
                    SkillAuditEventRow.action == "skill.approve",
                )
            )
            assert audit is not None and audit.actor == "admin"

    harness.run(scenario)


def test_approve_rejects_a_changed_package_hash(harness: Harness) -> None:
    async def scenario() -> None:
        skill, _version, submission = await _submitted(
            harness, extra_files=[("scripts/run.sh", b"echo ok\n", "executable")]
        )
        harness.git.rewrite_commit(
            submission.submitted_commit,
            f"users/u1/{skill.id}/scripts/run.sh",
            b"echo evil\n",
        )

        with pytest.raises(SkillConflict):
            await harness.review.approve(
                admin_user_id="admin", submission_id=submission.id
            )

        assert harness.git.publish_calls == []
        async with harness.session_factory() as session:
            row = await session.get(SkillSubmissionRow, submission.id)
            assert row is not None and row.status == "pending"
            assert await session.get(SkillPublicActiveRow, skill.id) is None

    harness.run(scenario)


def test_approve_rejects_a_public_name_collision(harness: Harness) -> None:
    async def scenario() -> None:
        _first, _first_version, first_submission = await _submitted(
            harness, owner_user_id="u1", name="pdf-tools"
        )
        _second, _second_version, second_submission = await _submitted(
            harness, owner_user_id="u2", name="pdf-tools"
        )
        await harness.review.approve(
            admin_user_id="admin", submission_id=first_submission.id
        )

        with pytest.raises(SkillConflict):
            await harness.review.approve(
                admin_user_id="admin", submission_id=second_submission.id
            )

        assert len(harness.git.publish_calls) == 1
        async with harness.session_factory() as session:
            row = await session.get(SkillSubmissionRow, second_submission.id)
            assert row is not None and row.status == "pending"

    harness.run(scenario)


def test_approve_rejects_self_approval_when_disabled(harness: Harness) -> None:
    async def scenario() -> None:
        harness.review_config.allow_self_approval = False
        _skill, _version, submission = await _submitted(harness, owner_user_id="u1")

        with pytest.raises(SkillConflict):
            await harness.review.approve(
                admin_user_id="u1", submission_id=submission.id
            )

        assert harness.git.publish_calls == []

    harness.run(scenario)


def test_approve_reuses_the_immutable_public_name(harness: Harness) -> None:
    async def scenario() -> None:
        skill, _version, first_submission = await _submitted(harness, name="pdf-tools")
        await harness.review.approve(
            admin_user_id="admin", submission_id=first_submission.id
        )

        async with harness.session_factory() as session:
            row = await session.get(SkillRow, skill.id)
            assert row is not None
            row.private_name = "renamed-tools"
            await session.commit()

        await harness.author.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=skill_md("pdf-tools", "Second\n"),
        )
        await harness.author.save(owner_user_id="u1", skill_id=skill.id)
        second_submission = await harness.author.submit(
            owner_user_id="u1", skill_id=skill.id
        )
        await harness.review.approve(
            admin_user_id="admin", submission_id=second_submission.id
        )

        assert [call.public_name for call in harness.git.publish_calls] == [
            "pdf-tools",
            "pdf-tools",
        ]

    harness.run(scenario)


def test_reject_keeps_the_private_pointer_and_audits(harness: Harness) -> None:
    async def scenario() -> None:
        skill, version, submission = await _submitted(harness)

        rejected = await harness.review.reject(
            admin_user_id="admin",
            submission_id=submission.id,
            reason="needs a clearer procedure",
        )

        assert rejected.status == "rejected"
        assert rejected.reject_reason == "needs a clearer procedure"
        assert rejected.reviewed_by == "admin"
        assert harness.git.publish_calls == []
        async with harness.session_factory() as session:
            active = await session.get(SkillPrivateActiveRow, skill.id)
            assert active is not None and active.version_id == version.id
            assert await session.get(SkillPublicActiveRow, skill.id) is None
            audit = await session.scalar(
                select(SkillAuditEventRow).where(
                    SkillAuditEventRow.skill_id == skill.id,
                    SkillAuditEventRow.action == "skill.reject",
                )
            )
            assert audit is not None and audit.actor == "admin"

    harness.run(scenario)


def test_unpublish_removes_public_active_but_keeps_release_rows(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill, _version, submission = await _submitted(harness)
        release = await harness.review.approve(
            admin_user_id="admin", submission_id=submission.id
        )

        await harness.review.unpublish(admin_user_id="admin", skill_id=skill.id)

        async with harness.session_factory() as session:
            assert await session.get(SkillPublicActiveRow, skill.id) is None
            assert await session.get(SkillReleaseRow, release.id) is not None
        delete_events = await _outbox(harness, skill.id, "delete_active")
        delete_events = [
            event for event in delete_events if event.scope == "public"
        ]
        assert len(delete_events) == 1
        assert delete_events[0].payload_json == {}
        async with harness.session_factory() as session:
            audit = await session.scalar(
                select(SkillAuditEventRow)
                .where(
                    SkillAuditEventRow.skill_id == skill.id,
                    SkillAuditEventRow.action == "skill.unpublish",
                )
                .order_by(SkillAuditEventRow.created_at.desc())
            )
            assert audit is not None and audit.actor == "admin"

    harness.run(scenario)


def test_unpublish_succeeds_after_the_author_deleted_the_private_draft(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill, _version, submission = await _submitted(harness)
        await harness.review.approve(
            admin_user_id="admin", submission_id=submission.id
        )
        await harness.author.delete_draft(owner_user_id="u1", skill_id=skill.id)

        # 公共版本还在，管理员必须还能下架；不能因为私有记录是软删就 409。
        await harness.review.unpublish(admin_user_id="admin", skill_id=skill.id)

        assert [
            item
            for item in await harness.store.all_active()
            if item.scope.value == "public" and item.skill.skill_id == skill.id
        ] == []
        assert await harness.store.get_skill(skill.id) is None

    harness.run(scenario)


def test_open_draft_revives_a_soft_deleted_skill_and_save_works(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill, _version, submission = await _submitted(harness)
        await harness.review.approve(
            admin_user_id="admin", submission_id=submission.id
        )
        await harness.author.delete_draft(owner_user_id="u1", skill_id=skill.id)
        assert await harness.store.get_skill(skill.id) is None

        files = await harness.author.open_draft(
            owner_user_id="u1", skill_id=skill.id
        )
        assert [file.path for file in files] == ["SKILL.md"]
        revived = await harness.store.get_skill(skill.id)
        assert revived is not None and revived.id == skill.id
        async with harness.session_factory() as session:
            row = await session.get(SkillRow, skill.id)
            assert row is not None and row.deleted_at is None

        saved = await harness.author.save(owner_user_id="u1", skill_id=skill.id)
        assert saved.skill_id == skill.id

    harness.run(scenario)


def test_activate_rolls_back_to_an_older_release_without_committing_git(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill, first_version, first_submission = await _submitted(harness)
        first_release = await harness.review.approve(
            admin_user_id="admin", submission_id=first_submission.id
        )

        await harness.author.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=skill_md("pdf-tools", "Second\n"),
        )
        second_version = await harness.author.save(
            owner_user_id="u1", skill_id=skill.id
        )
        second_submission = await harness.author.submit(
            owner_user_id="u1", skill_id=skill.id
        )
        second_release = await harness.review.approve(
            admin_user_id="admin", submission_id=second_submission.id
        )
        publishes_before = len(harness.git.publish_calls)

        await harness.review.activate(
            admin_user_id="admin",
            skill_id=skill.id,
            release_id=first_release.id,
        )

        assert len(harness.git.publish_calls) == publishes_before
        async with harness.session_factory() as session:
            active = await session.get(SkillPublicActiveRow, skill.id)
            assert active is not None and active.release_id == first_release.id
            assert await session.get(SkillReleaseRow, first_release.id) is not None
            assert await session.get(SkillReleaseRow, second_release.id) is not None
        assert first_version.id != second_version.id
        events = [
            event
            for event in await _outbox(harness, skill.id, "upsert_active")
            if event.scope == "public"
        ]
        assert events[-1].version_id == first_version.id

    harness.run(scenario)


def test_only_pending_submissions_can_be_reviewed(harness: Harness) -> None:
    async def scenario() -> None:
        _skill, _version, submission = await _submitted(harness)
        await harness.review.approve(
            admin_user_id="admin", submission_id=submission.id
        )

        with pytest.raises(SkillConflict):
            await harness.review.approve(
                admin_user_id="admin", submission_id=submission.id
            )
        with pytest.raises(SkillConflict):
            await harness.review.reject(
                admin_user_id="admin",
                submission_id=submission.id,
                reason="too late",
            )

    harness.run(scenario)


def test_release_failure_after_git_leaves_the_submission_retryable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        _skill, _version, submission = await _submitted(harness)

        async def fail_release(**_kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(harness.store, "record_public_release", fail_release)

        with pytest.raises(RuntimeError, match="database unavailable"):
            await harness.review.approve(
                admin_user_id="admin", submission_id=submission.id
            )

        assert len(harness.git.publish_calls) == 1
        assert await harness.store.pending_submission(submission.skill_id) is not None

    harness.run(scenario)


def test_list_pending_and_detail_are_backed_by_the_submitted_commit(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill, version, submission = await _submitted(
            harness,
            extra_files=[("references/notes.md", b"notes\n", "text")],
        )

        pending = await harness.review.list_pending()
        assert [item.submission_id for item in pending] == [submission.id]
        assert pending[0].skill_id == skill.id
        assert pending[0].name == "pdf-tools"
        assert pending[0].version == version.skill_version
        assert pending[0].submitted_commit == submission.submitted_commit

        detail = await harness.review.detail(submission.id)
        assert detail.item == pending[0]
        assert [file.path for file in detail.files] == [
            "SKILL.md",
            "references/notes.md",
        ]
        assert detail.package_bytes == len(skill_md("pdf-tools")) + len(b"notes\n")
        assert detail.files[0].sha256 == hashlib.sha256(skill_md("pdf-tools")).hexdigest()
        assert detail.files[0].size_bytes == len(skill_md("pdf-tools"))
        assert detail.skill_md.startswith("---")
        assert "pdf-tools" in detail.skill_md
        assert "SKILL.md" in detail.diff
        assert "+notes" in detail.diff
        assert detail.metadata["name"] == "pdf-tools"

    harness.run(scenario)
