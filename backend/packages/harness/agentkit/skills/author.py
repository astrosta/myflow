"""Author-facing orchestration for private skill drafts and activation.

The service keeps Git and SQL separate: Git commits happen before the catalog
transaction that records them, and a per-skill advisory lock serializes saves,
scans and activation for the same skill.  PostgreSQL uses a session-level
advisory lock on a dedicated connection; SQLite relies on its transactional
write lock plus an in-process lock.

Lifecycle:
    create -> stage -> save (immutable Git draft version)
           -> scan (cisco-ai-skill-scanner over the saved commit)
           -> activate (passing scan -> active pointer + outbox)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from sqlalchemy import text

from agentkit.config.skills_config import SkillCreationConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SecurityScanRecord,
    SkillConflict,
    SkillRecord,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
)
from agentkit.skills.git import GitCommit, SkillGitRepository
from agentkit.skills.package import build_package, render_skill_md, validate_skill_path
from agentkit.skills.parser import FRONTMATTER_RE, SkillParseError, parse_skill_text
from agentkit.skills.security_scan import (
    CiscoSkillScanner,
    SkillSecurityScanner,
)
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
    """Return the containing directory for a saved version path."""
    path = validate_skill_path(raw_path)
    if PurePosixPath(path).name == SKILL_FILE_NAME:
        parent = str(PurePosixPath(path).parent)
        if parent in ("", "."):
            raise InvalidSkillPackage("a skill path must have a containing directory")
        return parent
    return path


class SkillAuthorService:
    """Create, edit, save, scan, activate, and delete one owner's skills."""

    def __init__(
        self,
        store: SqlSkillStore,
        git: SkillGitRepository,
        content: SkillContentStore,
        creation: SkillCreationConfig,
        *,
        reserved_names: Callable[[], frozenset[str]] | None = None,
        scanner: SkillSecurityScanner | None = None,
    ) -> None:
        self._store = store
        self._git = git
        self._content = content
        self._creation = creation
        self._reserved_names = reserved_names
        self._scanner = scanner or CiscoSkillScanner()
        self._local_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # identity and drafts
    # ------------------------------------------------------------------

    async def create_draft(
        self, *, owner_user_id: str, name: str, description: str
    ) -> SkillRecord:
        """Create a private skill and stage its initial SKILL.md.

        This never writes Git and never creates a version.  The first save is
        what produces a draft version.
        """

        if not self._creation.enabled:
            raise SkillConflict("skill creation is disabled")
        if self._reserved_names is not None and name in self._reserved_names():
            raise SkillConflict("name is reserved by a built-in skill")

        owned = await self._store.list_owned(owner_user_id)
        taken = await self._display_names(owned)
        taken.update(skill.private_name for skill in owned)
        if name in taken:
            raise SkillConflict(
                f"skill name {name!r} is already in use by another of your skills"
            )

        initial = StagedFile(
            path=SKILL_FILE_NAME,
            content=render_skill_md(name, description).encode("utf-8"),
            mode="text",
        )
        build_package(
            {initial.path: initial},
            max_files=self._creation.max_files,
            max_file_bytes=self._creation.max_file_bytes,
            max_package_bytes=self._creation.max_package_bytes,
        )

        if len(owned) >= self._creation.max_drafts_per_user:
            raise SkillConflict("draft quota exceeded")

        skill = await self._store.create_skill(
            owner_user_id=owner_user_id, private_name=name
        )
        await self._store.put_draft_file(
            skill.id, initial.path, initial.content, initial.mode
        )
        return skill

    async def import_extracted_skill(
        self,
        *,
        owner_user_id: str,
        skill_md: str,
        source_uri: str,
        source_task_id: str | None = None,
    ) -> SkillRecord:
        """Import an OpenViking session-skill as an extracted, unscanned draft.

        The generated SKILL.md is saved as a normal inert draft version. It is
        deliberately not scanned or activated: the user reviews it in the skills
        page, runs the security scan, and activates it explicitly. Re-importing
        the same source URI is idempotent.
        """

        if not self._creation.enabled:
            raise SkillConflict("skill creation is disabled")
        try:
            parsed = parse_skill_text(skill_md)
        except SkillParseError as exc:
            raise InvalidSkillPackage(str(exc)) from exc
        if not parsed.name:
            raise InvalidSkillPackage("extracted skill has no name")

        owned = await self._store.list_owned(owner_user_id)
        for existing in owned:
            if existing.source_uri == source_uri:
                return existing
        if self._reserved_names is not None and parsed.name in self._reserved_names():
            raise SkillConflict("name is reserved by a built-in skill")
        if len(owned) >= self._creation.max_drafts_per_user:
            raise SkillConflict("draft quota exceeded")

        taken = {skill.private_name for skill in owned}
        taken.update(await self._display_names(owned))
        private_name = _unique_skill_name(parsed.name, taken, source_uri)
        if private_name != parsed.name:
            skill_md = _rename_skill_md(skill_md, private_name)
        skill = await self._store.create_skill(
            owner_user_id=owner_user_id,
            private_name=private_name,
            source="extracted",
            source_uri=source_uri,
        )
        staged = StagedFile(
            path=SKILL_FILE_NAME,
            content=skill_md.encode("utf-8"),
            mode="text",
        )
        await self._store.put_draft_file(
            skill.id, staged.path, staged.content, staged.mode
        )
        await self.save(
            owner_user_id=owner_user_id,
            skill_id=skill.id,
            extra_metadata={
                "source": "extracted",
                "source_uri": source_uri,
                "source_task_id": source_task_id,
            },
        )
        return skill

    async def open_draft(
        self, *, owner_user_id: str, skill_id: str
    ) -> list[StagedFileInfo]:
        """Return staged files, reviving a soft-deleted record if necessary."""
        await self._owned_skill(owner_user_id, skill_id, include_deleted=True)
        await self._store.revive_private_draft(
            skill_id=skill_id, owner_user_id=owner_user_id
        )
        existing = await self._store.list_draft_files(skill_id)
        return [_staged_info(file) for file in existing]

    async def list_versions(
        self, *, owner_user_id: str, skill_id: str
    ) -> list[SkillVersionRecord]:
        await self._owned_skill(owner_user_id, skill_id)
        return await self._store.list_versions(skill_id)

    async def latest_version(
        self, *, owner_user_id: str, skill_id: str
    ) -> SkillVersionRecord | None:
        await self._owned_skill(owner_user_id, skill_id)
        return await self._store.latest_version(skill_id)

    async def active_version(
        self, *, owner_user_id: str, skill_id: str
    ) -> SkillVersionRecord | None:
        await self._owned_skill(owner_user_id, skill_id)
        return await self._store.private_active(skill_id)

    async def list_mine(self, *, owner_user_id: str) -> list[SkillRecord]:
        """Owned, non-deleted skills, including drafts with no version yet."""
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
    # save
    # ------------------------------------------------------------------

    async def save(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        extra_metadata: Mapping[str, object] | None = None,
    ) -> SkillVersionRecord:
        """Commit the exact staging tree and record an inert draft version."""

        async with self._skill_lock(skill_id):
            await self._owned_skill(owner_user_id, skill_id)
            files = await self._store.list_draft_files(skill_id)
            manifest = self._build({file.path: file for file in files})
            await self._ensure_name_available(
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                name=manifest.parsed.name,
            )

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

    # ------------------------------------------------------------------
    # security scan and activation
    # ------------------------------------------------------------------

    async def latest_scan(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        version_id: str | None = None,
    ) -> SecurityScanRecord | None:
        await self._owned_skill(owner_user_id, skill_id)
        return await self._store.latest_security_scan(skill_id, version_id)

    async def list_scans(
        self, *, owner_user_id: str, skill_id: str, limit: int = 20
    ) -> list[SecurityScanRecord]:
        await self._owned_skill(owner_user_id, skill_id)
        return await self._store.list_security_scans(skill_id, limit=limit)

    async def scan_security(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        version_id: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> SecurityScanRecord:
        """Scan the latest saved version with the imported Cisco scanner.

        The scan target is immutable Git content from the commit cache, never
        the mutable staging table.  A scan of an older version is rejected so
        the editor cannot show a misleading "passed" state for current content.
        """
        async with self._skill_lock(skill_id):
            await self._owned_skill(owner_user_id, skill_id)
            version = await self._store.latest_version(skill_id)
            if version is None or not version.draft_commit:
                raise SkillConflict(f"skill {skill_id!r} has no saved version")
            if version_id is not None and version_id != version.id:
                raise SkillConflict(
                    f"version {version_id!r} is not the latest saved version"
                )

            try:
                root = await self._content.ensure(version.draft_commit)
            except Exception as exc:  # noqa: BLE001 - never leak a cache/Git path
                logger.warning(
                    "Could not materialize skill %s for scanning (%s)",
                    skill_id,
                    type(exc).__name__,
                )
                raise SkillConflict(
                    "saved skill content is unavailable; save the skill again and retry"
                ) from exc
            directory = _skill_directory(version.skill_path)
            target = root / PurePosixPath(directory)
            if target.is_symlink() or not target.is_dir():
                raise SkillConflict(
                    f"skill {skill_id!r} content is not available for scanning"
                )

            # The scanner is synchronous CPU work; keep the event loop free.
            # on_status is only forwarded to the Cisco adapter. Test doubles
            # implement the one-argument protocol and must keep working.
            if on_status is None:
                outcome = await asyncio.to_thread(self._scanner.scan, target)
            else:
                outcome = await asyncio.to_thread(
                    self._scanner.scan, target, on_status=on_status
                )
            return await self._store.record_security_scan(
                skill_id=skill_id,
                owner_user_id=owner_user_id,
                version_id=version.id,
                status=outcome.status,
                is_safe=outcome.is_safe,
                max_severity=outcome.max_severity,
                findings=outcome.findings,
                scanner="cisco-ai-skill-scanner",
                scanner_version=outcome.scanner_version,
                duration_ms=outcome.duration_ms,
                error=outcome.error,
            )

    async def activate(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        version_id: str | None = None,
        index_enabled: bool = True,
    ) -> SkillVersionRecord:
        """Activate the latest version only when its latest scan passed."""
        async with self._skill_lock(skill_id):
            skill = await self._owned_skill(owner_user_id, skill_id)
            version = await self._store.latest_version(skill_id)
            if version is None:
                raise SkillConflict(f"skill {skill_id!r} has no saved version")
            if version_id is not None and version_id != version.id:
                raise SkillConflict(
                    f"version {version_id!r} is not the latest saved version"
                )

            await self._ensure_name_available(
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                name=str(version.metadata.get("name") or skill.private_name),
            )
            scan = await self._store.latest_security_scan(skill_id, version.id)
            if scan is None:
                raise SkillConflict("run a security scan before activating this skill")
            if scan.status != "completed":
                raise SkillConflict(
                    "the latest security scan failed; fix it and scan again"
                )
            if not scan.is_safe:
                raise SkillConflict(
                    "the latest security scan did not pass; fix the findings and scan again"
                )
            return await self._store.activate_version(
                skill_id=skill_id,
                owner_user_id=owner_user_id,
                version_id=version.id,
                scan_id=scan.id,
                actor=owner_user_id,
                index_enabled=index_enabled,
            )

    async def set_enabled(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        enabled: bool,
        index_enabled: bool = True,
    ) -> SkillRecord:
        """Toggle an active private skill without touching its content or version."""
        async with self._skill_lock(skill_id):
            await self._owned_skill(owner_user_id, skill_id)
            return await self._store.set_skill_enabled(
                skill_id=skill_id,
                owner_user_id=owner_user_id,
                enabled=enabled,
                index_enabled=index_enabled,
            )

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete_draft(self, *, owner_user_id: str, skill_id: str) -> None:
        """Delete the owner's skill, active pointer and projection row intent."""
        await self._owned_skill(owner_user_id, skill_id)
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

    def _ensure_name_not_reserved(self, name: str) -> None:
        if self._reserved_names is not None and name in self._reserved_names():
            raise SkillConflict("name is reserved by a built-in skill")

    async def _display_name(self, skill: SkillRecord) -> str:
        """Return the name users actually see for one owned skill.

        A saved version's frontmatter wins; a never-saved draft falls back to
        its staged SKILL.md, then to the internal row name.
        """
        latest = await self._store.latest_version(skill.id)
        if latest is not None:
            return str(latest.metadata.get("name") or skill.private_name)
        for staged in await self._store.list_draft_files(skill.id):
            if staged.path != SKILL_FILE_NAME:
                continue
            try:
                parsed = parse_skill_text(staged.content.decode("utf-8"))
            except (UnicodeDecodeError, SkillParseError, ValueError):
                break
            if parsed.name:
                return parsed.name
            break
        return skill.private_name

    async def _display_names(
        self,
        owned: list[SkillRecord],
        *,
        exclude_skill_id: str | None = None,
    ) -> set[str]:
        names: set[str] = set()
        for skill in owned:
            if skill.id == exclude_skill_id:
                continue
            names.add(await self._display_name(skill))
        return names

    async def _ensure_name_available(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        name: str,
    ) -> None:
        if not name:
            return
        self._ensure_name_not_reserved(name)
        owned = await self._store.list_owned(owner_user_id)
        if name in await self._display_names(owned, exclude_skill_id=skill_id):
            raise SkillConflict(
                f"skill name {name!r} is already in use by another of your skills"
            )

    def _build(self, files: dict[str, StagedFile]):
        return build_package(
            files,
            max_files=self._creation.max_files,
            max_file_bytes=self._creation.max_file_bytes,
            max_package_bytes=self._creation.max_package_bytes,
        )

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
        """Serialize saves/scans/activation for one skill without holding a DB txn."""
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


def _rename_skill_md(text: str, name: str) -> str:
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return text
    front, count = re.subn(
        r"(?m)^name:\s*.*$",
        f"name: {name}",
        match.group(1),
        count=1,
    )
    if count == 0:
        return text
    return text[: match.start(1)] + front + text[match.end(1) :]


def _unique_skill_name(base: str, taken: set[str], source_uri: str) -> str:
    if base not in taken:
        return base
    suffix = hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:8]
    candidate = f"{base}-{suffix}"
    index = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}-{index}"
        index += 1
    return candidate


__all__ = ["SkillAuthorService"]
