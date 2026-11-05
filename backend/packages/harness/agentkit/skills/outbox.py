"""Projection outbox worker: turn catalog commits into active Milvus rows.

PostgreSQL stays authoritative.  A claimed outbox event is only a hint: the
worker re-reads the current active pointer and settings before embedding, so a
stale event is marked done instead of overwriting a newer projection.  Milvus
writes never happen while a PostgreSQL transaction is open.

The active collection has one row per active private skill.  When several
pending events target the same projection key, only the newest is processed;
the older events are settled as no-ops.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

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
#: 修复快路径的退避：坏行按指数退避，跨重启有效（状态在 PostgreSQL 里）。
_DEFAULT_REPAIR_MAX_ATTEMPTS = 8
_DEFAULT_RETRY_BASE_SECONDS = 60.0
_DEFAULT_RETRY_CAP_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class RepairStats:
    """One bounded repair pass."""

    scanned: int = 0
    repaired: int = 0
    failed: int = 0
    truncated: bool = False
    #: 另一个 worker 拿着 advisory lock：这一轮什么都没做。
    skipped_due_to_lock: bool = False
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class VerifyStats:
    """One integrity sweep over the projection keys."""

    keys_seen: int = 0
    missing: int = 0
    deleted: int = 0
    truncated: bool = False
    #: 另一个 worker 拿着 advisory lock：这一轮什么都没做。
    skipped_due_to_lock: bool = False
    seconds: float = 0.0


def _scope_value(scope: SkillScope | str) -> str:
    return scope.value if isinstance(scope, SkillScope) else str(scope)


def _projection_key(source: VisibleSkill) -> str:
    skill_id = source.version.skill_id
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
    return commits


#: 投影文本的语义版本（设计 §7.2）。改格式、换模型或换维度都等于让全部存量
#: 向量失效，需要运维对 collection 做一次强制重建/对账。
PROJECTION_TEXT_VERSION = 3


def projection_text(name: str, description: str) -> str:
    """参与 embedding 的文本：只有作者手写的元数据。

    正文不进来是刻意的：正文每次改动都会变（等于每次保存都重新 embedding），
    还可能超出 embedding 的 max_input_tokens，让整条索引直接失败。正文交给
    BM25 通道与精排去读，它们才真的需要长文本（设计 §7.2 / §7.4）。
    """
    return f"{name}\n{description}".strip()


async def _projection_row(
    embedder: Embedder,
    source: VisibleSkill,
) -> ProjectionRow:
    """Build one active row from a freshly read active pointer."""
    version = source.version
    commit = version.draft_commit
    name = source.skill.name
    owner_user_id = source.skill.owner_user_id
    if not commit:
        raise ValueError("active skill has no content commit")

    text = projection_text(name, source.skill.description)
    vectors = await embedder.embed_documents([text])
    if len(vectors) != 1:
        raise ValueError("embedding provider returned the wrong document count")
    embedding = list(vectors[0])
    if not embedding:
        raise ValueError("embedding provider returned an empty vector")

    return ProjectionRow(
        projection_key=_projection_key(source),
        scope=SkillScope.PRIVATE.value,
        owner_user_id=owner_user_id,
        skill_id=version.skill_id,
        version_id=version.id,
        skill_version=version.skill_version,
        enabled=bool(source.skill.enabled),
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
    # The event only identifies the active version. The worker re-reads the
    # current enabled switch, so a stale toggle event still converges.
    return True


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
    row = await _projection_row(embedder, source)
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
        repair_max_attempts: int = _DEFAULT_REPAIR_MAX_ATTEMPTS,
        retry_base_seconds: float = _DEFAULT_RETRY_BASE_SECONDS,
        retry_cap_seconds: float = _DEFAULT_RETRY_CAP_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if repair_max_attempts < 1:
            raise ValueError("repair_max_attempts must be positive")
        if retry_base_seconds <= 0 or retry_cap_seconds <= 0:
            raise ValueError("retry delays must be positive")
        self._store = store
        self._content = content
        self._embedder = embedder
        self._projection = projection
        self._poll_seconds = float(poll_seconds)
        self._max_attempts = int(repair_max_attempts)
        self._retry_base_seconds = float(retry_base_seconds)
        self._retry_cap_seconds = float(retry_cap_seconds)
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
                    await self._store.mark_version_indexed(row.version_id)
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

    async def repair_missing(
        self, *, budget_rows: int, budget_seconds: float
    ) -> RepairStats:
        """Repair active versions whose projection is not ready.

        index_status is the authority for "should this version be in Milvus", so a
        healthy catalog costs one indexed query and nothing else.  Rows that keep
        failing get a backoff (see the store) and stop consuming the budget, and
        the whole pass is bounded by rows *and* time: recovering a wiped
        collection then takes a few passes instead of one unbounded run.
        """
        started = time.monotonic()
        wanted = max(1, int(budget_rows))
        # One extra row tells us "there is more to do" without a second query.
        rows = await self._store.list_index_repair_candidates(
            limit=wanted + 1, max_attempts=self._max_attempts
        )
        repaired = failed = attempted = 0
        for source in rows[:wanted]:
            if attempted and time.monotonic() - started >= max(0.0, budget_seconds):
                break
            attempted += 1
            try:
                row = await _projection_row(self._embedder, source)
                await self._projection.upsert_active(row)
                await self._store.mark_version_indexed(source.version.id)
                repaired += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                failed += 1
                await self._store.mark_version_index_failed(
                    source.version.id,
                    error=f"{type(exc).__name__}: {exc}",
                    max_attempts=self._max_attempts,
                    base_seconds=self._retry_base_seconds,
                    cap_seconds=self._retry_cap_seconds,
                )
                logger.warning(
                    "Repair failed for version %s (%s); backing off",
                    source.version.id,
                    type(exc).__name__,
                )
        return RepairStats(
            scanned=min(len(rows), wanted),
            repaired=repaired,
            failed=failed,
            truncated=len(rows) > wanted,
            seconds=time.monotonic() - started,
        )

    async def verify_projection(
        self, *, budget_rows: int, budget_seconds: float
    ) -> VerifyStats:
        """Detect silent loss: rows the catalog believes are indexed but are gone.

        This is the only pass that reads Milvus keys, and it never embeds: a key
        that disappeared is marked pending so the bounded repair pass rebuilds it.
        A scan that hits the budget cannot prove absence, so the "missing" phase
        only runs on a completed scan; orphan deletion is safe either way.
        """
        started = time.monotonic()
        sources = await self._store.list_active_visible()
        pending = await self._store.list_pending_outbox()
        by_key = {_projection_key(source): source for source in sources}
        keep = set(by_key) | {event.projection_key for event in pending}

        keys, truncated = await self._projection.scan_projection_keys(
            limit=max(1, int(budget_rows))
        )
        seen = set(keys)

        deleted = 0
        for key in keys:
            if key in keep or key not in seen:
                continue
            seen.discard(key)
            if time.monotonic() - started >= max(0.0, budget_seconds):
                truncated = True
                break
            await self._projection.delete_active(key)
            deleted += 1

        missing = 0
        if not truncated:
            for key, source in by_key.items():
                if key in seen or source.version.index_status == "pending":
                    continue
                await self._store.set_version_index_status(source.version.id, "pending")
                missing += 1
        return VerifyStats(
            keys_seen=len(keys),
            missing=missing,
            deleted=deleted,
            truncated=truncated,
            seconds=time.monotonic() - started,
        )

    async def forget_commits(
        self,
        skill_id: str,
        *,
        keep_versions: int,
        min_age_seconds: float = 0.0,
    ) -> int:
        """Event-driven cache cleanup: drop one skill's unreferenced commits.

        Only this skill's commits are candidates, so no directory walk and no
        global comparison is needed.  The commit cache is a cache: anything still
        needed is re-materialized from Git by SkillContentStore.ensure().
        """
        keep = max(0, int(keep_versions))
        versions = await self._store.list_versions(skill_id)
        if not versions:
            return 0
        protected: set[str] = set()
        record = await self._store.get_skill(skill_id)
        retired = record is None or record.deleted_at is not None
        if keep and not retired:
            # 保留最近 N 版：删除技能之后没有任何"回滚"可言，缓存可以一次收干净。
            for version in versions[-keep:]:
                _remember_commit(protected, version.draft_commit)
        pending = [
            event
            for event in await self._store.list_pending_outbox()
            if event.skill_id == skill_id
        ]
        pending_versions = {event.version_id for event in pending if event.version_id}
        for source in await self._store.list_active_visible():
            if source.version.skill_id == skill_id:
                _remember_commit(protected, source.version.draft_commit)
        for version in versions:
            if version.id in pending_versions:
                _remember_commit(protected, version.draft_commit)
        candidates = {
            version.draft_commit
            for version in versions
            if version.draft_commit and version.draft_commit.lower() not in protected
        }
        if not candidates:
            return 0
        return await self._content.forget_commits(
            candidates, min_age_seconds=min_age_seconds
        )
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
