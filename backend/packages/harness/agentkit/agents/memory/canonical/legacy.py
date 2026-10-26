"""Read-only migration from the legacy LangGraph BaseStore.

The migrator preserves legacy memory ids, writes the canonical tables through
the repository, and never copies raw transcript/tool output. Re-running it is
idempotent by memory id.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agentkit.agents.memory.canonical.domain import MutationContext, RecordDraft
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.types import MemoryKind, MemoryQuery, MemoryScope


@dataclass(frozen=True, slots=True)
class MigrationReport:
    scanned: int = 0
    migrated: int = 0
    skipped: int = 0
    errors: int = 0
    dry_run: bool = True


class LegacyMigrator:
    def __init__(self, legacy_store: Any, repo: MemoryRepository) -> None:
        self._legacy = legacy_store
        self._repo = repo

    async def migrate(
        self, *, user_id: str | None = None, dry_run: bool = True
    ) -> MigrationReport:
        scopes = await asyncio.to_thread(self._scopes, user_id)
        scanned = migrated = skipped = errors = 0
        for scope in scopes:
            profile = await asyncio.to_thread(
                self._legacy.get_record, scope, MemoryKind.PROFILE, "profile"
            )
            if profile is not None:
                scanned += 1
                existing = await self._repo.get_profile(scope.user_id)
                if existing is None:
                    if not dry_run:
                        await self._repo.cas_profile(
                            scope.user_id,
                            expected_revision=0,
                            profile=dict(profile.structured or {}),
                            tombstones={},
                            mutation=MutationContext(
                                "SYSTEM", "legacy-migration", "ADD", "migrate", ()
                            ),
                        )
                    migrated += 1
                else:
                    skipped += 1

            page = await asyncio.to_thread(
                self._legacy.search_records,
                scope,
                MemoryQuery(include_history=True, limit=100, sort="recency"),
            )
            while True:
                for hit in page.items:
                    record = getattr(hit, "record", hit)
                    scanned += 1
                    if record.kind == MemoryKind.PROFILE:
                        continue
                    if await self._repo.get_record(scope.user_id, record.id) is not None:
                        skipped += 1
                        continue
                    try:
                        if not dry_run:
                            await self._repo.create_record(
                                _draft(scope.user_id, record),
                                mutation=MutationContext(
                                    "SYSTEM",
                                    "legacy-migration",
                                    "ADD",
                                    "migrate",
                                    tuple(getattr(record, "source_episode_ids", ()) or ()),
                                ),
                            )
                        migrated += 1
                    except Exception:
                        errors += 1
                if not page.next_cursor:
                    break
                page = await asyncio.to_thread(
                    self._legacy.search_records,
                    scope,
                    MemoryQuery(
                        include_history=True,
                        limit=100,
                        cursor=page.next_cursor,
                        sort="recency",
                    ),
                )
        return MigrationReport(
            scanned=scanned,
            migrated=migrated,
            skipped=skipped,
            errors=errors,
            dry_run=dry_run,
        )

    def _scopes(self, user_id: str | None) -> list[MemoryScope]:
        if user_id is not None:
            return [MemoryScope(user_id)]
        page = self._legacy.list_scopes(limit=1000)
        return list(page.items)


def _draft(user_id: str, record: Any) -> RecordDraft:
    structured = dict(record.structured or {})
    return RecordDraft(
        user_id=user_id,
        id=record.id,
        kind=record.kind,
        subtype=record.subtype,
        status=record.status,
        content=record.content,
        payload=structured,
        topic_key=record.topic_key,
        scope_condition=getattr(record, "scope_condition", None) or "global",
        source=record.source,
        evidence_tier=getattr(record, "evidence_tier", None),
        confidence=float(getattr(record, "confidence", 0.5)),
        salience=float(getattr(record, "salience", 0.5)),
        explicit_remember=bool(getattr(record, "explicit_remember", False)),
        parent_memory_id=getattr(record, "parent_memory_id", None),
        correction_of=getattr(record, "correction_of", None),
        valid_from=getattr(record, "valid_from", None),
        valid_to=getattr(record, "valid_to", None),
        occurred_at=getattr(record, "occurred_at", None),
        thread_id=getattr(record, "thread_id", None),
        agent_name=getattr(record, "agent_name", None),
        provenance=dict(getattr(record, "provenance", {}) or {}),
        source_episode_ids=tuple(getattr(record, "source_episode_ids", ()) or ()),
    )
