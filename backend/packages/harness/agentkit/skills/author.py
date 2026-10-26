"""Author-facing orchestration for private skill drafts.

The service deliberately keeps Git and SQL separate: Git commits happen before
the catalog transaction that records them, and a per-skill advisory lock keeps
two saves for the same skill from racing each other.  PostgreSQL uses a
session-level advisory lock on a dedicated connection; SQLite relies on its
transactional write lock plus an in-process lock for the same skill.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from sqlalchemy import func, select, text

from agentkit.config.skills_config import SkillCreationConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SkillConflict,
    SkillRecord,
    SkillReleaseRecord,
    SkillScope,
    SkillSubmissionRecord,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
)
from agentkit.skills.git import GitCommit, SkillGitRepository
from agentkit.skills.package import build_package, render_skill_md, validate_skill_path
from agentkit.skills.store.model import SkillRow, SkillSubmissionRow
from agentkit.skills.store.sql import SqlSkillStore
from agentkit.skills.types import SKILL_FILE_NAME

logger = logging.getLogger(__name__)


def _staged_info(file: StagedFile) -> StagedFileInfo:
    return StagedFileInfo(
        path=file.path,
        size_bytes=len(file.content),
        mode=file.mode,
        sha256=hashlib.sha256(file.content).hexdigest(),
    )


def _skill_directory(raw_path: str) -> str:
    """Return the containing directory for a release path.

    Releases record the SKILL.md path (public/name/SKILL.md), while Git
    returns the directory it wrote.  Both forms are accepted here.
    """

    path = validate_skill_path(raw_path)
    if PurePosixPath(path).name == SKILL_FILE_NAME:
        parent = str(PurePosixPath(path).parent)
        if parent in ("", "."):
            raise InvalidSkillPackage("a skill path must have a containing directory")
        return parent
    return path


class SkillAuthorService:
    """Create, edit, save, submit, and delete one owner's private skill drafts."""

    def __init__(
        self,
        store: SqlSkillStore,
        git: SkillGitRepository,
        content: SkillContentStore,
        creation: SkillCreationConfig,
    ) -> None:
        self._store = store
        self._git = git
        self._content = content
        self._creation = creation
        self._local_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def create_draft(
        self, *, owner_user_id: str, name: str, description: str
    ) -> SkillRecord:
        """Create a private skill and stage its initial SKILL.md.

        This never writes Git.  The first save is what produces a version and
        a private active pointer.
        """

        initial = StagedFile(
            path=SKILL_FILE_NAME,
            content=render_skill_md(name, description).encode("utf-8"),
            mode="text",
        )
        # Validate the generated frontmatter and the package limits before an
        # orphan skill row can be created.
        build_package(
            {initial.path: initial},
            max_files=self._creation.max_files,
            max_file_bytes=self._creation.max_file_bytes,
            max_package_bytes=self._creation.max_package_bytes,
        )

        owned = await self._store.list_owned(owner_user_id)
        if len(owned) >= self._creation.max_drafts_per_user:
            raise SkillConflict("draft quota exceeded")
        if await self._public_name_taken(name):
            raise SkillConflict(f"public name {name!r} is already in use")

        skill = await self._store.create_skill(
            owner_user_id=owner_user_id, private_name=name
        )
        await self._store.put_draft_file(
            skill.id, initial.path, initial.content, initial.mode
        )
        return skill

    async def open_draft(
        self, *, owner_user_id: str, skill_id: str
    ) -> list[StagedFileInfo]:
        """Return staged files, seeding an empty draft from the public release."""

        # 重新打开是公共状态路径：作者删掉私有草稿后，记录仍是软删，
        # 但已发布的版本还在。先确认所有权并恢复记录，再按需从公共版本播种。
        await self._owned_skill(owner_user_id, skill_id, include_deleted=True)
        await self._store.revive_private_draft(
            skill_id=skill_id, owner_user_id=owner_user_id
        )
        existing = await self._store.list_draft_files(skill_id)
        if existing:
            return [_staged_info(file) for file in existing]

        release = await self._public_active_for(owner_user_id, skill_id)
        if release is None:
            return []
        directory = _skill_directory(release.skill_path)
        staged: list[StagedFile] = []
        for listed in await self._git.list_tree(release.git_commit, directory):
            relative = self._relative_tree_path(listed, directory)
            content = await self._git.read_file(
                release.git_commit, f"{directory}/{relative}"
            )
            staged.append(StagedFile(relative, content, "text"))

        # Refuse to seed a package Git cannot have produced as a valid skill.
        build_package(
            {file.path: file for file in staged},
            max_files=self._creation.max_files,
            max_file_bytes=self._creation.max_file_bytes,
            max_package_bytes=self._creation.max_package_bytes,
        )
        infos: list[StagedFileInfo] = []
        for file in sorted(staged, key=lambda item: item.path):
            infos.append(
                await self._store.put_draft_file(
                    skill_id, file.path, file.content, file.mode
                )
            )
        return infos

    async def list_versions(
        self, *, owner_user_id: str, skill_id: str
    ) -> list[SkillVersionRecord]:
        await self._owned_skill(owner_user_id, skill_id)
        return await self._store.list_versions(skill_id)

    async def list_mine(self, *, owner_user_id: str) -> list[SkillRecord]:
        """Owned, non-deleted skills, including drafts that have no version yet.

        The route layer needs this before a first save; visible_for_user() only
        contains rows with an active version, so it cannot back /skills/mine.
        """

        return await self._store.list_owned(owner_user_id)

    async def get_owned_skill(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        include_deleted: bool = False,
    ) -> SkillRecord | None:
        """Return the skill only when this caller owns it; None for absent/foreign."""

        skill = await self._store.get_skill(skill_id, include_deleted=include_deleted)
        if skill is None or skill.owner_user_id != owner_user_id:
            return None
        return skill

    async def latest_submission(
        self, *, owner_user_id: str, skill_id: str
    ) -> SkillSubmissionRecord | None:
        """The most recent submission, whatever its status."""

        await self._owned_skill(owner_user_id, skill_id)
        return await self._store.latest_submission(skill_id)

    async def submission_status(
        self, *, owner_user_id: str, skill_id: str
    ) -> str | None:
        """The latest submission status, or None when nothing was ever submitted."""

        latest = await self.latest_submission(
            owner_user_id=owner_user_id, skill_id=skill_id
        )
        return latest.status if latest is not None else None

    # ------------------------------------------------------------------
    # staging
    # ------------------------------------------------------------------

    async def put_file(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        path: str,
        content: bytes,
        mode: str = "text",
    ) -> StagedFileInfo:
        await self._owned_skill(owner_user_id, skill_id)
        await self._reject_pending(skill_id)
        staged = {
            file.path: file for file in await self._store.list_draft_files(skill_id)
        }
        staged[path] = StagedFile(path=path, content=content, mode=mode)
        self._build(staged)
        return await self._store.put_draft_file(skill_id, path, content, mode)

    async def delete_file(
        self, *, owner_user_id: str, skill_id: str, path: str
    ) -> None:
        await self._owned_skill(owner_user_id, skill_id)
        await self._reject_pending(skill_id)
        validate_skill_path(path)
        await self._store.delete_draft_file(skill_id, path)

    async def list_files(
        self, *, owner_user_id: str, skill_id: str
    ) -> list[StagedFileInfo]:
        await self._owned_skill(owner_user_id, skill_id)
        files = await self._store.list_draft_files(skill_id)
        return [_staged_info(file) for file in files]

    async def read_file(
        self, *, owner_user_id: str, skill_id: str, path: str
    ) -> StagedFile | None:
        """Read one staged file; None means the path is not staged for this skill."""

        await self._owned_skill(owner_user_id, skill_id)
        validate_skill_path(path)
        for file in await self._store.list_draft_files(skill_id):
            if file.path == path:
                return file
        return None

    # ------------------------------------------------------------------
    # save, submit, withdraw
    # ------------------------------------------------------------------

    async def save(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> SkillVersionRecord:
        """Commit the exact staging tree, then record it in the catalog."""

        async with self._skill_lock(skill_id):
            await self._owned_skill(owner_user_id, skill_id)
            await self._reject_pending(skill_id)
            files = await self._store.list_draft_files(skill_id)
            manifest = self._build({file.path: file for file in files})

            # Never call Git while a database transaction is open.  The store
            # call below opens its own transaction after the commit succeeds.
            commit = await self._git.save_draft(
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                files=manifest.files,
                message=f"Save draft for {skill_id}",
            )
            return await self._store.record_saved_version(
                skill_id=skill_id,
                owner_user_id=owner_user_id,
                manifest=manifest,
                draft_ref=commit.ref,
                draft_commit=commit.commit,
                skill_path=self._skill_path(commit),
                extra_metadata=extra_metadata,
            )

    async def submit(
        self, *, owner_user_id: str, skill_id: str
    ) -> SkillSubmissionRecord:
        await self._owned_skill(owner_user_id, skill_id)
        if await self._store.pending_submission(skill_id) is not None:
            raise SkillConflict(f"skill {skill_id!r} already has a pending submission")

        pending_count = await self._pending_submission_count(owner_user_id)
        if pending_count >= self._creation.max_pending_submissions_per_user:
            raise SkillConflict("pending submission quota exceeded")

        version = await self._private_active_version(owner_user_id, skill_id)
        if version is None or not version.draft_ref:
            raise SkillConflict(f"skill {skill_id!r} has no saved version")

        # The branch head is authoritative for review.  The whole package,
        # not only SKILL.md, must match the saved version, otherwise an
        # attachment could change between save and review.
        submitted_commit = await self._git.head(version.draft_ref)
        submitted_package = await self._git.package_sha256(
            submitted_commit, version.skill_path
        )
        if submitted_package != version.package_sha256:
            raise SkillConflict(
                f"skill {skill_id!r} draft changed since the saved version"
            )

        return await self._store.create_submission(
            skill_id=skill_id,
            version=version,
            owner_user_id=owner_user_id,
            submitted_commit=submitted_commit,
        )

    async def withdraw(
        self, *, owner_user_id: str, skill_id: str
    ) -> SkillSubmissionRecord:
        await self._owned_skill(owner_user_id, skill_id)
        pending = await self._store.pending_submission(skill_id)
        if pending is None:
            raise SkillConflict(f"skill {skill_id!r} has no pending submission")
        if pending.owner_user_id != owner_user_id:
            raise SkillConflict(f"skill {skill_id!r} does not exist")
        return await self._store.set_submission_status(
            pending.id, status="withdrawn"
        )

    async def delete_draft(self, *, owner_user_id: str, skill_id: str) -> None:
        """Delete private staging and the private pointer, never public state."""

        await self._owned_skill(owner_user_id, skill_id)
        await self._reject_pending(skill_id)
        deleted = await self._store.delete_private_draft(
            skill_id=skill_id, owner_user_id=owner_user_id, actor=owner_user_id
        )
        if not deleted:
            raise SkillConflict(f"skill {skill_id!r} does not exist")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _owned_skill(
        self, owner_user_id: str, skill_id: str, *, include_deleted: bool = False
    ) -> SkillRecord:
        skill = await self._store.get_skill(skill_id, include_deleted=include_deleted)
        if skill is None or skill.owner_user_id != owner_user_id:
            # Deliberately indistinguishable from a missing skill: another
            # owner must not learn that this draft exists.
            raise SkillConflict(f"skill {skill_id!r} does not exist")
        return skill

    async def _reject_pending(self, skill_id: str) -> None:
        if await self._store.pending_submission(skill_id) is not None:
            raise SkillConflict(f"skill {skill_id!r} has a pending submission")

    def _build(self, files: dict[str, StagedFile]):
        return build_package(
            files,
            max_files=self._creation.max_files,
            max_file_bytes=self._creation.max_file_bytes,
            max_package_bytes=self._creation.max_package_bytes,
        )

    async def _public_name_taken(self, name: str) -> bool:
        async with self._store._sf() as session:  # type: ignore[attr-defined]
            found = await session.scalar(
                select(SkillRow.id)
                .where(SkillRow.public_name == name)
                .limit(1)
            )
            return found is not None

    async def _public_active_for(
        self, owner_user_id: str, skill_id: str
    ) -> SkillReleaseRecord | None:
        """Find an owned public active release through the visibility projection."""

        for visible in await self._store.visible_for_user(owner_user_id):
            if (
                visible.scope == SkillScope.PUBLIC
                and visible.skill.skill_id == skill_id
                and visible.skill.owner_user_id == owner_user_id
            ):
                return visible.release
        return None

    async def _private_active_version(
        self, owner_user_id: str, skill_id: str
    ) -> SkillVersionRecord | None:
        for visible in await self._store.visible_for_user(owner_user_id):
            if (
                visible.scope == SkillScope.PRIVATE
                and visible.skill.skill_id == skill_id
                and visible.skill.owner_user_id == owner_user_id
            ):
                return visible.version
        return None

    async def _pending_submission_count(self, owner_user_id: str) -> int:
        async with self._store._sf() as session:  # type: ignore[attr-defined]
            count = await session.scalar(
                select(func.count())
                .select_from(SkillSubmissionRow)
                .where(
                    SkillSubmissionRow.owner_user_id == owner_user_id,
                    SkillSubmissionRow.status == "pending",
                )
            )
            return int(count or 0)

    @staticmethod
    def _relative_tree_path(listed: str, directory: str) -> str:
        if listed == directory:
            return PurePosixPath(directory).name
        prefix = directory.rstrip("/") + "/"
        if listed.startswith(prefix):
            return listed[len(prefix) :]
        return listed

    @staticmethod
    def _skill_path(commit: GitCommit) -> str:
        path = commit.path.rstrip("/")
        if PurePosixPath(path).name == SKILL_FILE_NAME:
            return path
        return f"{path}/{SKILL_FILE_NAME}"

    @staticmethod
    def _lock_key(skill_id: str) -> int:
        digest = hashlib.sha256(f"skill-author:{skill_id}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def _bind(self):
        factory = getattr(self._store, "_sf", None)
        kwargs = getattr(factory, "kw", None)
        if not isinstance(kwargs, dict):
            return None
        return kwargs.get("bind")

    async def _local_lock(self, skill_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._local_locks.get(skill_id)
            if lock is None:
                lock = asyncio.Lock()
                self._local_locks[skill_id] = lock
            return lock

    @asynccontextmanager
    async def _skill_lock(self, skill_id: str) -> AsyncIterator[None]:
        """Serialize saves for one skill without holding a DB transaction."""

        async with await self._local_lock(skill_id):
            bind = self._bind()
            dialect = getattr(bind, "dialect", None)
            if dialect is None or dialect.name != "postgresql":
                yield
                return

            key = self._lock_key(skill_id)
            async with bind.connect() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_lock(CAST(:key AS bigint))"),
                    {"key": key},
                )
                # End the acquisition transaction; the session-level advisory
                # lock stays on this connection until we explicitly release it.
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
                    except Exception:  # pragma: no cover - release on close
                        logger.warning(
                            "could not release skill author advisory lock",
                            exc_info=True,
                        )


__all__ = ["SkillAuthorService"]
