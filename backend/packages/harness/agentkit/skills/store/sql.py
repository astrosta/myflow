"""SQL repository for the Git-backed skill catalog.

Every public method opens exactly one short-lived session.  The three mutating
calls that move an active pointer (record_saved_version, create_submission and
record_public_release) write the state change, active pointer, audit event and
projection outbox row in one transaction, so a worker can never observe a
half-applied catalog change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from agentkit.skills.domain import (
    PackageManifest,
    ReviewDetail,
    ReviewItem,
    SkillConflict,
    SkillRecord,
    SkillReleaseRecord,
    SkillScope,
    SkillSubmissionRecord,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
    VisibleSkill,
)
from agentkit.skills.store.model import (
    SkillAuditEventRow,
    SkillDraftFileRow,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillPublicActiveRow,
    SkillReleaseBatchRow,
    SkillReleaseRow,
    SkillRow,
    SkillSettingsRow,
    SkillSubmissionRow,
    SkillVersionRow,
)
from agentkit.skills.types import SecretRequirement, Skill, SkillCategory

if TYPE_CHECKING:
    # Task 10 owns the Milvus projection record.  Importing it only for type
    # checking keeps this store usable before projection.py exists.
    from agentkit.skills.projection import ProjectionHit


_MAX_OUTBOX_PAYLOAD_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """A claimed projection-outbox row, without worker-only bookkeeping fields."""

    id: str
    op: str
    projection_key: str
    scope: str
    owner_user_id: str | None
    skill_id: str
    version_id: str | None
    payload: dict[str, Any]
    attempts: int


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; the catalog contract is always UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _metadata(manifest: PackageManifest) -> dict[str, Any]:
    """Snapshot the frontmatter that later services need without Git."""
    parsed = manifest.parsed
    return {
        "name": parsed.name,
        "description": parsed.description,
        "license": parsed.license,
        "argument_hint": parsed.argument_hint,
        "allowed_tools": list(parsed.allowed_tools) if parsed.allowed_tools is not None else None,
        "required_secrets": [
            {"name": secret.name, "optional": secret.optional}
            for secret in parsed.required_secrets
        ],
        "secrets_autonomous": parsed.secrets_autonomous,
    }


def _skill_record(row: SkillRow) -> SkillRecord:
    return SkillRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        private_name=row.private_name,
        public_name=row.public_name,
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_utc(row.updated_at),  # type: ignore[arg-type]
        deleted_at=_utc(row.deleted_at),
    )


def _version_record(row: SkillVersionRow) -> SkillVersionRecord:
    return SkillVersionRecord(
        id=row.id,
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        version_seq=row.version_seq,
        package_sha256=row.package_sha256,
        skill_md_sha256=row.skill_md_sha256,
        metadata=dict(row.metadata_json or {}),
        draft_ref=row.draft_ref,
        draft_commit=row.draft_commit,
        skill_path=row.skill_path,
        index_status=row.index_status,
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
    )


def _submission_record(row: SkillSubmissionRow) -> SkillSubmissionRecord:
    return SkillSubmissionRecord(
        id=row.id,
        skill_id=row.skill_id,
        version_id=row.version_id,
        submitted_commit=row.submitted_commit,
        owner_user_id=row.owner_user_id,
        status=row.status,
        submitted_at=_utc(row.submitted_at),  # type: ignore[arg-type]
        reviewed_by=row.reviewed_by,
        reviewed_at=_utc(row.reviewed_at),
        reject_reason=row.reject_reason,
        public_release_id=row.public_release_id,
    )


def _release_record(row: SkillReleaseRow) -> SkillReleaseRecord:
    return SkillReleaseRecord(
        id=row.id,
        batch_id=row.batch_id,
        version_id=row.version_id,
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        git_repository=row.git_repository,
        git_commit=row.git_commit,
        skill_path=row.skill_path,
        public_name=row.public_name,
        source_commit=row.source_commit,
        approved_by=row.approved_by,
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
    )


def _review_item(
    skill: SkillRow, version: SkillVersionRow, submission: SkillSubmissionRow
) -> ReviewItem:
    return ReviewItem(
        submission_id=submission.id,
        skill_id=skill.id,
        name=skill.private_name,
        version=version.skill_version,
        owner_user_id=submission.owner_user_id,
        submitted_at=_utc(submission.submitted_at),  # type: ignore[arg-type]
        submitted_commit=submission.submitted_commit,
    )


def _allowed_tools(metadata: dict[str, Any]) -> tuple[str, ...] | None:
    raw = metadata.get("allowed_tools")
    if raw is None:
        return None
    return tuple(str(item) for item in raw)


def _required_secrets(metadata: dict[str, Any]) -> tuple[SecretRequirement, ...]:
    raw = metadata.get("required_secrets") or []
    secrets: list[SecretRequirement] = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            secrets.append(
                SecretRequirement(
                    name=str(item["name"]), optional=bool(item.get("optional", False))
                )
            )
    return tuple(secrets)


def _skill_for_visibility(
    skill: SkillRow,
    version: SkillVersionRow,
    *,
    scope: SkillScope,
    enabled: bool,
    release: SkillReleaseRow | None = None,
) -> Skill:
    metadata = dict(version.metadata_json or {})
    if scope is SkillScope.PUBLIC:
        name = str(skill.public_name or metadata.get("name") or skill.private_name)
    else:
        name = str(metadata.get("name") or skill.private_name)
    return Skill(
        name=name,
        description=str(metadata.get("description") or ""),
        license=metadata.get("license"),
        skill_dir=Path(version.skill_path).parent,
        skill_file=Path(version.skill_path),
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        allowed_tools=_allowed_tools(metadata),
        enabled=enabled,
        required_secrets=_required_secrets(metadata),
        secrets_autonomous=bool(metadata.get("secrets_autonomous", True)),
        argument_hint=metadata.get("argument_hint"),
        skill_id=skill.id,
        scope=scope,
        owner_user_id=skill.owner_user_id,
        version_id=version.id,
        skill_version=version.skill_version,
        release_id=release.id if release is not None else None,
        git_repository=release.git_repository if release is not None else None,
        git_commit=release.git_commit if release is not None else version.draft_commit,
        repo_path=version.skill_path,
    )


def _visible(
    skill: SkillRow,
    version: SkillVersionRow,
    *,
    scope: SkillScope,
    enabled: bool,
    release: SkillReleaseRow | None = None,
) -> VisibleSkill:
    return VisibleSkill(
        skill=_skill_for_visibility(
            skill, version, scope=scope, enabled=enabled, release=release
        ),
        scope=scope,
        version=_version_record(version),
        release=_release_record(release) if release is not None else None,
    )


def _validate_outbox_payload(payload: dict[str, Any]) -> None:
    """Reject payloads whose stored JSON would exceed the 64 KiB contract."""
    try:
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("outbox payload must be JSON serializable") from exc
    if len(encoded) > _MAX_OUTBOX_PAYLOAD_BYTES:
        raise ValueError("outbox payload exceeds 64 KiB")


def _outbox_event(row: SkillIndexOutboxRow) -> OutboxEvent:
    return OutboxEvent(
        id=row.id,
        op=row.op,
        projection_key=row.projection_key,
        scope=row.scope,
        owner_user_id=row.owner_user_id,
        skill_id=row.skill_id,
        version_id=row.version_id,
        payload=dict(row.payload_json or {}),
        attempts=row.attempts,
    )


def _projection_payload(
    version: SkillVersionRow,
    *,
    enabled: bool,
    name: str | None = None,
) -> dict[str, Any]:
    metadata = dict(version.metadata_json or {})
    return {
        "enabled": enabled,
        "name": name or metadata.get("name") or version.skill_version,
        "description": metadata.get("description") or "",
        "skill_version": version.skill_version,
        "package_sha256": version.package_sha256,
        "skill_md_sha256": version.skill_md_sha256,
    }


class SqlSkillStore:
    """Async SQL implementation of catalog identity and lifecycle state."""

    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------
    # identity and versions
    # ------------------------------------------------------------------

    async def create_skill(self, *, owner_user_id: str, private_name: str) -> SkillRecord:
        if not private_name or not private_name.strip():
            raise ValueError("private_name must not be blank")
        now = _now()
        row = SkillRow(
            owner_user_id=owner_user_id,
            private_name=private_name,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await self._flush(session, f"skill {private_name!r} already exists for this owner")
            record = _skill_record(row)
            await self._commit(session, f"skill {private_name!r} already exists for this owner")
            return record

    async def get_skill(self, skill_id: str) -> SkillRecord | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(SkillRow).where(
                    SkillRow.id == skill_id,
                    SkillRow.deleted_at.is_(None),
                )
            )
            return _skill_record(row) if row is not None else None

    async def list_owned(self, owner_user_id: str) -> list[SkillRecord]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(SkillRow)
                    .where(
                        SkillRow.owner_user_id == owner_user_id,
                        SkillRow.deleted_at.is_(None),
                    )
                    .order_by(SkillRow.created_at.asc(), SkillRow.id.asc())
                )
            ).all()
            return [_skill_record(row) for row in rows]

    async def find_version_by_hash(
        self, skill_id: str, package_sha256: str
    ) -> SkillVersionRecord | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(SkillVersionRow).where(
                    SkillVersionRow.skill_id == skill_id,
                    SkillVersionRow.package_sha256 == package_sha256,
                )
            )
            return _version_record(row) if row is not None else None

    async def next_version_seq(self, skill_id: str) -> int:
        async with self._sf() as session:
            highest = await session.scalar(
                select(func.max(SkillVersionRow.version_seq)).where(
                    SkillVersionRow.skill_id == skill_id
                )
            )
            return int(highest or 0) + 1

    async def record_saved_version(
        self,
        *,
        skill_id: str,
        owner_user_id: str,
        manifest: PackageManifest,
        draft_ref: str,
        draft_commit: str,
        skill_path: str,
    ) -> SkillVersionRecord:
        now = _now()
        async with self._sf() as session:
            skill = await self._owned_skill(session, skill_id, owner_user_id)
            version = await session.scalar(
                select(SkillVersionRow).where(
                    SkillVersionRow.skill_id == skill_id,
                    SkillVersionRow.package_sha256 == manifest.package_sha256,
                )
            )
            if version is None:
                highest = await session.scalar(
                    select(func.max(SkillVersionRow.version_seq)).where(
                        SkillVersionRow.skill_id == skill_id
                    )
                )
                seq = int(highest or 0) + 1
                version = SkillVersionRow(
                    skill_id=skill_id,
                    skill_version=f"v{seq}",
                    version_seq=seq,
                    package_sha256=manifest.package_sha256,
                    skill_md_sha256=manifest.skill_md_sha256,
                    metadata_json=_metadata(manifest),
                    draft_ref=draft_ref,
                    draft_commit=draft_commit,
                    skill_path=skill_path,
                    index_status="pending",
                    created_at=now,
                )
                session.add(version)
                await self._flush(session, f"skill {skill_id!r} version could not be allocated")

            active = await session.get(SkillPrivateActiveRow, skill_id)
            if active is None:
                session.add(
                    SkillPrivateActiveRow(
                        skill_id=skill_id,
                        owner_user_id=owner_user_id,
                        version_id=version.id,
                        updated_at=now,
                    )
                )
            else:
                active.owner_user_id = owner_user_id
                active.version_id = version.id
                active.updated_at = now

            self._audit_row(
                session,
                actor=owner_user_id,
                action="skill.save",
                skill_id=skill_id,
                detail={
                    "version_id": version.id,
                    "version": version.skill_version,
                    "package_sha256": version.package_sha256,
                },
                now=now,
            )
            self._outbox_row(
                session,
                op="upsert_active",
                projection_key=f"private:{owner_user_id}:{skill_id}",
                scope="private",
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                version_id=version.id,
                payload=_projection_payload(version, enabled=True),
                now=now,
            )
            skill.updated_at = now

            record = _version_record(version)
            await self._commit(session, f"skill {skill_id!r} version could not be allocated")
            return record

    # ------------------------------------------------------------------
    # submissions and reviews
    # ------------------------------------------------------------------

    async def create_submission(
        self,
        *,
        skill_id: str,
        version: SkillVersionRecord,
        owner_user_id: str,
        submitted_commit: str,
    ) -> SkillSubmissionRecord:
        now = _now()
        async with self._sf() as session:
            skill = await self._owned_skill(session, skill_id, owner_user_id)
            version_row = await session.get(SkillVersionRow, version.id)
            if version_row is None or version_row.skill_id != skill_id:
                raise SkillConflict(f"version {version.id!r} does not belong to skill {skill_id!r}")

            pending = await session.scalar(
                select(SkillSubmissionRow.id).where(
                    SkillSubmissionRow.skill_id == skill_id,
                    SkillSubmissionRow.status == "pending",
                )
            )
            if pending is not None:
                raise SkillConflict(f"skill {skill_id!r} already has a pending submission")

            submission = SkillSubmissionRow(
                skill_id=skill_id,
                version_id=version_row.id,
                submitted_commit=submitted_commit,
                owner_user_id=owner_user_id,
                status="pending",
                submitted_at=now,
                created_at=now,
            )
            session.add(submission)
            await self._flush(session, f"skill {skill_id!r} already has a pending submission")

            # Submitting does not create a new version, but keeping the private
            # pointer explicit here makes the one-transaction contract obvious.
            active = await session.get(SkillPrivateActiveRow, skill_id)
            if active is None:
                session.add(
                    SkillPrivateActiveRow(
                        skill_id=skill_id,
                        owner_user_id=owner_user_id,
                        version_id=version_row.id,
                        updated_at=now,
                    )
                )
            else:
                active.owner_user_id = owner_user_id
                active.version_id = version_row.id
                active.updated_at = now

            self._outbox_row(
                session,
                op="upsert_active",
                projection_key=f"private:{owner_user_id}:{skill_id}",
                scope="private",
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                version_id=version_row.id,
                payload=_projection_payload(version_row, enabled=True),
                now=now,
            )
            self._audit_row(
                session,
                actor=owner_user_id,
                action="skill.submit",
                skill_id=skill_id,
                submission_id=submission.id,
                detail={
                    "version_id": version_row.id,
                    "version": version_row.skill_version,
                    "submitted_commit": submitted_commit,
                },
                now=now,
            )
            skill.updated_at = now

            record = _submission_record(submission)
            await self._commit(session, f"skill {skill_id!r} already has a pending submission")
            return record

    async def pending_submission(self, skill_id: str) -> SkillSubmissionRecord | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(SkillSubmissionRow).where(
                    SkillSubmissionRow.skill_id == skill_id,
                    SkillSubmissionRow.status == "pending",
                )
            )
            return _submission_record(row) if row is not None else None

    async def submission(self, submission_id: str) -> SkillSubmissionRecord | None:
        async with self._sf() as session:
            row = await session.get(SkillSubmissionRow, submission_id)
            return _submission_record(row) if row is not None else None

    async def set_submission_status(
        self,
        submission_id: str,
        *,
        status: str,
        reviewed_by: str | None = None,
        reject_reason: str | None = None,
        public_release_id: str | None = None,
    ) -> SkillSubmissionRecord:
        now = _now()
        async with self._sf() as session:
            row = await session.get(SkillSubmissionRow, submission_id)
            if row is None:
                raise SkillConflict(f"submission {submission_id!r} does not exist")

            row.status = status
            if reviewed_by is not None:
                row.reviewed_by = reviewed_by
            if reject_reason is not None:
                row.reject_reason = reject_reason
            if public_release_id is not None:
                row.public_release_id = public_release_id
            if reviewed_by is not None or status != "pending":
                row.reviewed_at = now

            action = {
                "approved": "skill.approve",
                "rejected": "skill.reject",
                "withdrawn": "skill.withdraw",
            }.get(status, "skill.submit")
            self._audit_row(
                session,
                actor=reviewed_by or row.owner_user_id,
                action=action,
                skill_id=row.skill_id,
                submission_id=row.id,
                release_id=row.public_release_id,
                detail={
                    "status": status,
                    "reviewed_by": reviewed_by,
                    "reject_reason": reject_reason,
                },
                now=now,
            )
            record = _submission_record(row)
            await self._commit(session, f"submission {submission_id!r} could not be updated")
            return record

    async def list_pending_submissions(self, limit: int = 50) -> list[ReviewItem]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(SkillSubmissionRow, SkillVersionRow, SkillRow)
                    .join(SkillVersionRow, SkillVersionRow.id == SkillSubmissionRow.version_id)
                    .join(SkillRow, SkillRow.id == SkillSubmissionRow.skill_id)
                    .where(SkillSubmissionRow.status == "pending")
                    .order_by(
                        SkillSubmissionRow.submitted_at.asc(),
                        SkillSubmissionRow.id.asc(),
                    )
                    .limit(limit)
                )
            ).all()
            return [
                _review_item(skill, version, submission)
                for submission, version, skill in rows
            ]

    async def review_detail(self, submission_id: str) -> ReviewDetail | None:
        """Return the database-known review envelope.

        Git-backed file contents are attached by the review service; this store
        intentionally neither reads Git nor invents body content it does not
        have.  The metadata snapshot and exact source commit are authoritative
        here.
        """
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillSubmissionRow, SkillVersionRow, SkillRow)
                    .join(SkillVersionRow, SkillVersionRow.id == SkillSubmissionRow.version_id)
                    .join(SkillRow, SkillRow.id == SkillSubmissionRow.skill_id)
                    .where(SkillSubmissionRow.id == submission_id)
                )
            ).first()
            if row is None:
                return None
            submission, version, skill = row
            return ReviewDetail(
                item=_review_item(skill, version, submission),
                files=[],
                skill_md="",
                diff="",
                metadata=dict(version.metadata_json or {}),
                package_bytes=0,
            )

    async def list_versions(self, skill_id: str) -> list[SkillVersionRecord]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(SkillVersionRow)
                    .where(SkillVersionRow.skill_id == skill_id)
                    .order_by(SkillVersionRow.version_seq.asc())
                )
            ).all()
            return [_version_record(row) for row in rows]

    # ------------------------------------------------------------------
    # public releases and active pointers
    # ------------------------------------------------------------------

    async def record_public_release(
        self,
        *,
        submission: SkillSubmissionRecord,
        public_name: str,
        git_repository: str,
        git_commit: str,
        skill_path: str,
        source_commit: str,
        actor: str,
    ) -> SkillReleaseRecord:
        now = _now()
        async with self._sf() as session:
            submission_row = await session.get(SkillSubmissionRow, submission.id)
            if submission_row is None:
                raise SkillConflict(f"submission {submission.id!r} does not exist")
            if submission_row.status != "pending":
                raise SkillConflict(f"submission {submission.id!r} is not pending")

            skill = await session.get(SkillRow, submission_row.skill_id)
            version = await session.get(SkillVersionRow, submission_row.version_id)
            if skill is None or version is None:
                raise SkillConflict(f"submission {submission.id!r} has no catalog rows")
            if skill.public_name is not None and skill.public_name != public_name:
                raise SkillConflict(
                    f"public name for skill {skill.id!r} is immutable: {skill.public_name!r}"
                )
            collision = await session.scalar(
                select(SkillRow.id).where(
                    SkillRow.public_name == public_name,
                    SkillRow.id != skill.id,
                )
            )
            if collision is not None:
                raise SkillConflict(f"public name {public_name!r} is already in use")

            batch = SkillReleaseBatchRow(
                git_repository=git_repository,
                git_commit=git_commit,
                approved_by=actor,
                created_at=now,
                stats_json={"skills": 1},
            )
            session.add(batch)
            await self._flush(session, "public release batch could not be recorded")

            release = SkillReleaseRow(
                batch_id=batch.id,
                version_id=version.id,
                skill_id=skill.id,
                skill_version=version.skill_version,
                git_repository=git_repository,
                git_commit=git_commit,
                skill_path=skill_path,
                public_name=public_name,
                source_commit=source_commit,
                approved_by=actor,
                created_at=now,
            )
            session.add(release)
            await self._flush(session, f"public release for skill {skill.id!r} conflicts")

            skill.public_name = public_name
            skill.updated_at = now

            active = await session.get(SkillPublicActiveRow, skill.id)
            if active is None:
                session.add(
                    SkillPublicActiveRow(
                        skill_id=skill.id,
                        release_id=release.id,
                        activated_by=actor,
                        updated_at=now,
                    )
                )
            else:
                active.release_id = release.id
                active.activated_by = actor
                active.updated_at = now

            settings = await session.get(SkillSettingsRow, skill.id)
            if settings is None:
                session.add(
                    SkillSettingsRow(
                        skill_id=skill.id,
                        enabled=True,
                        updated_by=actor,
                        updated_at=now,
                    )
                )
            else:
                settings.enabled = True
                settings.updated_by = actor
                settings.updated_at = now

            self._outbox_row(
                session,
                op="upsert_active",
                projection_key=f"public:{skill.id}",
                scope="public",
                owner_user_id=None,
                skill_id=skill.id,
                version_id=version.id,
                payload=_projection_payload(version, enabled=True, name=public_name),
                now=now,
            )
            self._audit_row(
                session,
                actor=actor,
                action="skill.approve",
                skill_id=skill.id,
                submission_id=submission_row.id,
                release_id=release.id,
                detail={
                    "version_id": version.id,
                    "version": version.skill_version,
                    "public_name": public_name,
                    "git_commit": git_commit,
                    "source_commit": source_commit,
                },
                now=now,
            )

            submission_row.status = "approved"
            submission_row.reviewed_by = actor
            submission_row.reviewed_at = now
            submission_row.public_release_id = release.id

            record = _release_record(release)
            await self._commit(session, f"public release for skill {skill.id!r} conflicts")
            return record

    async def set_public_active(self, skill_id: str, release_id: str, actor: str) -> None:
        now = _now()
        async with self._sf() as session:
            skill = await session.get(SkillRow, skill_id)
            release = await session.get(SkillReleaseRow, release_id)
            if skill is None:
                raise SkillConflict(f"skill {skill_id!r} does not exist")
            if release is None or release.skill_id != skill_id:
                raise SkillConflict(
                    f"release {release_id!r} does not belong to skill {skill_id!r}"
                )
            version = await session.get(SkillVersionRow, release.version_id)
            if version is None:
                raise SkillConflict(f"release {release_id!r} has no version row")

            active = await session.get(SkillPublicActiveRow, skill_id)
            if active is None:
                session.add(
                    SkillPublicActiveRow(
                        skill_id=skill_id,
                        release_id=release_id,
                        activated_by=actor,
                        updated_at=now,
                    )
                )
            else:
                active.release_id = release_id
                active.activated_by = actor
                active.updated_at = now

            settings = await session.get(SkillSettingsRow, skill_id)
            if settings is None:
                settings = SkillSettingsRow(
                    skill_id=skill_id,
                    enabled=True,
                    updated_by=actor,
                    updated_at=now,
                )
                session.add(settings)
            enabled = settings.enabled

            self._outbox_row(
                session,
                op="upsert_active",
                projection_key=f"public:{skill_id}",
                scope="public",
                owner_user_id=None,
                skill_id=skill_id,
                version_id=release.version_id,
                payload=_projection_payload(
                    version, enabled=enabled, name=release.public_name
                ),
                now=now,
            )
            self._audit_row(
                session,
                actor=actor,
                action="skill.activate",
                skill_id=skill_id,
                release_id=release_id,
                detail={"release_id": release_id, "version_id": release.version_id},
                now=now,
            )
            skill.updated_at = now
            await self._commit(
                session, f"public active pointer for skill {skill_id!r} conflicts"
            )

    async def clear_public_active(self, skill_id: str) -> None:
        now = _now()
        async with self._sf() as session:
            skill = await session.get(SkillRow, skill_id)
            if skill is None:
                raise SkillConflict(f"skill {skill_id!r} does not exist")

            active = await session.get(SkillPublicActiveRow, skill_id)
            if active is not None:
                await session.delete(active)

            self._outbox_row(
                session,
                op="delete_active",
                projection_key=f"public:{skill_id}",
                scope="public",
                owner_user_id=None,
                skill_id=skill_id,
                version_id=None,
                payload={},
                now=now,
            )
            self._audit_row(
                session,
                actor="system",
                action="skill.unpublish",
                skill_id=skill_id,
                detail={},
                now=now,
            )
            skill.updated_at = now
            await self._commit(
                session, f"public active pointer for skill {skill_id!r} conflicts"
            )

    async def set_global_enabled(self, skill_id: str, enabled: bool, actor: str) -> None:
        now = _now()
        async with self._sf() as session:
            skill = await session.get(SkillRow, skill_id)
            if skill is None:
                raise SkillConflict(f"skill {skill_id!r} does not exist")

            settings = await session.get(SkillSettingsRow, skill_id)
            if settings is None:
                settings = SkillSettingsRow(
                    skill_id=skill_id,
                    enabled=enabled,
                    updated_by=actor,
                    updated_at=now,
                )
                session.add(settings)
            else:
                settings.enabled = enabled
                settings.updated_by = actor
                settings.updated_at = now

            active = await session.get(SkillPublicActiveRow, skill_id)
            if active is not None:
                release = await session.get(SkillReleaseRow, active.release_id)
                version = (
                    await session.get(SkillVersionRow, release.version_id)
                    if release is not None
                    else None
                )
                if release is None or version is None:
                    raise SkillConflict(
                        f"skill {skill_id!r} has a dangling public active pointer"
                    )
                self._outbox_row(
                    session,
                    op="upsert_active",
                    projection_key=f"public:{skill_id}",
                    scope="public",
                    owner_user_id=None,
                    skill_id=skill_id,
                    version_id=version.id,
                    payload=_projection_payload(
                        version, enabled=enabled, name=release.public_name
                    ),
                    now=now,
                )

            self._audit_row(
                session,
                actor=actor,
                action="skill.enable" if enabled else "skill.disable",
                skill_id=skill_id,
                detail={"enabled": enabled},
                now=now,
            )
            skill.updated_at = now
            await self._commit(session, f"skill setting for {skill_id!r} conflicts")

    # ------------------------------------------------------------------
    # draft staging
    # ------------------------------------------------------------------

    async def put_draft_file(
        self,
        skill_id: str,
        path: str,
        content: bytes,
        mode: str = "text",
    ) -> StagedFileInfo:
        content = bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)
        now = _now()
        async with self._sf() as session:
            row = await session.get(SkillDraftFileRow, (skill_id, path))
            if row is None:
                session.add(
                    SkillDraftFileRow(
                        skill_id=skill_id,
                        path=path,
                        content=content,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        mode=mode,
                        updated_at=now,
                    )
                )
            else:
                row.content = content
                row.sha256 = sha256
                row.size_bytes = size_bytes
                row.mode = mode
                row.updated_at = now
            await self._commit(session, f"draft file {path!r} for skill {skill_id!r} conflicts")
        return StagedFileInfo(
            path=path,
            size_bytes=size_bytes,
            mode=mode,
            sha256=sha256,
        )

    async def list_draft_files(self, skill_id: str) -> list[StagedFile]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(SkillDraftFileRow)
                    .where(SkillDraftFileRow.skill_id == skill_id)
                    .order_by(SkillDraftFileRow.path.asc())
                )
            ).all()
            return [
                StagedFile(
                    path=row.path,
                    content=bytes(row.content),
                    mode=row.mode,
                )
                for row in rows
            ]

    async def delete_draft_file(self, skill_id: str, path: str) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(SkillDraftFileRow).where(
                    SkillDraftFileRow.skill_id == skill_id,
                    SkillDraftFileRow.path == path,
                )
            )
            deleted = result.rowcount > 0
            await self._commit(
                session, f"draft file {path!r} for skill {skill_id!r} could not be deleted"
            )
            return deleted

    async def delete_all_draft_files(self, skill_id: str) -> None:
        async with self._sf() as session:
            await session.execute(
                delete(SkillDraftFileRow).where(SkillDraftFileRow.skill_id == skill_id)
            )
            await self._commit(
                session, f"draft files for skill {skill_id!r} could not be deleted"
            )

    # ------------------------------------------------------------------
    # projection outbox
    # ------------------------------------------------------------------

    async def enqueue_outbox(
        self,
        *,
        op: str,
        projection_key: str,
        scope: str,
        owner_user_id: str | None,
        skill_id: str,
        version_id: str | None,
        payload: dict,
    ) -> None:
        now = _now()
        async with self._sf() as session:
            self._outbox_row(
                session,
                op=op,
                projection_key=projection_key,
                scope=scope,
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                version_id=version_id,
                payload=payload,
                now=now,
            )
            await self._commit(session, "projection outbox event could not be enqueued")

    async def claim_outbox(self, limit: int = 20) -> list[OutboxEvent]:
        if limit <= 0:
            return []
        now = _now()
        async with self._sf() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                rows = (
                    await session.scalars(
                        select(SkillIndexOutboxRow)
                        .where(
                            SkillIndexOutboxRow.status == "pending",
                            SkillIndexOutboxRow.next_attempt_at <= now,
                        )
                        .order_by(
                            SkillIndexOutboxRow.created_at.asc(),
                            SkillIndexOutboxRow.id.asc(),
                        )
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.status = "running"
                    row.attempts += 1
                    row.updated_at = now
                events = [_outbox_event(row) for row in rows]
                await self._commit(session, "projection outbox claim conflicts")
                return events

            # SQLite has no FOR UPDATE.  Select candidates and then claim them
            # with a conditional UPDATE in the same transaction; a concurrent
            # caller that lost the race gets no rows back and never duplicates
            # an event.
            candidates = (
                await session.scalars(
                    select(SkillIndexOutboxRow)
                    .where(
                        SkillIndexOutboxRow.status == "pending",
                        SkillIndexOutboxRow.next_attempt_at <= now,
                    )
                    .order_by(
                        SkillIndexOutboxRow.created_at.asc(),
                        SkillIndexOutboxRow.id.asc(),
                    )
                    .limit(limit)
                )
            ).all()
            if not candidates:
                return []

            attempts = {row.id: row.attempts for row in candidates}
            result = await session.execute(
                update(SkillIndexOutboxRow)
                .where(
                    SkillIndexOutboxRow.id.in_([row.id for row in candidates]),
                    SkillIndexOutboxRow.status == "pending",
                )
                .values(
                    status="running",
                    attempts=SkillIndexOutboxRow.attempts + 1,
                    updated_at=now,
                )
                .returning(SkillIndexOutboxRow.id)
                .execution_options(synchronize_session=False)
            )
            claimed_ids = set(result.scalars().all())
            events = [
                OutboxEvent(
                    id=row.id,
                    op=row.op,
                    projection_key=row.projection_key,
                    scope=row.scope,
                    owner_user_id=row.owner_user_id,
                    skill_id=row.skill_id,
                    version_id=row.version_id,
                    payload=dict(row.payload_json or {}),
                    attempts=attempts[row.id] + 1,
                )
                for row in candidates
                if row.id in claimed_ids
            ]
            await self._commit(session, "projection outbox claim conflicts")
            return events

    async def finish_outbox(self, event_id: str) -> None:
        now = _now()
        async with self._sf() as session:
            row = await session.get(SkillIndexOutboxRow, event_id)
            if row is None:
                return
            row.status = "done"
            row.updated_at = now
            await self._commit(session, f"projection outbox event {event_id!r} conflicts")

    async def retry_outbox(
        self, event_id: str, error: str, delay_seconds: int
    ) -> None:
        now = _now()
        # A retry is always scheduled strictly in the future so an immediate
        # claim cannot spin on the same failed event.
        next_attempt_at = now + timedelta(seconds=max(1, int(delay_seconds)))
        async with self._sf() as session:
            row = await session.get(SkillIndexOutboxRow, event_id)
            if row is None:
                return
            row.status = "pending"
            row.last_error = error
            row.next_attempt_at = next_attempt_at
            row.updated_at = now
            await self._commit(session, f"projection outbox event {event_id!r} conflicts")

    async def pending_outbox_count(self) -> int:
        async with self._sf() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(SkillIndexOutboxRow)
                .where(SkillIndexOutboxRow.status == "pending")
            )
            return int(count or 0)

    # ------------------------------------------------------------------
    # visibility, projection confirmation, and audit
    # ------------------------------------------------------------------

    async def visible_for_user(self, user_id: str) -> list[VisibleSkill]:
        async with self._sf() as session:
            public_rows = (
                await session.execute(
                    select(
                        SkillReleaseRow,
                        SkillVersionRow,
                        SkillPublicActiveRow,
                        SkillRow,
                        SkillSettingsRow,
                    )
                    .join(SkillRow, SkillRow.id == SkillReleaseRow.skill_id)
                    .join(SkillVersionRow, SkillVersionRow.id == SkillReleaseRow.version_id)
                    .join(
                        SkillPublicActiveRow,
                        (SkillPublicActiveRow.skill_id == SkillRow.id)
                        & (SkillPublicActiveRow.release_id == SkillReleaseRow.id),
                    )
                    .outerjoin(SkillSettingsRow, SkillSettingsRow.skill_id == SkillRow.id)
                    .order_by(
                        func.coalesce(SkillRow.public_name, SkillRow.private_name).asc(),
                        SkillRow.id.asc(),
                    )
                )
            ).all()
            private_rows = (
                await session.execute(
                    select(SkillPrivateActiveRow, SkillVersionRow, SkillRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.id == SkillPrivateActiveRow.version_id,
                    )
                    .join(SkillRow, SkillRow.id == SkillPrivateActiveRow.skill_id)
                    .where(
                        SkillPrivateActiveRow.owner_user_id == user_id,
                        SkillRow.deleted_at.is_(None),
                    )
                    .order_by(SkillRow.private_name.asc(), SkillRow.id.asc())
                )
            ).all()

        visible = [
            _visible(
                skill,
                version,
                scope=SkillScope.PUBLIC,
                enabled=bool(settings.enabled) if settings is not None else True,
                release=release,
            )
            for release, version, _active, skill, settings in public_rows
        ]
        visible.extend(
            _visible(skill, version, scope=SkillScope.PRIVATE, enabled=True)
            for _active, version, skill in private_rows
        )
        return visible

    async def private_skill_ids(self, user_id: str) -> tuple[str, ...]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(SkillPrivateActiveRow.skill_id)
                    .join(SkillRow, SkillRow.id == SkillPrivateActiveRow.skill_id)
                    .where(
                        SkillPrivateActiveRow.owner_user_id == user_id,
                        SkillRow.deleted_at.is_(None),
                    )
                    .order_by(SkillPrivateActiveRow.skill_id.asc())
                )
            ).all()
            return tuple(rows)

    async def confirm_hits(
        self, user_id: str, hits: Sequence[ProjectionHit]
    ) -> list[VisibleSkill]:
        """Confirm projection hits against authoritative active pointers.

        This deliberately uses one join per visibility scope, never one query
        per hit.  A private hit is accepted only when its owner is the caller
        and its version matches the caller's current private active pointer.
        """
        candidates = list(hits)
        public_skill_ids = {
            hit.skill_id
            for hit in candidates
            if str(getattr(hit, "scope", "")) == "public"
        }
        private_skill_ids = {
            hit.skill_id
            for hit in candidates
            if str(getattr(hit, "scope", "")) == "private"
            and getattr(hit, "owner_user_id", None) == user_id
        }

        async with self._sf() as session:
            public_map: dict[
                str,
                tuple[SkillReleaseRow, SkillVersionRow, SkillRow, SkillSettingsRow | None],
            ] = {}
            if public_skill_ids:
                rows = (
                    await session.execute(
                        select(
                            SkillReleaseRow,
                            SkillVersionRow,
                            SkillPublicActiveRow,
                            SkillRow,
                            SkillSettingsRow,
                        )
                        .join(SkillRow, SkillRow.id == SkillReleaseRow.skill_id)
                        .join(
                            SkillVersionRow,
                            SkillVersionRow.id == SkillReleaseRow.version_id,
                        )
                        .join(
                            SkillPublicActiveRow,
                            (SkillPublicActiveRow.skill_id == SkillRow.id)
                            & (SkillPublicActiveRow.release_id == SkillReleaseRow.id),
                        )
                        .outerjoin(SkillSettingsRow, SkillSettingsRow.skill_id == SkillRow.id)
                        .where(SkillPublicActiveRow.skill_id.in_(public_skill_ids))
                    )
                ).all()
                for release, version, _active, skill, settings in rows:
                    public_map[skill.id] = (release, version, skill, settings)

            private_map: dict[str, tuple[SkillVersionRow, SkillRow]] = {}
            if private_skill_ids:
                rows = (
                    await session.execute(
                        select(SkillPrivateActiveRow, SkillVersionRow, SkillRow)
                        .join(
                            SkillVersionRow,
                            SkillVersionRow.id == SkillPrivateActiveRow.version_id,
                        )
                        .join(SkillRow, SkillRow.id == SkillPrivateActiveRow.skill_id)
                        .where(
                            SkillPrivateActiveRow.owner_user_id == user_id,
                            SkillPrivateActiveRow.skill_id.in_(private_skill_ids),
                            SkillRow.deleted_at.is_(None),
                        )
                    )
                ).all()
                for _active, version, skill in rows:
                    private_map[skill.id] = (version, skill)

        confirmed: list[VisibleSkill] = []
        seen: set[tuple[str, str, str]] = set()
        for hit in candidates:
            scope = str(getattr(hit, "scope", ""))
            skill_id = hit.skill_id
            version_id = hit.version_id
            if scope == "public":
                found = public_map.get(skill_id)
                if found is None:
                    continue
                release, version, skill, settings = found
                if version.id != version_id:
                    continue
                if settings is not None and not settings.enabled:
                    continue
                key = (scope, skill.id, version.id)
                if key in seen:
                    continue
                seen.add(key)
                confirmed.append(
                    _visible(
                        skill,
                        version,
                        scope=SkillScope.PUBLIC,
                        enabled=True,
                        release=release,
                    )
                )
            elif scope == "private":
                if getattr(hit, "owner_user_id", None) != user_id:
                    continue
                found = private_map.get(skill_id)
                if found is None:
                    continue
                version, skill = found
                if version.id != version_id:
                    continue
                key = (scope, skill.id, version.id)
                if key in seen:
                    continue
                seen.add(key)
                confirmed.append(
                    _visible(skill, version, scope=SkillScope.PRIVATE, enabled=True)
                )
        return confirmed

    async def audit(
        self,
        *,
        actor: str,
        action: str,
        skill_id: str | None = None,
        submission_id: str | None = None,
        release_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        async with self._sf() as session:
            self._audit_row(
                session,
                actor=actor,
                action=action,
                skill_id=skill_id,
                submission_id=submission_id,
                release_id=release_id,
                detail=detail,
                now=_now(),
            )
            await self._commit(session, "audit event could not be recorded")

    # ------------------------------------------------------------------
    # transaction helpers
    # ------------------------------------------------------------------

    async def _owned_skill(self, session, skill_id: str, owner_user_id: str) -> SkillRow:
        skill = await session.get(SkillRow, skill_id)
        if skill is None or skill.deleted_at is not None:
            raise SkillConflict(f"skill {skill_id!r} does not exist")
        if skill.owner_user_id != owner_user_id:
            raise SkillConflict(f"skill {skill_id!r} is not owned by {owner_user_id!r}")
        return skill

    async def _flush(self, session, message: str) -> None:
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise SkillConflict(message) from exc

    async def _commit(self, session, message: str) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise SkillConflict(message) from exc

    def _audit_row(
        self,
        session,
        *,
        actor: str,
        action: str,
        skill_id: str | None = None,
        submission_id: str | None = None,
        release_id: str | None = None,
        detail: dict | None = None,
        now: datetime,
    ) -> None:
        session.add(
            SkillAuditEventRow(
                actor=actor,
                action=action,
                skill_id=skill_id,
                submission_id=submission_id,
                release_id=release_id,
                detail_json=detail or {},
                created_at=now,
            )
        )

    def _outbox_row(
        self,
        session,
        *,
        op: str,
        projection_key: str,
        scope: str,
        owner_user_id: str | None,
        skill_id: str,
        version_id: str | None,
        payload: dict,
        now: datetime,
    ) -> None:
        _validate_outbox_payload(payload)
        session.add(
            SkillIndexOutboxRow(
                op=op,
                projection_key=projection_key,
                scope=scope,
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                version_id=version_id,
                payload_json=payload,
                status="pending",
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        )


__all__ = ["OutboxEvent", "SqlSkillStore"]
