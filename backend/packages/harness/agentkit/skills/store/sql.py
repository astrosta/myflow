"""SQL repository for the user-isolated Git-backed skill catalog.

Every public method opens exactly one short-lived session.  The lifecycle calls
that create or move user-visible state write the state change, active pointer
(when applicable), audit event and projection outbox row in one transaction, so
a worker can never observe a half-applied catalog change.

v2 has no submissions, releases or public active pointers.  Saving a version is
deliberately inert: only activate_version makes a version runtime-visible.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from agentkit.skills.domain import (
    PackageManifest,
    SecurityScanRecord,
    SkillConflict,
    SkillRecord,
    SkillScope,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
    VisibleSkill,
)
from agentkit.skills.store.model import (
    SkillAuditEventRow,
    SkillCatalogEpochRow,
    SkillDraftFileRow,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillRow,
    SkillSecurityScanRow,
    SkillVersionRow,
)
from agentkit.skills.types import SecretRequirement, Skill, SkillCategory

if TYPE_CHECKING:
    from agentkit.skills.projection import ProjectionHit


_MAX_OUTBOX_PAYLOAD_BYTES = 64 * 1024
_CATALOG_EPOCH_KEY = "catalog"


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


#: 允许写入 skill_versions.index_status 的取值。
_INDEX_STATUSES = frozenset({"unindexed", "pending", "ready", "failed", "disabled"})
#: 对账快路径只拾取这三种：ready 已就绪，disabled 是"本部署不建索引"。
_REPAIRABLE_INDEX_STATUSES = ("pending", "failed", "unindexed")


def _reset_index_repair(row: SkillVersionRow) -> None:
    """Clear the repair backoff: the version is being projected again."""
    row.index_attempts = 0
    row.index_last_error = None
    row.index_next_attempt_at = None

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
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_utc(row.updated_at),  # type: ignore[arg-type]
        deleted_at=_utc(row.deleted_at),
        source=row.source,
        source_uri=row.source_uri,
        enabled=bool(row.enabled),
        load_count=int(row.load_count or 0),
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


def _scan_record(row: SkillSecurityScanRow) -> SecurityScanRecord:
    return SecurityScanRecord(
        id=row.id,
        skill_id=row.skill_id,
        version_id=row.version_id,
        owner_user_id=row.owner_user_id,
        package_sha256=row.package_sha256,
        status=row.status,
        is_safe=bool(row.is_safe),
        max_severity=row.max_severity,
        findings_count=int(row.findings_count or 0),
        findings=list(row.findings_json or []),
        scanner=row.scanner,
        scanner_version=row.scanner_version,
        duration_ms=int(row.duration_ms or 0),
        error=row.error,
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
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


def _skill_for_visibility(skill: SkillRow, version: SkillVersionRow) -> Skill:
    metadata = dict(version.metadata_json or {})
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
        enabled=bool(skill.enabled),
        load_count=int(skill.load_count or 0),
        required_secrets=_required_secrets(metadata),
        secrets_autonomous=bool(metadata.get("secrets_autonomous", True)),
        argument_hint=metadata.get("argument_hint"),
        skill_id=skill.id,
        scope=SkillScope.PRIVATE,
        owner_user_id=skill.owner_user_id,
        version_id=version.id,
        skill_version=version.skill_version,
        git_commit=version.draft_commit,
        repo_path=version.skill_path,
    )


def _visible(skill: SkillRow, version: SkillVersionRow) -> VisibleSkill:
    return VisibleSkill(
        skill=_skill_for_visibility(skill, version),
        scope=SkillScope.PRIVATE,
        version=_version_record(version),
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
    enabled: bool = True,
) -> dict[str, Any]:
    metadata = dict(version.metadata_json or {})
    return {
        "enabled": enabled,
        "name": metadata.get("name") or version.skill_version,
        "description": metadata.get("description") or "",
        "skill_version": version.skill_version,
        "package_sha256": version.package_sha256,
        "skill_md_sha256": version.skill_md_sha256,
    }


def _projection_key(owner_user_id: str, skill_id: str) -> str:
    return f"private:{owner_user_id}:{skill_id}"


class SqlSkillStore:
    """Async SQL implementation of user-private skill identity and lifecycle."""

    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------
    # catalog generation (cross-worker invalidation)
    # ------------------------------------------------------------------

    async def catalog_epoch(self) -> int:
        """当前目录代数；单例行不存在时按 0 处理（空库/首次启动）。"""
        async with self._sf() as session:
            value = await session.scalar(
                select(SkillCatalogEpochRow.epoch).where(
                    SkillCatalogEpochRow.id == _CATALOG_EPOCH_KEY
                )
            )
            return int(value or 0)

    async def bump_catalog_epoch(self) -> int:
        """原子递增目录代数并返回新值，多 worker 并发安全。"""
        now = _now()
        async with self._sf() as session:
            result = await session.execute(
                update(SkillCatalogEpochRow)
                .where(SkillCatalogEpochRow.id == _CATALOG_EPOCH_KEY)
                .values(epoch=SkillCatalogEpochRow.epoch + 1, updated_at=now)
            )
            if result.rowcount:
                value = await session.scalar(
                    select(SkillCatalogEpochRow.epoch).where(
                        SkillCatalogEpochRow.id == _CATALOG_EPOCH_KEY
                    )
                )
                await self._commit(session, "catalog epoch could not be committed")
                return int(value or 0)

            session.add(
                SkillCatalogEpochRow(id=_CATALOG_EPOCH_KEY, epoch=1, updated_at=now)
            )
            try:
                await session.commit()
                return 1
            except IntegrityError:
                await session.rollback()
                await session.execute(
                    update(SkillCatalogEpochRow)
                    .where(SkillCatalogEpochRow.id == _CATALOG_EPOCH_KEY)
                    .values(epoch=SkillCatalogEpochRow.epoch + 1, updated_at=_now())
                )
                value = await session.scalar(
                    select(SkillCatalogEpochRow.epoch).where(
                        SkillCatalogEpochRow.id == _CATALOG_EPOCH_KEY
                    )
                )
                await self._commit(session, "catalog epoch could not be committed")
                return int(value or 0)

    # ------------------------------------------------------------------
    # identity and versions
    # ------------------------------------------------------------------

    async def set_skill_enabled(
        self,
        *,
        skill_id: str,
        owner_user_id: str,
        enabled: bool,
        index_enabled: bool = True,
    ) -> SkillRecord:
        """Persist the user switch for an active skill and refresh its projection."""
        now = _now()
        async with self._sf() as session:
            skill = await self._owned_skill(session, skill_id, owner_user_id)
            active = await session.get(SkillPrivateActiveRow, skill_id)
            if active is None:
                raise SkillConflict("only active skills can be enabled or disabled")
            skill.enabled = bool(enabled)
            skill.updated_at = now
            if index_enabled:
                version = await session.get(SkillVersionRow, active.version_id)
                if version is not None:
                    version.index_status = "pending"
                    # 重新入队：上次耗尽的退避不该继续挡住这条版本。
                    _reset_index_repair(version)
                    self._outbox_row(
                        session,
                        op="upsert_active",
                        projection_key=_projection_key(owner_user_id, skill_id),
                        scope=SkillScope.PRIVATE.value,
                        owner_user_id=owner_user_id,
                        skill_id=skill_id,
                        version_id=version.id,
                        payload=_projection_payload(version, enabled=bool(enabled)),
                        now=now,
                    )
            self._audit_row(
                session,
                actor=owner_user_id,
                action="skill.enabled",
                skill_id=skill_id,
                detail={"enabled": bool(enabled)},
                now=now,
            )
            await self._commit(session, f"skill {skill_id!r} enabled state conflicts")
            return _skill_record(skill)

    async def increment_skill_load_count(self, skill_id: str) -> int:
        """Increment and return one private skill's successful load counter."""
        async with self._sf() as session:
            result = await session.execute(
                update(SkillRow)
                .where(SkillRow.id == skill_id, SkillRow.deleted_at.is_(None))
                .values(load_count=SkillRow.load_count + 1)
                .returning(SkillRow.load_count)
            )
            value = result.scalar_one_or_none()
            await self._commit(session, f"skill {skill_id!r} load count conflicts")
            return int(value or 0)

    async def list_skill_load_counts(self) -> list[tuple[str, str, int]]:
        """Return (owner_user_id, skill_id, load_count) for Redis startup seeding."""
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(
                        SkillRow.owner_user_id,
                        SkillRow.id,
                        SkillRow.load_count,
                    )
                    .where(
                        SkillRow.deleted_at.is_(None),
                        SkillRow.load_count > 0,
                    )
                    .order_by(SkillRow.owner_user_id.asc(), SkillRow.id.asc())
                )
            ).all()
            return [
                (str(owner), str(skill_id), int(count or 0))
                for owner, skill_id, count in rows
            ]

    async def create_skill(
        self,
        *,
        owner_user_id: str,
        private_name: str,
        source: str = "manual",
        source_uri: str | None = None,
    ) -> SkillRecord:
        if not private_name or not private_name.strip():
            raise ValueError("private_name must not be blank")
        now = _now()
        row = SkillRow(
            owner_user_id=owner_user_id,
            private_name=private_name,
            source=source,
            source_uri=source_uri,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await self._flush(session, f"skill {private_name!r} already exists for this owner")
            record = _skill_record(row)
            await self._commit(session, f"skill {private_name!r} already exists for this owner")
            return record

    async def get_skill(
        self, skill_id: str, *, include_deleted: bool = False
    ) -> SkillRecord | None:
        conditions = [SkillRow.id == skill_id]
        if not include_deleted:
            conditions.append(SkillRow.deleted_at.is_(None))
        async with self._sf() as session:
            row = await session.scalar(select(SkillRow).where(*conditions))
            return _skill_record(row) if row is not None else None

    async def revive_private_draft(
        self, *, skill_id: str, owner_user_id: str
    ) -> bool:
        """Clear the soft-delete marker so an owner can reopen their skill."""
        async with self._sf() as session:
            row = await session.scalar(
                select(SkillRow).where(
                    SkillRow.id == skill_id,
                    SkillRow.owner_user_id == owner_user_id,
                )
            )
            if row is None:
                return False
            if row.deleted_at is not None:
                row.deleted_at = None
                row.updated_at = _now()
                await self._commit(
                    session, f"private draft for skill {skill_id!r} could not be revived"
                )
            return True

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
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> SkillVersionRecord:
        """Record an immutable Git-backed draft version.

        Saving is deliberately inert: it never moves the active pointer or
        enqueues indexing.  Only activate_version makes a version visible.
        """
        now = _now()
        async with self._sf() as session:
            skill = await self._owned_skill(session, skill_id, owner_user_id)
            version = await session.scalar(
                select(SkillVersionRow).where(
                    SkillVersionRow.skill_id == skill_id,
                    SkillVersionRow.package_sha256 == manifest.package_sha256,
                )
            )
            if version is not None and extra_metadata:
                merged = {**dict(version.metadata_json or {}), **dict(extra_metadata)}
                if merged != dict(version.metadata_json or {}):
                    version.metadata_json = merged
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
                    metadata_json={**_metadata(manifest), **dict(extra_metadata or {})},
                    draft_ref=draft_ref,
                    draft_commit=draft_commit,
                    skill_path=skill_path,
                    index_status="unindexed",
                    created_at=now,
                )
                session.add(version)
                await self._flush(session, f"skill {skill_id!r} version could not be allocated")

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
            skill.updated_at = now

            record = _version_record(version)
            await self._commit(session, f"skill {skill_id!r} version could not be allocated")
            return record

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

    async def list_all_versions(self) -> list[SkillVersionRecord]:
        """Return every version for retention and reconciliation scans."""
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(SkillVersionRow).order_by(
                        SkillVersionRow.skill_id.asc(),
                        SkillVersionRow.version_seq.asc(),
                        SkillVersionRow.id.asc(),
                    )
                )
            ).all()
            return [_version_record(row) for row in rows]

    async def latest_version(self, skill_id: str) -> SkillVersionRecord | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(SkillVersionRow)
                .where(SkillVersionRow.skill_id == skill_id)
                .order_by(
                    SkillVersionRow.version_seq.desc(),
                    SkillVersionRow.id.desc(),
                )
                .limit(1)
            )
            return _version_record(row) if row is not None else None

    async def get_version(
        self, skill_id: str, version_id: str
    ) -> SkillVersionRecord | None:
        async with self._sf() as session:
            row = await session.get(SkillVersionRow, version_id)
            if row is None or row.skill_id != skill_id:
                return None
            return _version_record(row)

    async def set_version_index_status(self, version_id: str, status: str) -> None:
        """Update only the mutable projection status of one immutable version."""
        if status not in _INDEX_STATUSES:
            raise ValueError(f"unsupported index status: {status!r}")
        async with self._sf() as session:
            row = await session.get(SkillVersionRow, version_id)
            if row is None:
                return
            row.index_status = status
            if status != "failed":
                # 新一轮投影（生效/开关/成功）重新开始计数，旧退避不该拖住它。
                _reset_index_repair(row)
            await self._commit(
                session, f"skill version {version_id!r} index status conflicts"
            )

    async def list_index_repair_candidates(
        self,
        *,
        limit: int,
        max_attempts: int,
        now: datetime | None = None,
    ) -> list[VisibleSkill]:
        """Active versions whose projection is not ready and whose backoff elapsed.

        This is the only query the periodic repair pass runs: index_status is the
        authority for "should this version be in Milvus", so a healthy catalog costs
        one indexed lookup and nothing else.  max_attempts keeps rows that keep
        failing from starving the rest of the queue.
        """
        if limit <= 0:
            return []
        moment = now or _now()
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(SkillPrivateActiveRow, SkillVersionRow, SkillRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.id == SkillPrivateActiveRow.version_id,
                    )
                    .join(SkillRow, SkillRow.id == SkillPrivateActiveRow.skill_id)
                    .where(
                        SkillRow.deleted_at.is_(None),
                        SkillVersionRow.index_status.in_(_REPAIRABLE_INDEX_STATUSES),
                        SkillVersionRow.index_attempts < int(max_attempts),
                        or_(
                            SkillVersionRow.index_next_attempt_at.is_(None),
                            SkillVersionRow.index_next_attempt_at <= moment,
                        ),
                    )
                    .order_by(
                        SkillVersionRow.index_next_attempt_at.asc().nullsfirst(),
                        SkillVersionRow.version_seq.asc(),
                        SkillVersionRow.id.asc(),
                    )
                    .limit(int(limit))
                )
            ).all()
        return [_visible(skill, version) for _active, version, skill in rows]

    async def mark_version_indexed(self, version_id: str) -> None:
        """The projection row is in Milvus: ready, and the backoff state is cleared."""
        async with self._sf() as session:
            row = await session.get(SkillVersionRow, version_id)
            if row is None:
                return
            row.index_status = "ready"
            _reset_index_repair(row)
            await self._commit(
                session, f"skill version {version_id!r} index status conflicts"
            )

    async def mark_version_index_failed(
        self,
        version_id: str,
        *,
        error: str,
        max_attempts: int,
        base_seconds: float = 60.0,
        cap_seconds: float = 3600.0,
        now: datetime | None = None,
    ) -> int:
        """Record one failed repair and schedule the next attempt. Returns attempts."""
        moment = now or _now()
        async with self._sf() as session:
            row = await session.get(SkillVersionRow, version_id)
            if row is None:
                return 0
            attempts = int(row.index_attempts or 0) + 1
            row.index_status = "failed"
            row.index_attempts = attempts
            row.index_last_error = str(error or "")[:255]
            if attempts >= int(max_attempts):
                # 自动重试到此为止；运维处理或重新生效才会复活它。
                row.index_next_attempt_at = None
            else:
                delay = min(
                    float(cap_seconds),
                    float(base_seconds) * 2 ** max(0, attempts - 1),
                )
                row.index_next_attempt_at = moment + timedelta(seconds=delay)
            await self._commit(
                session, f"skill version {version_id!r} index status conflicts"
            )
            return attempts

    # ------------------------------------------------------------------
    # security scans
    # ------------------------------------------------------------------

    async def record_security_scan(
        self,
        *,
        skill_id: str,
        owner_user_id: str,
        version_id: str,
        status: str,
        is_safe: bool,
        max_severity: str,
        findings: Sequence[Mapping[str, Any]],
        scanner: str,
        scanner_version: str | None,
        duration_ms: int,
        error: str | None = None,
    ) -> SecurityScanRecord:
        """Persist one scan result bound to the immutable version content."""
        if status not in {"completed", "failed"}:
            raise ValueError(f"unsupported scan status: {status!r}")
        normalized = [dict(item) for item in findings]
        now = _now()
        async with self._sf() as session:
            skill = await self._owned_skill(session, skill_id, owner_user_id)
            version = await session.get(SkillVersionRow, version_id)
            if version is None or version.skill_id != skill_id:
                raise SkillConflict(
                    f"version {version_id!r} does not belong to skill {skill_id!r}"
                )
            row = SkillSecurityScanRow(
                skill_id=skill_id,
                version_id=version_id,
                owner_user_id=owner_user_id,
                package_sha256=version.package_sha256,
                status=status,
                is_safe=bool(is_safe),
                max_severity=max_severity,
                findings_count=len(normalized),
                findings_json=normalized,
                scanner=scanner,
                scanner_version=scanner_version,
                duration_ms=max(0, int(duration_ms)),
                error=error,
                created_at=now,
            )
            session.add(row)
            await self._flush(session, f"security scan for skill {skill_id!r} conflicts")
            self._audit_row(
                session,
                actor=owner_user_id,
                action="skill.scan",
                skill_id=skill_id,
                detail={
                    "scan_id": row.id,
                    "version_id": version_id,
                    "status": status,
                    "is_safe": bool(is_safe),
                    "max_severity": max_severity,
                    "findings_count": len(normalized),
                },
                now=now,
            )
            skill.updated_at = now
            record = _scan_record(row)
            await self._commit(session, f"security scan for skill {skill_id!r} conflicts")
            return record

    async def latest_security_scan(
        self, skill_id: str, version_id: str | None = None
    ) -> SecurityScanRecord | None:
        async with self._sf() as session:
            query = select(SkillSecurityScanRow).where(
                SkillSecurityScanRow.skill_id == skill_id
            )
            if version_id is not None:
                query = query.where(SkillSecurityScanRow.version_id == version_id)
            row = await session.scalar(
                query.order_by(
                    SkillSecurityScanRow.created_at.desc(),
                    SkillSecurityScanRow.id.desc(),
                ).limit(1)
            )
            return _scan_record(row) if row is not None else None

    async def list_security_scans(
        self, skill_id: str, *, limit: int = 20
    ) -> list[SecurityScanRecord]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(SkillSecurityScanRow)
                    .where(SkillSecurityScanRow.skill_id == skill_id)
                    .order_by(
                        SkillSecurityScanRow.created_at.desc(),
                        SkillSecurityScanRow.id.desc(),
                    )
                    .limit(max(1, min(int(limit), 100)))
                )
            ).all()
            return [_scan_record(row) for row in rows]

    async def security_scan(self, scan_id: str) -> SecurityScanRecord | None:
        async with self._sf() as session:
            row = await session.get(SkillSecurityScanRow, scan_id)
            return _scan_record(row) if row is not None else None

    # ------------------------------------------------------------------
    # explicit activation
    # ------------------------------------------------------------------

    async def activate_version(
        self,
        *,
        skill_id: str,
        owner_user_id: str,
        version_id: str,
        scan_id: str,
        actor: str,
        index_enabled: bool = True,
    ) -> SkillVersionRecord:
        """Point the user's runtime at one scanned version and enqueue indexing.

        The scan, version, owner, package hash and latest-version constraints are
        re-checked inside this transaction.  The caller cannot activate a stale
        scan or an older version that has since been superseded.
        """
        now = _now()
        async with self._sf() as session:
            skill = await self._owned_skill(session, skill_id, owner_user_id)
            version = await session.get(SkillVersionRow, version_id)
            if version is None or version.skill_id != skill_id:
                raise SkillConflict(
                    f"version {version_id!r} does not belong to skill {skill_id!r}"
                )
            latest_seq = await session.scalar(
                select(func.max(SkillVersionRow.version_seq)).where(
                    SkillVersionRow.skill_id == skill_id
                )
            )
            if int(latest_seq or 0) != int(version.version_seq):
                raise SkillConflict(
                    f"version {version.skill_version!r} is not the latest saved version"
                )

            scan = await session.get(SkillSecurityScanRow, scan_id)
            if (
                scan is None
                or scan.skill_id != skill_id
                or scan.version_id != version.id
                or scan.owner_user_id != owner_user_id
            ):
                raise SkillConflict("security scan does not belong to this version")
            if scan.status != "completed":
                raise SkillConflict("security scan did not complete successfully")
            if not scan.is_safe:
                raise SkillConflict(
                    "security scan did not pass; fix the findings and scan again"
                )
            if scan.package_sha256 != version.package_sha256:
                raise SkillConflict("security scan was produced for different content")
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

            version.index_status = "pending" if index_enabled else "disabled"
            _reset_index_repair(version)
            if index_enabled:
                self._outbox_row(
                    session,
                    op="upsert_active",
                    projection_key=_projection_key(owner_user_id, skill_id),
                    scope=SkillScope.PRIVATE.value,
                    owner_user_id=owner_user_id,
                    skill_id=skill_id,
                    version_id=version.id,
                    payload=_projection_payload(version, enabled=bool(skill.enabled)),
                    now=now,
                )
            self._audit_row(
                session,
                actor=actor,
                action="skill.activate",
                skill_id=skill_id,
                detail={
                    "version_id": version.id,
                    "version": version.skill_version,
                    "scan_id": scan.id,
                    "package_sha256": version.package_sha256,
                },
                now=now,
            )
            skill.updated_at = now
            record = _version_record(version)
            await self._commit(session, f"skill {skill_id!r} activation conflicts")
            return record

    async def private_active(self, skill_id: str) -> SkillVersionRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillVersionRow)
                    .join(
                        SkillPrivateActiveRow,
                        SkillPrivateActiveRow.version_id == SkillVersionRow.id,
                    )
                    .where(SkillPrivateActiveRow.skill_id == skill_id)
                    .limit(1)
                )
            ).first()
            return _version_record(row[0]) if row is not None else None

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

    async def delete_private_draft(
        self, *, skill_id: str, owner_user_id: str, actor: str
    ) -> bool:
        """Delete one owner's private skill and queue its projection removal.

        Versions and scan history stay in PostgreSQL for audit; the owner,
        active pointer and draft staging are removed from every runtime read.
        """
        now = _now()
        async with self._sf() as session:
            skill = await session.scalar(
                select(SkillRow).where(
                    SkillRow.id == skill_id,
                    SkillRow.owner_user_id == owner_user_id,
                    SkillRow.deleted_at.is_(None),
                )
            )
            if skill is None:
                return False

            await session.execute(
                delete(SkillDraftFileRow).where(
                    SkillDraftFileRow.skill_id == skill_id
                )
            )
            await session.execute(
                delete(SkillPrivateActiveRow).where(
                    SkillPrivateActiveRow.skill_id == skill_id,
                    SkillPrivateActiveRow.owner_user_id == owner_user_id,
                )
            )

            skill.deleted_at = now
            skill.updated_at = now

            self._outbox_row(
                session,
                op="delete_active",
                projection_key=_projection_key(owner_user_id, skill_id),
                scope=SkillScope.PRIVATE.value,
                owner_user_id=owner_user_id,
                skill_id=skill_id,
                version_id=None,
                payload={},
                now=now,
            )
            self._audit_row(
                session,
                actor=actor,
                action="skill.delete",
                skill_id=skill_id,
                detail={},
                now=now,
            )
            await self._commit(
                session, f"private draft for skill {skill_id!r} could not be deleted"
            )
            return True

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

    async def list_pending_outbox(self) -> list[OutboxEvent]:
        """Return pending events for retention keep-set decisions."""
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(SkillIndexOutboxRow)
                    .where(SkillIndexOutboxRow.status == "pending")
                    .order_by(
                        SkillIndexOutboxRow.created_at.asc(),
                        SkillIndexOutboxRow.id.asc(),
                    )
                )
            ).all()
            return [_outbox_event(row) for row in rows]

    # ------------------------------------------------------------------
    # visibility and projection confirmation
    # ------------------------------------------------------------------

    async def visible_for_user(self, user_id: str) -> list[VisibleSkill]:
        async with self._sf() as session:
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
                        SkillRow.owner_user_id == user_id,
                        SkillRow.deleted_at.is_(None),
                        SkillRow.enabled.is_(True),
                    )
                    .order_by(SkillRow.private_name.asc(), SkillRow.id.asc())
                )
            ).all()
        return [_visible(skill, version) for _active, version, skill in rows]

    async def list_active_visible(self) -> list[VisibleSkill]:
        """Return every owner's active private skill for reconciliation."""
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(SkillPrivateActiveRow, SkillVersionRow, SkillRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.id == SkillPrivateActiveRow.version_id,
                    )
                    .join(SkillRow, SkillRow.id == SkillPrivateActiveRow.skill_id)
                    .where(SkillRow.deleted_at.is_(None))
                    .order_by(
                        SkillPrivateActiveRow.owner_user_id.asc(),
                        SkillRow.private_name.asc(),
                        SkillRow.id.asc(),
                    )
                )
            ).all()
        return [_visible(skill, version) for _active, version, skill in rows]

    async def all_active(self) -> list[VisibleSkill]:
        """Snapshot refresh alias: every owner's active private pointer."""
        return await self.list_active_visible()

    async def get_active_visible(
        self,
        *,
        scope: str | SkillScope,
        skill_id: str,
        owner_user_id: str | None = None,
    ) -> VisibleSkill | None:
        """Re-read one projection target from the authoritative active pointer."""
        if str(scope) != SkillScope.PRIVATE.value or owner_user_id is None:
            return None
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillPrivateActiveRow, SkillVersionRow, SkillRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.id == SkillPrivateActiveRow.version_id,
                    )
                    .join(SkillRow, SkillRow.id == SkillPrivateActiveRow.skill_id)
                    .where(
                        SkillPrivateActiveRow.skill_id == skill_id,
                        SkillPrivateActiveRow.owner_user_id == owner_user_id,
                        SkillRow.owner_user_id == owner_user_id,
                        SkillRow.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            _active, version, skill = row
            return _visible(skill, version)

    async def confirm_hits(
        self, user_id: str, hits: Sequence[ProjectionHit]
    ) -> list[VisibleSkill]:
        """Confirm projection hits against the caller's active pointers.

        A hit is accepted only when it says private, belongs to the caller and
        names the version currently in the caller's active pointer.  One query
        confirms the whole hit set.
        """
        candidates = {
            hit.skill_id: hit.version_id
            for hit in hits
            if str(getattr(hit, "scope", "")) == SkillScope.PRIVATE.value
            and getattr(hit, "owner_user_id", None) == user_id
        }
        if not candidates:
            return []
        async with self._sf() as session:
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
                        SkillPrivateActiveRow.skill_id.in_(candidates),
                        SkillRow.owner_user_id == user_id,
                        SkillRow.deleted_at.is_(None),
                        SkillRow.enabled.is_(True),
                    )
                )
            ).all()
        confirmed: list[VisibleSkill] = []
        for active, version, skill in rows:
            if candidates.get(active.skill_id) != version.id:
                continue
            confirmed.append(_visible(skill, version))
        confirmed.sort(key=lambda item: (item.skill.name, item.version.skill_id))
        return confirmed

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    async def audit(
        self,
        *,
        actor: str,
        action: str,
        skill_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        async with self._sf() as session:
            self._audit_row(
                session,
                actor=actor,
                action=action,
                skill_id=skill_id,
                detail=dict(detail or {}),
                now=_now(),
            )
            await self._commit(session, "skill audit event could not be recorded")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _owned_skill(
        self, session, skill_id: str, owner_user_id: str
    ) -> SkillRow:
        skill = await session.scalar(
            select(SkillRow).where(
                SkillRow.id == skill_id,
                SkillRow.owner_user_id == owner_user_id,
                SkillRow.deleted_at.is_(None),
            )
        )
        if skill is None:
            raise SkillConflict(f"skill {skill_id!r} does not exist")
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
        skill_id: str | None,
        detail: dict[str, Any],
        now: datetime,
    ) -> None:
        session.add(
            SkillAuditEventRow(
                actor=actor,
                action=action,
                skill_id=skill_id,
                detail_json=detail,
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
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        if op not in {"upsert_active", "delete_active"}:
            raise ValueError(f"unsupported outbox operation: {op!r}")
        if scope != SkillScope.PRIVATE.value:
            raise ValueError("only private skill projections are supported")
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
