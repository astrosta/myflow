"""技能目录 → SkillResponse + Git 目录运行时。

两种来源，两种路径，**不混用**：

- `skills.source = filesystem`（仅测试/本地调试）：保留 M7 的文件系统扫描与
  列表与模型索引来自同一次 load_skills。
- `skills.source = git`：**永不扫盘**。列表来自进程级不可变快照
  （agentkit.skills.catalog.SkillSnapshot），只包含调用者已生效的私有技能；
  私有技能的启用开关持久化在 skills.enabled（键是内部 skill_id）。
  运行时由 app lifespan 构建；缺失运行时或空快照 = fail-closed（空列表），
  绝不回退到磁盘。

没有公共技能目录，也没有全局技能开关；保存后的草稿必须扫描并通过激活才可见。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from agentkit.config import get_app_config
from agentkit.config.runtime_paths import resolve_path
from agentkit.config.skills_config import SkillsConfig
from agentkit.knowledge.embedding import OpenAICompatibleEmbedder
from agentkit.persistence.engine import get_session_factory
from agentkit.skills import search as skill_search
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.builtin import builtin_names, clear_builtin_cache, load_builtin_skills
from agentkit.skills.catalog import (
    SkillCatalogService,
    SkillSnapshot,
    get_skill_snapshot,
    set_skill_snapshot,
)
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import SkillConflict
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.outbox import (
    RepairStats,
    SkillIndexOutboxWorker,
    VerifyStats,
)
from agentkit.skills.prompt import clear_skill_prompt_cache
from agentkit.skills.projection import MilvusSkillProjection
from agentkit.skills.search import SkillSearchService
from agentkit.skills.storage import clear_builtin_collision_warnings, load_skills
from agentkit.skills.store.sql import SqlSkillStore
from agentkit.skills.types import Skill
from agentkit.skills.usage import SkillUsageService, configure_skill_usage
from backend.app.gateway.errors import SkillConflictAPI, SkillNotFound, SkillRuntimeUnavailable
from backend.app.gateway.models.response import SkillResponse

logger = logging.getLogger(__name__)

GIT_SOURCE = "git"

#: 首轮对账的延迟上限：部署重启后尽快修复漂移，但不和启动时的 refresh 抢资源。
_FIRST_RECONCILE_DELAY_SECONDS = 30.0
#: 首轮完整性巡查的延迟上限：静默丢失要尽早发现，但不必在启动瞬间抢资源。
_FIRST_VERIFY_DELAY_SECONDS = 300.0


def _session_bind(store: Any) -> Any:
    """The engine behind a store's session factory, for advisory locks."""
    factory = getattr(store, "_sf", None)
    kwargs = getattr(factory, "kw", None)
    if not isinstance(kwargs, dict):
        return None
    return kwargs.get("bind")


def _maintenance_lock_key(projection: Any, kind: str) -> int:
    """Stable int64 advisory-lock key, scoped per collection and pass.

    Advisory locks are per database, so two deployments sharing PostgreSQL but
    using different Milvus collections must not contend for the same key.
    """
    collection = getattr(projection, "_collection", "") or ""
    material = f"skill-index-{kind}:{collection}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _is_missing_catalog_epoch_table(exc: Exception) -> bool:
    """区分“旧库没建表”和真正的数据库故障。"""
    message = str(exc).lower()
    return "skill_catalog_epoch" in message and (
        "no such table" in message
        or "does not exist" in message
        or "undefinedtable" in message
    )


def _report_missing_catalog_epoch(runtime: Any) -> None:
    """缺表只报一次：启动后每次写都刷同一条日志等于没有告警。"""
    if getattr(runtime, "_catalog_epoch_missing", False):
        return
    setattr(runtime, "_catalog_epoch_missing", True)
    logger.error(
        "skill_catalog_epoch is missing from the database schema; "
        "cross-worker skill catalog invalidation is disabled until the table "
        "exists. Run 'alembic upgrade head' (or bootstrap_schema) against this "
        "database and restart the gateway."
    )


async def _bump_catalog_epoch(runtime: Any) -> None:
    """Bump when possible; a soft-deleted legacy database must keep serving."""
    if getattr(runtime, "_catalog_epoch_missing", False):
        return
    store = getattr(runtime, "store", None)
    bump = getattr(store, "bump_catalog_epoch", None)
    if not callable(bump):
        return
    try:
        await bump()
    except Exception as exc:  # noqa: BLE001 - 缺表降级，其它故障交给调用方
        if not _is_missing_catalog_epoch_table(exc):
            raise
        _report_missing_catalog_epoch(runtime)


# ---------------------------------------------------------------------------
# 运行时：一个进程一份，由 app lifespan 拥有
# ---------------------------------------------------------------------------


@dataclass
class SkillRuntime:
    """Every service the Git-backed catalog needs, built once per process.

    Projection, outbox, and search are optional: without an embedding endpoint
    the catalog still serves lists, authoring, scanning, activation and
    snapshots; only the Milvus projection is absent.
    """

    store: Any
    git: Any
    content: Any
    author: Any
    search: Any = None
    catalog: Any = None
    outbox: Any = None
    projection: Any = None
    usage: SkillUsageService | None = None
    usage_redis: Any = None
    catalog_poll_seconds: float = 0.0
    #: 修复快路径的周期（秒）；0 表示不排期。只有装了 projection/outbox 才有意义。
    reconcile_interval_seconds: float = 0.0
    #: 修复快路径的预算：行数和墙钟时间，先到为准。
    repair_budget_rows: int = 500
    repair_budget_seconds: float = 180.0
    repair_max_attempts: int = 8
    #: 完整性巡查的周期（秒）与预算；0 表示不巡。
    verify_interval_seconds: float = 3600.0
    verify_budget_rows: int = 5000
    verify_budget_seconds: float = 300.0
    #: 每轮维护用 pg_try_advisory_lock 互斥，避免多 worker 重复 embedding。
    maintenance_lock: bool = True
    #: 事件驱动缓存清理时，每个技能保留多少个最近版本的提交缓存。
    keep_versions_per_skill: int = 20
    #: 清理的安全期：刚失去引用不到这么久的缓存先留着（retention.cache_ttl_days）。
    prune_min_age_seconds: float = 0.0
    _index_maintenance_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _verify_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _catalog_epoch: int | None = field(default=None, init=False, repr=False)
    _catalog_epoch_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _catalog_epoch_missing: bool = field(default=False, init=False, repr=False)

    async def _catalog_epoch_value(self) -> int:
        """读持久代数；旧测试替身没有这个方法时按 0 处理。"""
        if self._catalog_epoch_missing:
            return 0
        reader = getattr(self.store, "catalog_epoch", None)
        if not callable(reader):
            return 0
        try:
            return int(await reader())
        except Exception as exc:  # noqa: BLE001 - 旧库缺表要降级，其余数据库故障照常抛
            if not _is_missing_catalog_epoch_table(exc):
                raise
            _report_missing_catalog_epoch(self)
            return 0

    async def refresh(self) -> Any:
        """Rebuild the catalog snapshot, then the active mount trees.

        The snapshot is what prompts and list routes see; the trees are what a
        sandbox mounts.  Both are derived from the same active pointers, so a
        caller must never refresh only one of them.
        """
        epoch = await self._catalog_epoch_value()
        snapshot = await self.catalog.refresh()
        await self.materialize_active_trees()
        # 先读后建：构建期间别的 worker 再 bump 时，下一轮轮询还能看见，
        # 不会把一次并发变更误认为已经包含在当前快照里。
        self._catalog_epoch = epoch
        return snapshot

    async def poll_catalog_epoch_once(self) -> bool:
        """刷新一次持久代数；有变化就重建快照并返回 True。"""
        current = await self._catalog_epoch_value()
        if current == self._catalog_epoch:
            return False
        await self.refresh()
        return True

    async def materialize_active_trees(self) -> None:
        """Write active/users/<owner> from the active pointers.

        A broken skill must not abort startup or a reload: materialization is a
        per-tree best effort, exactly like the snapshot's per-skill skip.  The
        previous tree stays in place until the swap succeeds (content store).
        There is no shared/public tree in v2.
        """
        active = list(await self.store.all_active())
        by_owner: dict[str, list[Any]] = {}
        for row in active:
            owner = row.skill.owner_user_id
            if not owner:
                logger.warning("Skipping private mount: active row has no owner")
                continue
            by_owner.setdefault(owner, []).append(row)
        for owner, rows in by_owner.items():
            try:
                await self.content.materialize_private(owner, rows)
            except Exception as exc:  # noqa: BLE001 - one owner must not stop the rest
                logger.warning("Private skill tree materialization failed for %s: %s", owner, exc)

        # 上一轮物化过、这一轮已经没有私有激活行的用户：把旧树收成空树。
        # 不收的话，用户删掉最后一个私有技能之后沙箱还在挂一棵不该存在的目录。
        try:
            existing = set(self.content.existing_private_owners())
        except Exception as exc:  # noqa: BLE001 - pruning is best effort like materialization
            logger.warning("Could not enumerate private mount trees: %s", exc)
            return
        for owner in sorted(existing - set(by_owner)):
            try:
                await self.content.materialize_private(owner, [])
            except Exception as exc:  # noqa: BLE001 - one owner must not stop the rest
                logger.warning("Private skill tree prune failed for %s: %s", owner, exc)

    async def start(self) -> None:
        """Ready Git if possible, publish the snapshot, start the projection worker.

        Design §5.10: startup does not require Git to be online.  With the PG
        active pointers plus the local commit cache the catalog can serve, so a
        remote outage only degrades to a warning here.  refresh() itself may
        still raise (a dead database is not a degraded cache), and the write
        paths (save/publish) keep calling git.ensure_ready() themselves, so this
        tolerance never hides an outage from them.
        """
        ensure_ready = getattr(self.git, "ensure_ready", None)
        if callable(ensure_ready):
            try:
                await ensure_ready()
            except Exception as exc:  # noqa: BLE001 - a Git outage must not abort startup
                logger.warning(
                    "Skill Git repository is unavailable (%s: %s); starting from the "
                    "local commit cache. Saving, publishing, and cache rebuilds will "
                    "keep failing until Git is reachable again.",
                    type(exc).__name__,
                    exc,
                )
        # 旧库可能停在 0005 而没有 epoch 表：在这里发现一次、报一条可操作的
        # ERROR，然后继续启动；缺的只是跨 worker 失效提示，不是目录本身。
        await self._catalog_epoch_value()
        await self.refresh()
        if self.outbox is not None:
            await self.outbox.start()
        if self.usage is not None:
            # Schedules DB -> Redis seeding; startup must not wait for it.
            await self.usage.start()
        if self.catalog_poll_seconds > 0 and (
            self._catalog_epoch_task is None or self._catalog_epoch_task.done()
        ):
            self._catalog_epoch_task = asyncio.create_task(
                self._catalog_epoch_loop(), name="skill-catalog-epoch"
            )
        # 对账/清理排期：outbox 只是“尽力而为”的队列，事件丢失、重试耗尽、
        # Milvus 被清空这些漂移不会自己好。没有排期，reconciliation_interval_minutes
        # 就只是一行没人读的配置。
        if self.outbox is not None and self.reconcile_interval_seconds > 0 and (
            self._index_maintenance_task is None
            or self._index_maintenance_task.done()
        ):
            self._index_maintenance_task = asyncio.create_task(
                self._repair_loop(), name="skill-index-maintenance"
            )
        # 完整性巡查：只发现“PG 说 ready、Milvus 里没了”的静默丢失，
        # 把版本标回 pending 交给快路径带预算修复，自己不做 embedding。
        if self.outbox is not None and self.verify_interval_seconds > 0 and (
            self._verify_task is None or self._verify_task.done()
        ):
            self._verify_task = asyncio.create_task(
                self._verify_loop(), name="skill-index-verify"
            )

    async def _catalog_epoch_loop(self) -> None:
        """短轮询：失败只记日志，绝不悄悄杀死任务。"""
        while True:
            try:
                await self.poll_catalog_epoch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the poll loop must survive
                logger.warning(
                    "Skill catalog epoch poll failed (%s: %s)", type(exc).__name__, exc
                )
            await asyncio.sleep(self.catalog_poll_seconds)

    async def repair_projection_once(self) -> RepairStats:
        """One bounded repair pass over versions whose projection is not ready.

        只查 PostgreSQL：index_status 就是“这一版该不该在 Milvus 里”的权威信号。
        Milvus 主键扫描只发生在 verify_projection_once 的低频巡查里。
        """
        outbox = self.outbox
        if outbox is None:
            return RepairStats()
        async with self._maintenance_lock("reconcile") as acquired:
            if not acquired:
                logger.info("Skill index repair skipped: another worker holds the lock")
                return RepairStats(truncated=False, skipped_due_to_lock=True)
            stats = await outbox.repair_missing(
                budget_rows=self.repair_budget_rows,
                budget_seconds=self.repair_budget_seconds,
            )
        logger.info(
            "Skill index repair: scanned=%s repaired=%s failed=%s truncated=%s "
            "seconds=%.2f",
            stats.scanned,
            stats.repaired,
            stats.failed,
            stats.truncated,
            stats.seconds,
        )
        return stats

    async def verify_projection_once(self) -> VerifyStats:
        """One integrity sweep: find rows Milvus lost and orphan projection rows."""
        outbox = self.outbox
        if outbox is None:
            return VerifyStats()
        async with self._maintenance_lock("verify") as acquired:
            if not acquired:
                logger.info("Skill index verify skipped: another worker holds the lock")
                return VerifyStats(skipped_due_to_lock=True)
            stats = await outbox.verify_projection(
                budget_rows=self.verify_budget_rows,
                budget_seconds=self.verify_budget_seconds,
            )
        logger.info(
            "Skill index verify: keys=%s missing=%s deleted=%s truncated=%s "
            "seconds=%.2f",
            stats.keys_seen,
            stats.missing,
            stats.deleted,
            stats.truncated,
            stats.seconds,
        )
        return stats

    async def forget_commits(self, skill_id: str) -> int:
        """Event-driven cache cleanup for one skill (save/delete call this).

        提交缓存缺失不是错误：SkillContentStore.ensure() 会从 Git 重新物化。
        所以这里只删“这个技能里已经没人引用”的提交，不做周期性全量比对。
        """
        outbox = self.outbox
        if outbox is None or not skill_id:
            return 0
        try:
            return await outbox.forget_commits(
                skill_id,
                keep_versions=self.keep_versions_per_skill,
                min_age_seconds=self.prune_min_age_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - caching is best effort
            logger.warning(
                "Skill commit cache cleanup failed for %s (%s)",
                skill_id,
                type(exc).__name__,
            )
            return 0

    async def _repair_loop(self) -> None:
        """周期修复；失败只记日志，绝不悄悄杀死任务。"""
        # 首轮用短延迟：start() 刚 refresh() 过，不需要零延迟修复，但一次部署
        # 重启之后（比如刚修好投影 collection 的索引）不该干等一整个周期。
        delay = min(self.reconcile_interval_seconds, _FIRST_RECONCILE_DELAY_SECONDS)
        while True:
            await asyncio.sleep(delay)
            delay = self.reconcile_interval_seconds
            try:
                await self.repair_projection_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the repair loop must survive
                logger.warning(
                    "Skill index repair failed (%s: %s)",
                    type(exc).__name__,
                    exc,
                )

    async def _verify_loop(self) -> None:
        """低频完整性巡查；失败只记日志，绝不悄悄杀死任务。"""
        # 首轮 5 分钟内跑一次：集合被清空这类静默丢失，重启后越快发现越好。
        delay = min(self.verify_interval_seconds, _FIRST_VERIFY_DELAY_SECONDS)
        while True:
            await asyncio.sleep(delay)
            delay = self.verify_interval_seconds
            try:
                await self.verify_projection_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the verify loop must survive
                logger.warning(
                    "Skill index verify failed (%s: %s)",
                    type(exc).__name__,
                    exc,
                )

    @asynccontextmanager
    async def _maintenance_lock(self, kind: str) -> AsyncIterator[bool]:
        """pg_try_advisory_lock 每轮互斥；抢不到就跳过本轮（不排队）。

        会话级锁挂在专用连接上：抢到后 commit 结束隐式事务、finally 显式解锁，
        进程被杀时连接断开，PostgreSQL 自己释放。非 PostgreSQL（SQLite 测试）
        直接放行。
        """
        if not self.maintenance_lock:
            yield True
            return
        bind = _session_bind(self.store)
        if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
            yield True
            return
        key = _maintenance_lock_key(self.projection, kind)
        async with bind.connect() as connection:
            acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                    {"key": key},
                )
            )
            await connection.commit()
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                        {"key": key},
                    )
                    await connection.commit()
                except Exception:  # pragma: no cover - release on close
                    logger.warning(
                        "Could not release the skill maintenance advisory lock",
                        exc_info=True,
                    )

    async def _stop_index_maintenance(self) -> None:
        for task in (self._index_maintenance_task, self._verify_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._index_maintenance_task = None
        self._verify_task = None

    async def _stop_catalog_poller(self) -> None:
        task = self._catalog_epoch_task
        self._catalog_epoch_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Stop background work and drop process-wide catalog state.

        Dropping the registry is unconditional: a failing outbox stop or a failing
        projection close must not leave a half-dead runtime installed, because the
        next get_skill_runtime() would hand that dead service to a request.
        """
        try:
            await self._stop_index_maintenance()
            await self._stop_catalog_poller()
            if self.outbox is not None:
                await self.outbox.stop()
            if self.usage is not None:
                await self.usage.stop()
            if self.usage_redis is not None:
                close = getattr(self.usage_redis, "aclose", None)
                if callable(close):
                    await close()
            if self.projection is not None:
                await self.projection.aclose()
        finally:
            reset_skill_runtime()


_runtime_lock = threading.Lock()
_runtime: SkillRuntime | None = None


def configure_skill_runtime(runtime: SkillRuntime | None) -> None:
    """Install (or clear) the process-wide catalog runtime.

    The load_skill / search_skills tools read agentkit.skills.search's own
    registry; pointing both at the same service is the only way the tools and
    the routes can agree on which catalog is authoritative.  The prompt builder
    separately reads the same runtime's usage cache.
    """
    global _runtime
    with _runtime_lock:
        _runtime = runtime
    skill_search.configure_skill_runtime(runtime.search if runtime is not None else None)
    configure_skill_usage(runtime.usage if runtime is not None else None)


def get_skill_runtime() -> SkillRuntime | None:
    with _runtime_lock:
        return _runtime


def index_projection_configured() -> bool:
    """True only when this deployment can actually drain the projection outbox."""
    runtime = get_skill_runtime()
    return bool(
        runtime is not None
        and runtime.projection is not None
        and runtime.outbox is not None
    )


def reset_skill_runtime() -> None:
    """Drop the runtime, the search service, and the published snapshot."""
    configure_skill_runtime(None)
    configure_skill_usage(None)
    set_skill_snapshot(SkillSnapshot(epoch=""))


async def forget_commits(skill_id: str) -> int:
    """Event-driven commit-cache cleanup for one skill; never raises.

    提交缓存缺失不是错误（SkillContentStore.ensure 会从 Git 重新物化），所以这里
    失败只记日志：清理不成功最多多占一点磁盘，绝不能把一个已经提交的保存/删除
    变成 HTTP 错误。
    """
    try:
        runtime = get_skill_runtime()
    except Exception as exc:  # noqa: BLE001 - committed writes must not become 500s
        logger.warning("Skill commit cache cleanup skipped: %s", exc)
        return 0
    if runtime is None:
        return 0
    return await runtime.forget_commits(skill_id)

async def refresh_skill_runtime() -> None:
    """Rebuild the catalog after a committed mutation, without ever raising.

    The snapshot and mount trees are derived state: a failed refresh must not
    turn an already-committed save or activation into an HTTP error.  The
    filesystem source keeps reading disk per request, so it has nothing to
    invalidate here.

    The epoch is an invalidation hint, not the authority: the active pointers
    are authoritative.  The bump therefore runs in its own transaction after
    the mutation commits, and a crash between the two is repaired by any later
    mutation, reload, or restart.  A failed bump must never suppress the local
    refresh, and a failed refresh must never surface as a failed request.
    """
    try:
        if _skill_source() != GIT_SOURCE:
            return
        runtime = get_skill_runtime()
    except Exception as exc:  # noqa: BLE001 - committed writes must not become 500s
        logger.warning("Skill catalog refresh failed after a committed mutation: %s", exc)
        return
    if runtime is None:
        logger.warning("Skill runtime is not installed; skipping catalog refresh")
        return
    # 先 bump 再本地刷新：别的 worker 靠代际看见这次提交，本 worker 把
    # 自己的 bump 吸收进当前快照，下一轮轮询就不会再重建。两步必须独立：
    # bump 失败只丢跨 worker 提示，本地快照仍然要更新。
    try:
        await _bump_catalog_epoch(runtime)
    except Exception as exc:  # noqa: BLE001 - the local refresh must still run
        logger.warning("Skill catalog epoch bump failed: %s", exc)
    try:
        await runtime.refresh()
        clear_skill_prompt_cache()
    except Exception as exc:  # noqa: BLE001 - committed writes must not become 500s
        logger.warning("Skill catalog refresh failed after a committed mutation: %s", exc)


def runtime_for_request(request: Any) -> SkillRuntime | None:
    """app.state first (lifespan-owned), then the process registry (tests/embedding)."""
    state = getattr(getattr(request, "app", None), "state", None)
    runtime = getattr(state, "skill_runtime", None)
    return runtime if runtime is not None else get_skill_runtime()


def _build_skill_reranker(skills: SkillsConfig) -> Any | None:
    """精排是可选终排：关闭时返回 None，开了但没配地址就让启动失败。

    和 embedding 同一条纪律——代码里没有默认服务地址。空地址不会在启动时炸，
    只会在每次检索时变成一个沉默的超时。
    """
    rerank = skills.search.rerank
    if not rerank.enabled:
        return None
    if not rerank.base_url.strip() or not rerank.model.strip():
        raise RuntimeError(
            "skills.search.rerank.enabled=true requires both skills.search.rerank."
            "base_url and skills.search.rerank.model."
        )
    from agentkit.knowledge.rerank import build_reranker

    return build_reranker(rerank)


def build_skill_runtime(
    app_config: Any, *, session_factory: Any | None = None
) -> SkillRuntime:
    """Build the Git-backed runtime from configuration.

    No I/O happens here beyond constructing clients: the lifespan starts the
    runtime once PostgreSQL and Git are reachable.
    """
    skills: SkillsConfig = app_config.skills
    session = session_factory if session_factory is not None else get_session_factory()
    if session is None:
        backend = getattr(getattr(app_config, "database", None), "backend", "unknown")
        raise RuntimeError(
            "skills.source=git requires a SQL database: SqlSkillStore needs a session "
            f"factory, but database.backend={backend!r} provides none. Set "
            "database.backend to sqlite or postgres, or use skills.source=filesystem."
        )
    if not skills.git.repository.strip():
        # Git 是唯一内容源：地址为空不是"暂时连不上"，是没配。让它在这里炸，
        # 而不是让进程带着空目录健康启动、每次保存/扫描/生效都在第一次用到时失败。
        raise RuntimeError(
            "skills.source=git requires skills.git.repository: set it (the shipped "
            "config.yaml reads $SKILLS_GIT_REPOSITORY) or use skills.source=filesystem."
        )
    store = SqlSkillStore(session)
    cache_dir = resolve_path(skills.git.cache_dir)
    git = SkillGitRepository(skills.git, cache_dir)
    content = SkillContentStore(cache_dir, git)
    reserved_names = lambda: builtin_names(skills)
    author = SkillAuthorService(
        store, git, content, skills.creation, reserved_names=reserved_names
    )
    catalog = SkillCatalogService(store, content, skills.container_path)
    usage = None
    usage_redis = None
    if skills.usage.enabled and skills.usage.redis_url.strip():
        try:
            from redis.asyncio import Redis
        except ImportError:
            logger.warning(
                "skills.usage.redis_url is set but the redis package is missing; "
                "skill usage ranking will use PostgreSQL fallback"
            )
        else:
            usage_redis = Redis.from_url(
                skills.usage.redis_url,
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            usage = SkillUsageService(
                store,
                usage_redis,
                key_prefix=skills.usage.key_prefix,
                top_k=skills.usage.prompt_top_k,
                refresh_seconds=skills.usage.refresh_seconds,
            )

    search = None
    outbox = None
    projection = None
    # Every skill recall channel (dense, BM25, RRF) is a Milvus operation, so the
    # active projection and its outbox are required even when embedding is off.
    if skills.search.enabled:
        embedding = skills.search.embedding
        keyword = skills.search.keyword
        reranker = _build_skill_reranker(skills)
        vector_store = skills.search.active_vector_store(
            app_config.knowledge_base.vector_store
        )
        if not vector_store.uri.strip():
            # Empty is not "temporarily unreachable": it means no BM25 index either.
            raise RuntimeError(
                "skills.search.enabled=true requires a Milvus endpoint: set "
                "skills.search.vector_store.uri, or borrow one with "
                "knowledge_base.vector_store.uri."
            )
        embedder = None
        if embedding.enabled and embedding.base_url.strip():
            embedder = OpenAICompatibleEmbedder(embedding)
        projection = MilvusSkillProjection(
            # The skills projection owns its collection (myflow_skills_active);
            # only the Milvus connection may be borrowed from the knowledge base.
            vector_store,
            dimensions=embedding.dimensions,
            analyzer=keyword.analyzer,
            bm25_k1=keyword.bm25_k1,
            bm25_b=keyword.bm25_b,
        )
        search = SkillSearchService(
            store,
            projection,
            content,
            embedder,
            skills.search,
            reranker,
            usage=usage,
        )
        outbox = SkillIndexOutboxWorker(
            store,
            content,
            embedder,
            projection,
            dimensions=embedding.dimensions,
            poll_seconds=skills.index.outbox_poll_seconds,
            repair_max_attempts=skills.index.repair_max_attempts,
        )

    return SkillRuntime(
        store=store,
        git=git,
        content=content,
        author=author,
        search=search,
        catalog=catalog,
        outbox=outbox,
        projection=projection,
        usage=usage,
        usage_redis=usage_redis,
        catalog_poll_seconds=skills.index.catalog_poll_seconds,
        # 分钟级配置在运行时统一换算成秒排期。
        reconcile_interval_seconds=float(skills.index.reconciliation_interval_minutes) * 60.0,
        repair_budget_rows=skills.index.repair_budget_rows,
        repair_budget_seconds=skills.index.repair_budget_seconds,
        repair_max_attempts=skills.index.repair_max_attempts,
        verify_interval_seconds=float(skills.index.verify_interval_minutes) * 60.0,
        verify_budget_rows=skills.index.verify_budget_rows,
        verify_budget_seconds=skills.index.verify_budget_seconds,
        maintenance_lock=skills.index.maintenance_lock,
        keep_versions_per_skill=skills.retention.keep_versions_per_skill,
        prune_min_age_seconds=float(skills.retention.cache_ttl_days) * 86400.0,
    )


# ---------------------------------------------------------------------------
# 兼容层：filesystem 来源的 M7 行为（不删、不改形状）
# ---------------------------------------------------------------------------


def skills_config() -> SkillsConfig:
    return get_app_config().skills


def scan_skill_records() -> list[Skill]:
    """磁盘上扫到的全部技能。"""
    return load_skills(skills_config())


def _legacy_response(skill: Skill, *, user_id: str | None) -> SkillResponse:
    del user_id
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        category=skill.category.value,
        enabled=skill.enabled,
    )


def scan_skills_for_api(*, user_id: str | None) -> list[SkillResponse]:
    return [
        _legacy_response(skill, user_id=user_id)
        for skill in sorted(scan_skill_records(), key=lambda item: item.name)
    ]


def reload_skill_caches() -> None:
    """filesystem 来源：清本进程的 prompt 段落 LRU 缓存。"""
    clear_skill_prompt_cache()


# ---------------------------------------------------------------------------
# Git 来源：列表 / 个人偏好 / reload
# ---------------------------------------------------------------------------


def _skill_source() -> str:
    return get_app_config().skills.source


def _private_mounts_supported() -> bool:
    """Per-user private mounts are a sandbox-provider capability (Task 14)."""
    try:
        from agentkit.sandbox import get_sandbox_provider

        return bool(getattr(get_sandbox_provider(), "supports_per_user_skill_mounts", False))
    except Exception:  # noqa: BLE001 - no provider means no private container path
        logger.warning("Sandbox provider unavailable; omitting private container paths")
        return False


def visible_catalog_rows(
    snapshot: SkillSnapshot,
    user_id: str | None,
) -> list[Skill]:
    """The caller's active skills only.

    This does **not** filter disabled or personally-switched-off skills: the
    settings page has to render the row with its effective state.  Runtime
    visibility (prompt/search/slash) goes through SkillSnapshot.visible_for.
    """
    rows = tuple(snapshot.private_by_owner.get(user_id, ())) if user_id else ()
    return sorted(rows, key=lambda skill: (skill.name, skill.skill_id or ""))


# ---------------------------------------------------------------------------
# 内置技能：随项目发布、只读、不进 Git/数据库，所以单独成行再合并进列表
# ---------------------------------------------------------------------------


#: 已经告警过的同名冲突（内置技能盖住 catalog 行）。列表是每请求都走的路径，
#: 每次 GET 都刷同一条 WARNING 等于没有告警；reload 时清空，让配置改动重新可见。
_warned_builtin_collisions: set[str] = set()


def _warn_builtin_collision(name: str) -> None:
    if name in _warned_builtin_collisions:
        return
    _warned_builtin_collisions.add(name)
    logger.warning("Skill %s: builtin hides the catalog row", name)


def _reset_builtin_collision_warnings() -> None:
    """reload 之后重新告警：和 clear_builtin_cache() 清 warned-roots 是同一个理由。"""
    _warned_builtin_collisions.clear()


def _builtin_skills() -> tuple[Skill, ...]:
    """按当前配置扫内置技能；加载器本身已按失败退化成空元组。"""
    return load_builtin_skills(get_app_config().skills)


def _builtin_response(skill: Skill) -> SkillResponse:
    """内置行只有“它是什么”，没有目录身份：id/版本/commit/path 一律为空。"""
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        category="builtin",
        enabled=True,
        skill_id=None,
        scope="builtin",
        skill_version=None,
        version_id=None,
        git_commit=None,
        skill_path=None,
        status="builtin",
        owner_user_id=None,
        container_file=skill.container_file,
    )


def builtin_skill_responses() -> list[SkillResponse]:
    """GET /api/skills 的内置行：与 catalog 无关，没有 Git 运行时也存在。"""
    return [_builtin_response(skill) for skill in _builtin_skills()]


def merge_builtin_responses(
    builtin: list[SkillResponse], catalog: list[SkillResponse]
) -> list[SkillResponse]:
    """内置行在前；同名的 catalog 行被隐藏（内置优先，和提示词 overlay 一致）。"""
    if not builtin:
        return list(catalog)
    names = {row.name for row in builtin}
    for row in catalog:
        if row.name in names:
            _warn_builtin_collision(row.name)
    return [*builtin, *(row for row in catalog if row.name not in names)]


def _catalog_response(skill: Skill, *, user_id: str | None) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        category="custom",
        enabled=bool(skill.enabled),
        skill_id=skill.skill_id,
        scope="private",
        skill_version=skill.skill_version,
        version_id=skill.version_id,
        git_commit=skill.git_commit,
        skill_path=skill.repo_path,
        status="active",
        owner_user_id=skill.owner_user_id,
        container_file=skill.container_file if _private_mounts_supported() else None,
    )


def _catalog_responses(
    snapshot: SkillSnapshot, *, user_id: str | None
) -> list[SkillResponse]:
    return [
        _catalog_response(skill, user_id=user_id)
        for skill in visible_catalog_rows(snapshot, user_id)
    ]


async def list_skills_for_api(
    *,
    runtime: SkillRuntime | None,
    user_id: str | None,
) -> list[SkillResponse]:
    """GET /api/skills: builtin plus the caller's active private skills.

    The filesystem source keeps its legacy public/custom scan for local
    debugging only.  The Git source never reads another user's row and has no
    public catalog at all.
    """
    builtin = builtin_skill_responses()
    if _skill_source() != GIT_SOURCE:
        catalog = await asyncio.to_thread(
            scan_skills_for_api, user_id=user_id
        )
        return merge_builtin_responses(builtin, catalog)
    if runtime is None:
        # 没有运行时 = 没有权威目录。旧快照只是上一次刷新留下的残影，
        # 拿它当答案等于把删除的技能继续列出来，所以 git 来源 fail-closed。
        logger.error("Git skill catalog has no runtime; failing closed with an empty catalog list")
        return builtin
    catalog = _catalog_responses(get_skill_snapshot(), user_id=user_id)
    return merge_builtin_responses(builtin, catalog)


async def set_skill_preference(
    *,
    runtime: SkillRuntime | None,
    user_id: str | None,
    skill_id: str,
    enabled: bool,
) -> SkillResponse:
    """PUT /api/skills/{skill_id}/preference: persist the DB switch for one skill."""
    if _skill_source() != GIT_SOURCE:
        raise SkillConflictAPI(
            "filesystem skills are read-only; activate a Git skill to manage its switch"
        )
    if runtime is None:
        raise SkillRuntimeUnavailable("skill runtime is not configured")
    target = next(
        (
            row
            for row in visible_catalog_rows(get_skill_snapshot(), user_id)
            if row.skill_id == skill_id
        ),
        None,
    )
    if target is None:
        raise SkillNotFound(f"Skill {skill_id} not found")

    try:
        await runtime.author.set_enabled(
            owner_user_id=user_id or "",
            skill_id=skill_id,
            enabled=enabled,
            index_enabled=index_projection_configured(),
        )
    except SkillConflict as exc:
        raise SkillConflictAPI(str(exc)) from exc

    await refresh_skill_runtime()
    outbox = runtime.outbox
    if outbox is not None:
        try:
            await outbox.run_once()
        except Exception as exc:  # noqa: BLE001 - the outbox worker retries durably
            logger.warning("Immediate skill projection toggle failed: %s", exc)

    skill = next(
        (
            row
            for row in visible_catalog_rows(get_skill_snapshot(), user_id)
            if row.skill_id == skill_id
        ),
        None,
    )
    if skill is None:
        raise SkillNotFound(f"Skill {skill_id} not found")
    return _catalog_response(skill, user_id=user_id)


async def reload_skills(*, runtime: SkillRuntime | None) -> None:
    """POST /api/skills/reload: rebuild this process's snapshot (git) or caches."""
    # 内置技能的目录指纹缓存也要跟着重载走，且必须在任何 fail-closed 之前清：
    # 503 也要保证下次请求看见磁盘上的新目录。
    clear_builtin_cache()
    clear_builtin_collision_warnings()
    _reset_builtin_collision_warnings()
    if _skill_source() != GIT_SOURCE:
        await asyncio.to_thread(reload_skill_caches)
        return
    if runtime is None:
        # 报 200 却什么都没刷新，比失败更糟：调用方会以为新技能已经生效。
        logger.error("Refusing to reload: the skill runtime is not configured")
        # 没有快照可刷新，但提示词段落里仍可能留着内置技能的旧缓存；清一次再报错。
        clear_skill_prompt_cache()
        raise SkillRuntimeUnavailable(
            "skills catalog runtime is not configured; reload cannot refresh the snapshot"
        )
    await runtime.refresh()
    # 一次 reload 也承担跨 worker 失效：否则管理员得在每个 worker 上重复
    # 调用，和路由文档里承诺的单次修复不一致。
    await _bump_catalog_epoch(runtime)
    clear_skill_prompt_cache()