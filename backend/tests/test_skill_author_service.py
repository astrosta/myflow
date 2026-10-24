"""Task 6: the author-facing orchestration for private drafts."""

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

from agentkit.config.skills_config import SkillCreationConfig
from agentkit.persistence.schema import (
    Base,
    SkillAuditEventRow,
    SkillDraftFileRow,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillPublicActiveRow,
    SkillReleaseRow,
    SkillRow,
)
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SkillConflict,
    StagedFile,
    StagedFileInfo,
)
from agentkit.skills.git import GitCommit
from agentkit.skills.package import render_skill_md
from agentkit.skills.store.sql import SqlSkillStore

T = TypeVar("T")


class FakeContent:
    """The author service under test does not need a materialized cache."""

    async def ensure(self, commit: str) -> Path:  # pragma: no cover - defensive
        raise AssertionError(f"unexpected content.ensure({commit!r})")


@dataclass
class FakeGit:
    commits: dict[str, dict[str, StagedFile]] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)
    save_calls: list[tuple[str, str, list[StagedFile], str]] = field(default_factory=list)
    fail_save: bool = False
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
        if self.fail_save:
            raise RuntimeError("git failed")
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
        # A real draft ref keeps every skill directory on the owner branch.
        combined = dict(self.commits.get(self.heads.get(ref, ""), {}))
        combined.update(committed)
        commit = self._new_commit(combined)
        self.heads[ref] = commit
        return GitCommit(commit=commit, ref=ref, path=path)

    async def head(self, ref: str) -> str:
        return self.heads[ref]

    async def list_tree(self, commit: str, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            key[len(prefix) :]
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
            key[len(prefix) :]: staged
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

    def rewrite_skill(self, ref: str, content: bytes) -> None:
        current = dict(self.commits[self.heads[ref]])
        path = next(key for key in current if key.endswith("/SKILL.md"))
        current[path] = StagedFile(path, content, "text")
        self.heads[ref] = self._new_commit(current)

    def rewrite_file(
        self, ref: str, path: str, content: bytes, mode: str = "text"
    ) -> None:
        current = dict(self.commits[self.heads[ref]])
        current[path] = StagedFile(path, content, mode)
        self.heads[ref] = self._new_commit(current)

    def publish(self, source_commit: str, public_name: str) -> str:
        source = self.commits[source_commit]
        source_dir = next(iter(source)).rsplit("/", 1)[0]
        public = {
            f"public/{public_name}/{path[len(source_dir) + 1 :]}": StagedFile(
                f"public/{public_name}/{path[len(source_dir) + 1 :]}",
                item.content,
                item.mode,
            )
            for path, item in source.items()
        }
        return self._new_commit(public)


@dataclass
class Harness:
    store: SqlSkillStore
    service: SkillAuthorService
    git: FakeGit
    session_factory: async_sessionmaker[AsyncSession]
    creation: SkillCreationConfig

    def run(self, scenario: Callable[[], Awaitable[T]]) -> T:
        return asyncio.run(scenario())


@pytest.fixture(name="harness")
def harness_fixture(tmp_path: Path):
    creation = SkillCreationConfig(
        max_drafts_per_user=5,
        max_pending_submissions_per_user=2,
        max_package_bytes=16_384,
        max_file_bytes=4_096,
        max_files=20,
    )

    async def _build() -> tuple[Harness, object]:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "skills.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        store = SqlSkillStore(factory)
        git = FakeGit()
        service = SkillAuthorService(store, git, FakeContent(), creation)  # type: ignore[arg-type]
        return Harness(store, service, git, factory, creation), engine

    built, engine = asyncio.run(_build())
    yield built
    asyncio.run(engine.dispose())


def skill_md(name: str, body: str = "Body\n") -> bytes:
    return (render_skill_md(name, "Does things") + "\n" + body).encode()


async def _approve(
    harness: Harness,
    *,
    submission,
    public_name: str,
    actor: str = "admin",
):
    public_commit = harness.git.publish(submission.submitted_commit, public_name)
    return await harness.store.record_public_release(
        submission=submission,
        public_name=public_name,
        git_repository="https://example.invalid/skills.git",
        git_commit=public_commit,
        skill_path=f"public/{public_name}/SKILL.md",
        source_commit=submission.submitted_commit,
        actor=actor,
    )


def test_create_draft_stages_initial_skill_md_without_commit(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        assert skill.owner_user_id == "u1"
        assert harness.git.save_calls == []

        files = await harness.service.list_files(owner_user_id="u1", skill_id=skill.id)
        assert [file.path for file in files] == ["SKILL.md"]
        assert all(isinstance(file, StagedFileInfo) for file in files)
        expected = render_skill_md("pdf-tools", "Does things").encode()
        assert files[0].sha256 == hashlib.sha256(expected).hexdigest()

        async with harness.session_factory() as session:
            row = await session.get(SkillDraftFileRow, (skill.id, "SKILL.md"))
            assert row is not None and row.content == expected

    harness.run(scenario)


def test_save_reuses_identical_version_and_bumps_changed_version(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        v1 = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert v1.skill_version == "v1"
        assert v1.draft_commit == await harness.git.head(v1.draft_ref or "")
        assert v1.skill_path == f"users/u1/{skill.id}/SKILL.md"

        same = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert same.id == v1.id
        assert same.skill_version == "v1"
        assert [item.id for item in await harness.service.list_versions(
            owner_user_id="u1", skill_id=skill.id
        )] == [v1.id]

        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=skill_md("pdf-tools", "Changed\n"),
        )
        v2 = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert v2.skill_version == "v2"
        assert v2.id != v1.id
        versions = await harness.service.list_versions(
            owner_user_id="u1", skill_id=skill.id
        )
        assert [item.skill_version for item in versions] == ["v1", "v2"]

    harness.run(scenario)


def test_save_writes_private_active_and_outbox(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)

        async with harness.session_factory() as session:
            active = await session.get(SkillPrivateActiveRow, skill.id)
            assert active is not None
            assert active.owner_user_id == "u1"
            assert active.version_id == version.id
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


def test_pending_submission_blocks_staging_and_save(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        await harness.service.submit(owner_user_id="u1", skill_id=skill.id)

        with pytest.raises(SkillConflict):
            await harness.service.put_file(
                owner_user_id="u1",
                skill_id=skill.id,
                path="SKILL.md",
                content=skill_md("pdf-tools", "x\n"),
            )
        with pytest.raises(SkillConflict):
            await harness.service.delete_file(
                owner_user_id="u1", skill_id=skill.id, path="SKILL.md"
            )
        with pytest.raises(SkillConflict):
            await harness.service.save(owner_user_id="u1", skill_id=skill.id)

    harness.run(scenario)


def test_submit_uses_the_saved_version_commit(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="scripts/run.sh",
            content=b"echo ok\n",
            mode="executable",
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        submission = await harness.service.submit(
            owner_user_id="u1", skill_id=skill.id
        )
        assert submission.status == "pending"
        assert submission.version_id == version.id
        assert submission.submitted_commit == version.draft_commit
        assert submission.owner_user_id == "u1"
        assert await harness.store.pending_submission(skill.id) is not None

    harness.run(scenario)


def test_submit_rejects_changed_git_head(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        harness.git.rewrite_skill(version.draft_ref or "", skill_md("pdf-tools", "evil\n"))

        with pytest.raises(SkillConflict):
            await harness.service.submit(owner_user_id="u1", skill_id=skill.id)
        assert await harness.store.pending_submission(skill.id) is None

    harness.run(scenario)


def test_submit_rejects_changed_attachment_at_git_head(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="scripts/run.sh",
            content=b"echo ok\n",
            mode="executable",
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        harness.git.rewrite_file(
            version.draft_ref or "",
            f"users/u1/{skill.id}/scripts/run.sh",
            b"echo evil\n",
            mode="executable",
        )

        with pytest.raises(SkillConflict):
            await harness.service.submit(owner_user_id="u1", skill_id=skill.id)
        assert await harness.store.pending_submission(skill.id) is None

    harness.run(scenario)


def test_withdraw_releases_the_pending_guard(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        await harness.service.submit(owner_user_id="u1", skill_id=skill.id)
        withdrawn = await harness.service.withdraw(
            owner_user_id="u1", skill_id=skill.id
        )
        assert withdrawn.status == "withdrawn"
        assert await harness.store.pending_submission(skill.id) is None
        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=skill_md("pdf-tools", "again\n"),
        )

    harness.run(scenario)


def test_delete_draft_keeps_public_release(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        submission = await harness.service.submit(
            owner_user_id="u1", skill_id=skill.id
        )
        release = await _approve(
            harness, submission=submission, public_name="pdf-tools"
        )

        async with harness.session_factory() as session:
            assert await session.get(SkillPrivateActiveRow, skill.id) is not None
            assert await session.get(SkillPublicActiveRow, skill.id) is not None

        await harness.service.delete_draft(owner_user_id="u1", skill_id=skill.id)
        assert await harness.store.list_draft_files(skill.id) == []
        with pytest.raises(SkillConflict):
            await harness.service.list_files(owner_user_id="u1", skill_id=skill.id)
        async with harness.session_factory() as session:
            assert await session.get(SkillPrivateActiveRow, skill.id) is None
            assert await session.get(SkillPublicActiveRow, skill.id) is not None
            assert await session.get(SkillReleaseRow, release.id) is not None
            skill_row = await session.get(SkillRow, skill.id)
            assert skill_row is not None and skill_row.deleted_at is not None
        public = [
            item
            for item in await harness.store.visible_for_user("u1")
            if item.skill.skill_id == skill.id
        ]
        assert [item.scope for item in public] == ["public"]

    harness.run(scenario)


def test_delete_draft_enqueues_private_delete_outbox(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        submission = await harness.service.submit(
            owner_user_id="u1", skill_id=skill.id
        )
        release = await _approve(
            harness, submission=submission, public_name="pdf-tools"
        )

        await harness.service.delete_draft(owner_user_id="u1", skill_id=skill.id)

        async with harness.session_factory() as session:
            assert await session.get(SkillPrivateActiveRow, skill.id) is None
            assert await session.get(SkillPublicActiveRow, skill.id) is not None
            assert await session.get(SkillReleaseRow, release.id) is not None
            rows = (
                await session.scalars(
                    select(SkillDraftFileRow).where(
                        SkillDraftFileRow.skill_id == skill.id
                    )
                )
            ).all()
            assert rows == []
            delete_events = (
                await session.scalars(
                    select(SkillIndexOutboxRow).where(
                        SkillIndexOutboxRow.projection_key
                        == f"private:u1:{skill.id}",
                        SkillIndexOutboxRow.op == "delete_active",
                    )
                )
            ).all()
            assert len(delete_events) == 1
            event = delete_events[0]
            assert event.scope == "private"
            assert event.owner_user_id == "u1"
            assert event.skill_id == skill.id
            assert event.version_id is None
            assert event.payload_json == {}
            audit = await session.scalar(
                select(SkillAuditEventRow).where(
                    SkillAuditEventRow.skill_id == skill.id,
                    SkillAuditEventRow.action == "skill.delete",
                )
            )
            assert audit is not None and audit.actor == "u1"

        public = [
            item
            for item in await harness.store.visible_for_user("u1")
            if item.skill.skill_id == skill.id
        ]
        assert [item.scope for item in public] == ["public"]

    harness.run(scenario)


def test_open_draft_seeds_from_owned_public_release(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="scripts/run.sh",
            content=b"echo ok\n",
            mode="executable",
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        submission = await harness.service.submit(
            owner_user_id="u1", skill_id=skill.id
        )
        await _approve(harness, submission=submission, public_name="pdf-tools")
        await harness.store.delete_all_draft_files(skill.id)

        files = await harness.service.open_draft(
            owner_user_id="u1", skill_id=skill.id
        )
        assert [file.path for file in files] == ["SKILL.md", "scripts/run.sh"]
        async with harness.session_factory() as session:
            row = await session.get(SkillDraftFileRow, (skill.id, "scripts/run.sh"))
            assert row is not None and row.content == b"echo ok\n"

    harness.run(scenario)


def test_only_the_owner_can_touch_a_draft(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        with pytest.raises(SkillConflict):
            await harness.service.list_files(owner_user_id="u2", skill_id=skill.id)
        with pytest.raises(SkillConflict):
            await harness.service.open_draft(owner_user_id="u2", skill_id=skill.id)
        with pytest.raises(SkillConflict):
            await harness.service.list_versions(owner_user_id="u2", skill_id=skill.id)
        with pytest.raises(SkillConflict):
            await harness.service.put_file(
                owner_user_id="u2",
                skill_id=skill.id,
                path="SKILL.md",
                content=skill_md("pdf-tools"),
            )
        with pytest.raises(SkillConflict):
            await harness.service.delete_draft(
                owner_user_id="u2", skill_id=skill.id
            )
        assert len(
            await harness.service.list_files(owner_user_id="u1", skill_id=skill.id)
        ) == 1

    harness.run(scenario)


def test_create_draft_enforces_quota_and_public_name(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        submission = await harness.service.submit(
            owner_user_id="u1", skill_id=skill.id
        )
        await _approve(harness, submission=submission, public_name="pdf-tools")

        with pytest.raises(SkillConflict):
            await harness.service.create_draft(
                owner_user_id="u1", name="pdf-tools", description="collision"
            )

        harness.creation.max_drafts_per_user = 1
        with pytest.raises(SkillConflict):
            await harness.service.create_draft(
                owner_user_id="u1", name="other-tools", description="quota"
            )

    harness.run(scenario)


def test_put_file_enforces_package_limits_before_staging(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        with pytest.raises(InvalidSkillPackage):
            await harness.service.put_file(
                owner_user_id="u1",
                skill_id=skill.id,
                path="big.bin",
                content=b"x" * (harness.creation.max_file_bytes + 1),
                mode="binary",
            )
        assert [
            file.path
            for file in await harness.service.list_files(
                owner_user_id="u1", skill_id=skill.id
            )
        ] == ["SKILL.md"]

    harness.run(scenario)


def test_git_failure_leaves_staging_and_versions_untouched(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        harness.git.fail_save = True
        with pytest.raises(RuntimeError):
            await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert await harness.service.list_versions(
            owner_user_id="u1", skill_id=skill.id
        ) == []
        assert [
            file.path
            for file in await harness.service.list_files(
                owner_user_id="u1", skill_id=skill.id
            )
        ] == ["SKILL.md"]

    harness.run(scenario)


def test_submit_enforces_pending_quota(harness: Harness) -> None:
    async def scenario() -> None:
        first = await harness.service.create_draft(
            owner_user_id="u1", name="first-tools", description="Does things"
        )
        second = await harness.service.create_draft(
            owner_user_id="u1", name="second-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=first.id)
        await harness.service.save(owner_user_id="u1", skill_id=second.id)
        harness.creation.max_pending_submissions_per_user = 1
        await harness.service.submit(owner_user_id="u1", skill_id=first.id)
        with pytest.raises(SkillConflict):
            await harness.service.submit(owner_user_id="u1", skill_id=second.id)

    harness.run(scenario)


def test_withdraw_requires_a_pending_submission(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        with pytest.raises(SkillConflict):
            await harness.service.withdraw(owner_user_id="u1", skill_id=skill.id)

    harness.run(scenario)


def test_open_draft_without_public_release_returns_empty(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.store.delete_all_draft_files(skill.id)
        assert await harness.service.open_draft(
            owner_user_id="u1", skill_id=skill.id
        ) == []

    harness.run(scenario)


def test_delete_draft_requires_no_pending_submission(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        await harness.service.submit(owner_user_id="u1", skill_id=skill.id)
        with pytest.raises(SkillConflict):
            await harness.service.delete_draft(
                owner_user_id="u1", skill_id=skill.id
            )
        assert len(
            await harness.service.list_files(owner_user_id="u1", skill_id=skill.id)
        ) == 1

    harness.run(scenario)
