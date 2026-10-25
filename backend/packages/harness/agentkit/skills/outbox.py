"""Projection outbox worker: turn catalog commits into active Milvus rows.

PostgreSQL stays authoritative.  A claimed outbox event is only a hint: the
worker re-reads the current active pointer and settings before embedding, so a
stale event is marked done instead of overwriting a newer projection.  Milvus
writes never happen while a PostgreSQL transaction is open.

The active collection has one row per active public or private skill.  When
several pending events target the same projection key, only the newest is
processed; the older events are settled as no-ops.
"""

from __future__ import annotations

import asyncio
import logging

from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import SkillScope, VisibleSkill
from agentkit.skills.projection import ProjectionRow, SkillProjectionStore
from agentkit.skills.router import Embedder
from agentkit.skills.store.sql import OutboxEvent, SqlSkillStore

logger = logging.getLogger(__name__)

#: A failed event is retried until it has been attempted this many times.
_MAX_ATTEMPTS = 5
_MAX_RETRY_DELAY_SECONDS = 60
_INDEX_READY = "ready"
_INDEX_FAILED = "failed"


def _scope_value(scope: SkillScope | str) -> str:
    return scope.value if isinstance(scope, SkillScope) else str(scope)


def _projection_key(source: VisibleSkill) -> str:
    skill_id = source.version.skill_id
    if source.scope is SkillScope.PUBLIC:
        return f"public:{skill_id}"
    owner = source.skill.owner_user_id
    if not owner:
        raise ValueError("private active skill has no owner")
    return f"private:{owner}:{skill_id}"


def _remember_commit(commits: set[str], commit: str | None) -> None:
    """Add a commit in the same normalized form used by the content cache."""
    if commit:
        commits.add(commit.lower())


def _source_for_retention(source: VisibleSkill) -> set[str]:
    """Return commits that must survive while this active row exists."""
    commits: set[str] = set()
    _remember_commit(commits, source.version.draft_commit)
    if source.scope is SkillScope.PUBLIC and source.release is not None:
        _remember_commit(commits, source.release.git_commit)
    return commits


async def _projection_row(
    content: SkillContentStore,
    embedder: Embedder,
    source: VisibleSkill,
) -> ProjectionRow:
    """Build one active row from a freshly read active pointer."""
    scope = _scope_value(source.scope)
    version = source.version
    if scope == SkillScope.PUBLIC.value:
        release = source.release
        if release is None:
            raise ValueError("public active skill has no release")
        commit = release.git_commit
        path = release.skill_path
        name = release.public_name
        owner_user_id = None
        enabled = bool(source.skill.enabled)
    else:
        commit = version.draft_commit
        path = version.skill_path
        name = source.skill.name
        owner_user_id = source.skill.owner_user_id
        enabled = True
    if not commit:
        raise ValueError("active skill has no content commit")

    body = await content.read_text(commit, path)
    text = f"{name}\n{source.skill.description}"
    if body:
        text = f"{text}\n\n{body}"
    vectors = await embedder.embed_documents([text])
    if len(vectors) != 1:
        raise ValueError("embedding provider returned the wrong document count")
    embedding = list(vectors[0])
    if not embedding:
        raise ValueError("embedding provider returned an empty vector")

    return ProjectionRow(
        projection_key=_projection_key(source),
        scope=scope,
        owner_user_id=owner_user_id,
        skill_id=version.skill_id,
        version_id=version.id,
        skill_version=version.skill_version,
        enabled=enabled,
        name=name,
        description=source.skill.description,
        package_sha256=version.package_sha256,
        skill_md_sha256=version.skill_md_sha256,
        embedding=embedding,
    )


def _matches_event(source: VisibleSkill | None, event: OutboxEvent) -> bool:
    if source is None:
        return False
    if _projection_key(source) != event.projection_key:
        return False
    if source.version.id != event.version_id:
        return False
    enabled = (
        bool(source.skill.enabled)
        if _scope_value(source.scope) == SkillScope.PUBLIC.value
        else True
    )
    return event.payload.get("enabled") is enabled


async def _active_source(
    store: SqlSkillStore, event: OutboxEvent
) -> VisibleSkill | None:
    return await store.get_active_visible(
        scope=event.scope,
        skill_id=event.skill_id,
        owner_user_id=event.owner_user_id,
    )


async def build_projection_row(
    store: SqlSkillStore,
    content: SkillContentStore,
    embedder: Embedder,
    event: OutboxEvent,
) -> ProjectionRow | None:
    """Return the row for a still-current upsert, or None when it is stale."""
    if event.op != "upsert_active":
        return None
    source = await _active_source(store, event)
    if source is None or not _matches_event(source, event):
        return None
    row = await _projection_row(content, embedder, source)
    # Embedding can take long enough for a newer save to win.  Re-check the
    # authoritative pointer immediately before the caller writes to Milvus.
    if not _matches_event(await _active_source(store, event), event):
        return None
    return row


class SkillIndexOutboxWorker:
    """Drain the PG outbox into the active projection, plus repair and prune."""

    def __init__(
        self,
        store: SqlSkillStore,
        content: SkillContentStore,
        embedder: Embedder,
        projection: SkillProjectionStore,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._store = store
        self._content = content
        self._embedder = embedder
        self._projection = projection
        self._poll_seconds = float(poll_seconds)
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # outbox
    # ------------------------------------------------------------------

    async def run_once(self, limit: int = 20) -> int:
        """Claim and settle at most one outbox batch.

        Older events for one projection key are settled before the newest is
        processed, so a burst of saves does not produce a burst of embeddings.
        """
        events = await self._store.claim_outbox(limit)
        if not events:
            return 0

        newest: dict[str, OutboxEvent] = {}
        for event in events:
            previous = newest.get(event.projection_key)
            if previous is not None:
                await self._store.finish_outbox(previous.id)
                logger.info(
                    "Skipping superseded outbox event %s for %s",
                    previous.id,
                    previous.projection_key,
                )
            newest[event.projection_key] = event

        for event in newest.values():
            await self._process_event(event)
        return len(events)

    async def _process_event(self, event: OutboxEvent) -> None:
        try:
            if event.op == "delete_active":
                current = await self._store.get_active_visible(
                    scope=event.scope,
                    skill_id=event.skill_id,
                    owner_user_id=event.owner_user_id,
                )
                if current is not None:
                    logger.info(
                        "Skipping stale delete event %s for %s",
                        event.id,
                        event.projection_key,
                    )
                else:
                    await self._projection.delete_active(event.projection_key)
            elif event.op == "upsert_active":
                row = await build_projection_row(
                    self._store, self._content, self._embedder, event
                )
                if row is None:
                    logger.info(
                        "Skipping stale upsert event %s for %s",
                        event.id,
                        event.projection_key,
                    )
                else:
                    # The provider and the projection run outside every PG
                    # transaction; only the short status write follows.
                    await self._projection.upsert_active(row)
                    await self._store.set_version_index_status(row.version_id, _INDEX_READY)
            else:
                logger.warning(
                    "Skipping unknown outbox operation %s on event %s",
                    event.op,
                    event.id,
                )
            await self._store.finish_outbox(event.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every provider failure is retryable
            await self._fail_event(event, exc)

    async def _fail_event(self, event: OutboxEvent, exc: Exception) -> None:
        if event.attempts >= _MAX_ATTEMPTS:
            if event.op == "upsert_active" and event.version_id:
                try:
                    await self._store.set_version_index_status(
                        event.version_id, _INDEX_FAILED
                    )
                except Exception:  # noqa: BLE001 - preserve the terminal settle
                    logger.warning(
                        "Could not mark version %s failed after terminal event %s",
                        event.version_id,
                        event.id,
                    )
            await self._store.finish_outbox(event.id)
            logger.error(
                "Outbox event %s exhausted retries for version %s (%s)",
                event.id,
                event.version_id or "-",
                type(exc).__name__,
            )
            return

        delay = min(
            _MAX_RETRY_DELAY_SECONDS,
            2 ** max(0, event.attempts - 1),
        )
        await self._store.retry_outbox(event.id, type(exc).__name__, delay)
        logger.warning(
            "Outbox event %s failed for version %s (%s); retrying in %ss",
            event.id,
            event.version_id or "-",
            type(exc).__name__,
            delay,
        )

    # ------------------------------------------------------------------
    # repair and retention
    # ------------------------------------------------------------------

    async def reconcile(self, *, force: bool = False) -> int:
        """Make the projection equal the current active pointers.

        Existing projection keys are read first, then every current active row
        is checked against them, and every key without a current pointer is
        deleted.

        Steady state must be cheap: re-embedding the whole catalog every tick
        would cost real money for no benefit, so a row is left alone when its
        key already exists **and** its version is already indexed.  A key that
        disappeared from Milvus, or a version that never reached ``ready``
        (a lost outbox event, an exhausted retry, a wiped collection), is
        repaired.  ``force=True`` redoes every row, for operator repair after
        editing the collection by hand.
        """
        existing = await self._projection.list_projection_keys()
        sources = await self._store.list_active_visible()
        active_keys: set[str] = set()
        present = set(existing)
        touched = 0

        for source in sources:
            key = _projection_key(source)
            active_keys.add(key)
            if (
                not force
                and key in present
                and source.version.index_status == _INDEX_READY
            ):
                continue
            try:
                row = await _projection_row(self._content, self._embedder, source)
                await self._projection.upsert_active(row)
                await self._store.set_version_index_status(
                    row.version_id, _INDEX_READY
                )
                touched += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - repair the rest of the catalog
                logger.warning(
                    "Reconcile failed for active key %s (%s)",
                    key,
                    type(exc).__name__,
                )

        for key in dict.fromkeys(existing):
            if key in active_keys:
                continue
            await self._projection.delete_active(key)
            touched += 1
        return touched

    async def prune(
        self,
        keep_versions_per_skill: int = 20,
        *,
        min_age_seconds: float = 0.0,
    ) -> int:
        """Delete unreferenced commit caches and projection rows only.

        No PostgreSQL metadata is deleted.  The keep-set consists of the active
        pointers, pending outbox targets, and the newest N versions per skill.
        ``min_age_seconds`` protects caches that only just lost their last
        reference (skills.retention.cache_ttl_days).
        """
        keep = int(keep_versions_per_skill)
        if keep < 1:
            raise ValueError("keep_versions_per_skill must be positive")

        versions = await self._store.list_all_versions()
        active = await self._store.list_active_visible()
        pending = await self._store.list_pending_outbox()
        by_id = {version.id: version for version in versions}

        keep_commits: set[str] = set()
        active_keys: set[str] = set()
        pending_keys: set[str] = set()

        for source in active:
            active_keys.add(_projection_key(source))
            keep_commits.update(_source_for_retention(source))
        for event in pending:
            pending_keys.add(event.projection_key)
            version = by_id.get(event.version_id) if event.version_id else None
            if version is not None:
                _remember_commit(keep_commits, version.draft_commit)

        by_skill: dict[str, list] = {}
        for version in versions:
            by_skill.setdefault(version.skill_id, []).append(version)
        for skill_versions in by_skill.values():
            skill_versions.sort(key=lambda version: version.version_seq, reverse=True)
            for version in skill_versions[:keep]:
                _remember_commit(keep_commits, version.draft_commit)

        removed = await self._content.prune(
            keep_commits, min_age_seconds=min_age_seconds
        )
        existing = await self._projection.list_projection_keys()
        keep_keys = active_keys | pending_keys
        for key in dict.fromkeys(existing):
            if key in keep_keys:
                continue
            await self._projection.delete_active(key)
            removed += 1
        return removed

    # ------------------------------------------------------------------
    # background loop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="skill-index-outbox")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the poll loop must survive
                logger.warning("Outbox poll failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._poll_seconds)


__all__ = ["SkillIndexOutboxWorker", "build_projection_row"]
