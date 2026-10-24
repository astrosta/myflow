"""Admin review and public-release orchestration.

Git writes and catalog writes deliberately remain separate: approval first
publishes an immutable public commit, then one PostgreSQL transaction records
the release, the public active pointer, the settings switch, the projection
outbox event, and the audit row.  A failure after the Git commit therefore
leaves an unreferenced commit that a retry can safely replace; it never leaves
a half-approved catalog row.

Approval is serialized per public branch.  PostgreSQL sessions take a
session-level advisory lock on a dedicated connection, while SQLite tests use
an in-process lock and rely on SQLite's transactional write lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from difflib import unified_diff
from pathlib import PurePosixPath

from sqlalchemy import select, text

from agentkit.config.skills_config import SkillReviewConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import (
    ReviewDetail,
    ReviewItem,
    SkillConflict,
    SkillReleaseRecord,
    SkillScope,
    SkillSubmissionRecord,
    SkillVersionRecord,
    StagedFileInfo,
)
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.store.model import SkillRow
from agentkit.skills.store.sql import SqlSkillStore
from agentkit.skills.types import SKILL_FILE_NAME

logger = logging.getLogger(__name__)


class SkillReviewService:
    """List review work and decide public releases."""

    def __init__(
        self,
        store: SqlSkillStore,
        git: SkillGitRepository,
        content: SkillContentStore,
        review: SkillReviewConfig,
    ) -> None:
        self._store = store
        self._git = git
        self._content = content
        self._review = review
        self._local_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # review queue
    # ------------------------------------------------------------------

    async def list_pending(self, limit: int = 50) -> list[ReviewItem]:
        return await self._store.list_pending_submissions(limit)

    async def detail(self, submission_id: str) -> ReviewDetail:
        """Return the submitted commit's files, never the mutable staging area."""

        detail = await self._store.review_detail(submission_id)
        if detail is None:
            raise SkillConflict(f"submission {submission_id!r} does not exist")
        submission = await self._store.submission(submission_id)
        if submission is None:
            raise SkillConflict(f"submission {submission_id!r} does not exist")
        version = await self._version_for(submission)
        directory = self._skill_directory(version.skill_path)

        files: list[StagedFileInfo] = []
        contents: dict[str, bytes] = {}
        for relative in sorted(
            await self._git.list_tree(submission.submitted_commit, directory)
        ):
            path = f"{directory}/{relative}" if relative else directory
            data = await self._git.read_file(submission.submitted_commit, path)
            contents[relative] = data
            files.append(
                StagedFileInfo(
                    path=relative,
                    size_bytes=len(data),
                    mode="text",
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )

        diff = await self._diff(
            skill_id=submission.skill_id,
            owner_user_id=submission.owner_user_id,
            submitted_commit=submission.submitted_commit,
            contents=contents,
        )
        return replace(
            detail,
            files=files,
            skill_md=contents.get(SKILL_FILE_NAME, b"").decode("utf-8", "replace"),
            diff=diff,
            package_bytes=sum(file.size_bytes for file in files),
        )

    # ------------------------------------------------------------------
    # decisions
    # ------------------------------------------------------------------

    async def approve(
        self, *, admin_user_id: str, submission_id: str
    ) -> SkillReleaseRecord:
        async with self._public_branch_lock():
            submission = await self._pending_submission(submission_id)
            if (
                not self._review.allow_self_approval
                and admin_user_id == submission.owner_user_id
            ):
                raise SkillConflict("self-approval is disabled")

            skill = await self._store.get_skill(submission.skill_id)
            if skill is None:
                raise SkillConflict(f"skill {submission.skill_id!r} does not exist")
            version = await self._version_for(submission)

            actual_package = await self._git.package_sha256(
                submission.submitted_commit, version.skill_path
            )
            if actual_package != version.package_sha256:
                raise SkillConflict(
                    f"skill {skill.id!r} package changed since it was submitted"
                )

            # public_name is immutable after the first approval.  Appending a
            # new version can never rename the public entry.
            public_name = skill.public_name or skill.private_name
            await self._ensure_public_name_free(public_name, skill.id)

            public_commit = await self._git.publish_public(
                public_name=public_name,
                source_commit=submission.submitted_commit,
                source_path=version.skill_path,
                message=(
                    f"Approve {public_name} {version.skill_version} "
                    f"from {submission.submitted_commit}"
                ),
            )
            try:
                return await self._store.record_public_release(
                    submission=submission,
                    public_name=public_name,
                    git_repository=self._repository(),
                    git_commit=public_commit.commit,
                    skill_path=self._release_skill_path(public_commit.path),
                    source_commit=submission.submitted_commit,
                    actor=admin_user_id,
                )
            except Exception:
                # The Git commit is immutable and unreferenced; a retry creates
                # another commit.  Never claim success after a catalog failure.
                logger.exception(
                    "public release recording failed after Git commit %s",
                    public_commit.commit,
                )
                raise

    async def reject(
        self, *, admin_user_id: str, submission_id: str, reason: str
    ) -> SkillSubmissionRecord:
        await self._pending_submission(submission_id)
        return await self._store.set_submission_status(
            submission_id,
            status="rejected",
            reviewed_by=admin_user_id,
            reject_reason=reason,
        )

    async def unpublish(self, *, admin_user_id: str, skill_id: str) -> None:
        skill = await self._store.get_skill(skill_id)
        if skill is None:
            raise SkillConflict(f"skill {skill_id!r} does not exist")
        # clear_public_active has no actor parameter, so its transactional audit
        # row is attributed to "system"; append the reviewing admin explicitly.
        await self._store.clear_public_active(skill_id)
        await self._store.audit(
            actor=admin_user_id,
            action="skill.unpublish",
            skill_id=skill_id,
        )

    async def activate(
        self, *, admin_user_id: str, skill_id: str, release_id: str
    ) -> None:
        # The store verifies that release_id belongs to skill_id and emits both
        # the public upsert and the audit row.  This method never writes Git.
        await self._store.set_public_active(skill_id, release_id, admin_user_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _pending_submission(self, submission_id: str) -> SkillSubmissionRecord:
        submission = await self._store.submission(submission_id)
        if submission is None:
            raise SkillConflict(f"submission {submission_id!r} does not exist")
        if submission.status != "pending":
            raise SkillConflict(f"submission {submission_id!r} is not pending")
        return submission

    async def _version_for(
        self, submission: SkillSubmissionRecord
    ) -> SkillVersionRecord:
        for version in await self._store.list_versions(submission.skill_id):
            if version.id == submission.version_id:
                return version
        raise SkillConflict(
            f"submission {submission.id!r} has no version {submission.version_id!r}"
        )

    async def _ensure_public_name_free(self, public_name: str, skill_id: str) -> None:
        factory = getattr(self._store, "_sf", None)
        if factory is None:  # pragma: no cover - defensive for alternate stores
            return
        async with factory() as session:
            collision = await session.scalar(
                select(SkillRow.id)
                .where(
                    SkillRow.public_name == public_name,
                    SkillRow.id != skill_id,
                )
                .limit(1)
            )
            if collision is not None:
                raise SkillConflict(f"public name {public_name!r} is already in use")

    async def _diff(
        self,
        *,
        skill_id: str,
        owner_user_id: str,
        submitted_commit: str,
        contents: Mapping[str, bytes],
    ) -> str:
        previous: Mapping[str, bytes] = await self._previous_public_contents(
            skill_id=skill_id,
            owner_user_id=owner_user_id,
            submitted_commit=submitted_commit,
        )
        chunks: list[str] = []
        for path in sorted(set(previous) | set(contents)):
            before = previous.get(path, b"")
            after = contents.get(path, b"")
            if before == after:
                continue
            if b"\x00" in before or b"\x00" in after:
                chunks.append(f"Binary files a/{path} and b/{path} differ\n")
                continue
            chunks.extend(
                unified_diff(
                    before.decode("utf-8", "replace").splitlines(keepends=True),
                    after.decode("utf-8", "replace").splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        return "".join(chunks)

    async def _previous_public_contents(
        self, *, skill_id: str, owner_user_id: str, submitted_commit: str
    ) -> Mapping[str, bytes]:
        for visible in await self._store.visible_for_user(owner_user_id):
            if (
                visible.scope is not SkillScope.PUBLIC
                or visible.skill.skill_id != skill_id
                or visible.release is None
                or visible.release.git_commit == submitted_commit
            ):
                continue
            release = visible.release
            directory = self._skill_directory(release.skill_path)
            files: dict[str, bytes] = {}
            for relative in sorted(
                await self._git.list_tree(release.git_commit, directory)
            ):
                path = f"{directory}/{relative}" if relative else directory
                files[relative] = await self._git.read_file(release.git_commit, path)
            return files
        return {}

    @staticmethod
    def _skill_directory(raw_path: str) -> str:
        path = raw_path.rstrip("/")
        if PurePosixPath(path).name == SKILL_FILE_NAME:
            parent = str(PurePosixPath(path).parent)
            if parent in ("", "."):
                raise SkillConflict(f"invalid release skill path: {raw_path!r}")
            return parent
        return path

    def _repository(self) -> str:
        config = getattr(self._git, "config", None)
        repository = getattr(config, "repository", None)
        if not isinstance(repository, str) or not repository:
            repository = getattr(self._git, "repository", "")
        return repository if isinstance(repository, str) else ""

    def _public_branch(self) -> str:
        config = getattr(self._git, "config", None)
        branch = getattr(config, "public_branch", None)
        if not isinstance(branch, str) or not branch:
            branch = getattr(self._git, "public_branch", "main")
        return branch if isinstance(branch, str) and branch else "main"

    @staticmethod
    def _release_skill_path(commit_path: str) -> str:
        path = commit_path.rstrip("/")
        if PurePosixPath(path).name == SKILL_FILE_NAME:
            return path
        return f"{path}/{SKILL_FILE_NAME}"

    @staticmethod
    def _lock_key(public_branch: str) -> int:
        digest = hashlib.sha256(
            f"skill-review:{public_branch}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def _bind(self):
        factory = getattr(self._store, "_sf", None)
        kwargs = getattr(factory, "kw", None)
        if not isinstance(kwargs, dict):
            return None
        return kwargs.get("bind")

    async def _local_lock(self, public_branch: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._local_locks.get(public_branch)
            if lock is None:
                lock = asyncio.Lock()
                self._local_locks[public_branch] = lock
            return lock

    @asynccontextmanager
    async def _public_branch_lock(self) -> AsyncIterator[None]:
        """Serialize approvals for one public branch without a DB transaction."""

        branch = self._public_branch()
        async with await self._local_lock(branch):
            bind = self._bind()
            dialect = getattr(bind, "dialect", None)
            if dialect is None or dialect.name != "postgresql":
                yield
                return

            key = self._lock_key(branch)
            async with bind.connect() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_lock(CAST(:key AS bigint))"),
                    {"key": key},
                )
                # End the acquisition transaction; the session-level advisory
                # lock remains held on this dedicated connection.
                await connection.commit()
                try:
                    yield
                finally:
                    try:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                            {"key": key},
                        )
                        await connection.commit()
                    except Exception:  # pragma: no cover - released on close
                        logger.warning(
                            "could not release skill review advisory lock",
                            exc_info=True,
                        )


__all__ = ["SkillReviewService"]
