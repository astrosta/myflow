"""技能目录 → SkillResponse（M7 §2.4/§3.5）+ Git 目录运行时（M11 Task 15）。

两种来源，两种路径，**不混用**：

- `skills.source = filesystem`（仅测试/迁移）：保留 M7 的文件系统扫描与
  extensions_config.json 全局开关。列表与模型索引来自同一次 load_skills。
- `skills.source = git`：**永不扫盘**。列表来自进程级不可变快照
  （agentkit.skills.catalog.SkillSnapshot），全局开关写 PostgreSQL 的
  skill_settings，个人偏好仍写 skills_prefs.json（键是内部 skill_id）。
  运行时由 app lifespan 构建；缺失运行时或空快照 = fail-closed（空列表），
  绝不回退到磁盘。

个人偏好与全局开关是两层：effective = global and personal。路由只负责搬运，
真正的读写落点由来源决定。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from agentkit.config import get_app_config
from agentkit.config.extensions_config import (
    reset_extensions_config_cache,
    update_extensions_config,
)
from agentkit.config.runtime_paths import resolve_path
from agentkit.config.skills_config import SkillsConfig
from agentkit.config.skills_prefs import read_user_skill_prefs, write_catalog_skill_pref, write_user_skill_pref
from agentkit.knowledge.embedding import OpenAICompatibleEmbedder
from agentkit.persistence.engine import get_session_factory
from agentkit.skills import search as skill_search
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.catalog import (
    SkillCatalogService,
    SkillSnapshot,
    get_skill_snapshot,
    set_skill_snapshot,
)
from agentkit.skills.content import SkillContentStore
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.outbox import SkillIndexOutboxWorker
from agentkit.skills.prefs import effective_enabled, global_enabled_map
from agentkit.skills.prompt import clear_skill_prompt_cache
from agentkit.skills.projection import MilvusSkillProjection
from agentkit.skills.review import SkillReviewService
from agentkit.skills.search import SkillSearchService
from agentkit.skills.storage import load_skills
from agentkit.skills.store.sql import SqlSkillStore
from agentkit.skills.types import Skill
from backend.app.gateway.errors import SkillNotFound, SkillRuntimeUnavailable
from backend.app.gateway.models.response import SkillResponse

logger = logging.getLogger(__name__)

GIT_SOURCE = "git"


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
    the catalog still serves lists, authoring, review, and snapshots; only the
    Milvus projection is absent.
    """

    store: Any
    git: Any
    content: Any
    author: Any
    review: Any
    search: Any = None
    catalog: Any = None
    outbox: Any = None
    projection: Any = None
    catalog_poll_seconds: float = 0.0
    #: 对账 + 清理的周期（秒）；0 表示不排期。只有装了 projection/outbox 才有意义。
    reconcile_interval_seconds: float = 0.0
    #: prune 时每个技能保留多少个最近版本的提交缓存。
    keep_versions_per_skill: int = 20
    #: prune 的安全期：刚失去引用不到这么久的缓存先留着（retention.cache_ttl_days）。
    prune_min_age_seconds: float = 0.0
    _index_maintenance_task: asyncio.Task[None] | None = field(
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
        """Write active/public and active/users/<owner> from the active pointers.

        A broken skill must not abort startup or a reload: materialization is a
        per-tree best effort, exactly like the snapshot's per-skill skip.  The
        previous tree stays in place until the swap succeeds (content store).
        """
        active = list(await self.store.all_active())
        public = [row for row in active if _scope_value(row.scope) == "public"]
        try:
            await self.content.materialize_public(public)
        except Exception as exc:  # noqa: BLE001 - one broken tree must not stop startup
            logger.warning("Public skill tree materialization failed: %s", exc)

        by_owner: dict[str, list[Any]] = {}
        for row in active:
            if _scope_value(row.scope) != "private":
                continue
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
                self._index_maintenance_loop(), name="skill-index-maintenance"
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

    async def reconcile_projection_once(self) -> tuple[int, int]:
        """一轮对账 + 清理，返回（修复/删除的投影行数, 清掉的缓存目录数）。

        对账修的是“Milvus 与 PostgreSQL 漂移”：事件丢了、重试耗尽了、collection
        被清空了。清理删的是没被任何活指针/待处理事件/最近 N 版引用的提交缓存。
        两者都不删 PostgreSQL 元数据，也不自己产生 embedding。
        """
        outbox = self.outbox
        if outbox is None:
            return (0, 0)
        reconciled = await outbox.reconcile()
        pruned = await outbox.prune(
            self.keep_versions_per_skill, min_age_seconds=self.prune_min_age_seconds
        )
        if reconciled or pruned:
            logger.info(
                "Skill index maintenance: %s projection row(s) repaired, "
                "%s cache entr(ies) pruned",
                reconciled,
                pruned,
            )
        return (reconciled, pruned)

    async def _index_maintenance_loop(self) -> None:
        """周期对账/清理；失败只记日志，绝不悄悄杀死任务。"""
        while True:
            # 先睡再跑：start() 刚 refresh() 过，启动那一刻没有对账的必要。
            await asyncio.sleep(self.reconcile_interval_seconds)
            try:
                await self.reconcile_projection_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the sweep loop must survive
                logger.warning(
                    "Skill index maintenance failed (%s: %s)",
                    type(exc).__name__,
                    exc,
                )

    async def _stop_index_maintenance(self) -> None:
        task = self._index_maintenance_task
        self._index_maintenance_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

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
            if self.projection is not None:
                await self.projection.aclose()
        finally:
            reset_skill_runtime()


_runtime_lock = threading.Lock()
_runtime: SkillRuntime | None = None


def configure_skill_runtime(runtime: SkillRuntime | None) -> None:
    """Install (or clear) the process-wide catalog runtime.

    The builtin search_and_load_skills tool reads agentkit.skills.search's own
    registry; pointing both at the same service is the only way the tool and
    the routes can agree on which catalog is authoritative.
    """
    global _runtime
    with _runtime_lock:
        _runtime = runtime
    skill_search.configure_skill_runtime(runtime.search if runtime is not None else None)


def get_skill_runtime() -> SkillRuntime | None:
    with _runtime_lock:
        return _runtime


def reset_skill_runtime() -> None:
    """Drop the runtime, the search service, and the published snapshot."""
    configure_skill_runtime(None)
    set_skill_snapshot(SkillSnapshot(epoch="", public=(), private_by_owner={}))


async def refresh_skill_runtime() -> None:
    """Rebuild the catalog after a committed mutation, without ever raising.

    The snapshot and mount trees are derived state: a failed refresh must not
    turn an already-committed save or review decision into an HTTP error.  The
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
        # 而不是让进程带着空目录健康启动、每次保存/审核都在第一次用到时失败。
        raise RuntimeError(
            "skills.source=git requires skills.git.repository: set it (the shipped "
            "config.yaml reads $SKILLS_GIT_REPOSITORY) or use skills.source=filesystem."
        )
    store = SqlSkillStore(session)
    cache_dir = resolve_path(skills.git.cache_dir)
    git = SkillGitRepository(skills.git, cache_dir)
    content = SkillContentStore(cache_dir, git)
    author = SkillAuthorService(store, git, content, skills.creation)
    review = SkillReviewService(store, git, content, skills.review)
    catalog = SkillCatalogService(store, content, skills.container_path)

    search = None
    outbox = None
    projection = None
    # 关键词（BM25）通道不依赖任何外部服务，而 shipped 配置正是
    # search.enabled=true + embedding.enabled=false。检索服务必须无条件建起来，
    # 否则 git 来源在默认部署下根本没有检索——投影与 outbox 才是可选的那部分。
    if skills.search.enabled:
        embedding = skills.search.embedding
        reranker = _build_skill_reranker(skills)
        embedder = None
        if embedding.enabled and embedding.base_url.strip():
            vector_store = skills.search.active_vector_store(
                app_config.knowledge_base.vector_store
            )
            if not vector_store.uri.strip():
                # 空地址不会在启动时炸，只会在第一次 upsert 时变成一个沉默的超时。
                raise RuntimeError(
                    "skills.search.embedding.enabled=true but no Milvus endpoint is "
                    "configured: set skills.search.vector_store.uri, or borrow one with "
                    "knowledge_base.vector_store.uri."
                )
            embedder = OpenAICompatibleEmbedder(embedding)
            projection = MilvusSkillProjection(
                # The skills projection owns its collection (myflow_skills_active);
                # only the Milvus connection may be borrowed from the knowledge base.
                vector_store,
                dimensions=embedding.dimensions,
            )
        search = SkillSearchService(
            store, projection, content, embedder, skills.search, reranker
        )
        if projection is not None:
            outbox = SkillIndexOutboxWorker(
                store,
                content,
                embedder,
                projection,
                poll_seconds=skills.index.outbox_poll_seconds,
            )

    return SkillRuntime(
        store=store,
        git=git,
        content=content,
        author=author,
        review=review,
        search=search,
        catalog=catalog,
        outbox=outbox,
        projection=projection,
        catalog_poll_seconds=skills.index.catalog_poll_seconds,
        # reconciliation_interval_minutes 是分钟，运行时按秒排期。
        reconcile_interval_seconds=float(skills.index.reconciliation_interval_minutes) * 60.0,
        keep_versions_per_skill=skills.retention.keep_versions_per_skill,
        prune_min_age_seconds=float(skills.retention.cache_ttl_days) * 86400.0,
    )


# ---------------------------------------------------------------------------
# 兼容层：filesystem 来源的 M7 行为（不删、不改形状）
# ---------------------------------------------------------------------------


def skills_config() -> SkillsConfig:
    return get_app_config().skills


def scan_skill_records() -> list[Skill]:
    """磁盘上扫到的全部技能（含全局禁用的），带全局开关。"""
    return load_skills(skills_config(), enabled_map=global_enabled_map())


def _legacy_response(
    skill: Skill, *, user_id: str | None, include_global: bool
) -> SkillResponse:
    prefs = read_user_skill_prefs(user_id)
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        category=skill.category.value,
        enabled=effective_enabled(skill, prefs),
        global_enabled=skill.enabled if include_global else None,
    )


def scan_skills_for_api(*, user_id: str | None, include_global: bool = False) -> list[SkillResponse]:
    return [
        _legacy_response(skill, user_id=user_id, include_global=include_global)
        for skill in sorted(scan_skill_records(), key=lambda item: item.name)
    ]


def _legacy_response_for(
    name: str, *, user_id: str | None, include_global: bool = False
) -> SkillResponse:
    for skill in scan_skill_records():
        if skill.name == name:
            return _legacy_response(skill, user_id=user_id, include_global=include_global)
    raise SkillNotFound(f"Skill {name} not found")


def set_user_skill_pref(user_id: str | None, name: str, enabled: bool) -> SkillResponse:
    """filesystem 来源的个人开关：只写 skills_prefs.json（按名字）。"""
    if name not in {skill.name for skill in scan_skill_records()}:
        raise SkillNotFound(f"Skill {name} not found")
    write_user_skill_pref(user_id, name, enabled)
    return _legacy_response_for(name, user_id=user_id)


def patch_skill_enabled(name: str, enabled: bool) -> SkillResponse:
    """filesystem 来源的全局开关：写 extensions_config.json。"""

    def mutate(data: dict) -> None:
        scanned = {skill.name for skill in scan_skill_records()}
        if name not in scanned:
            raise SkillNotFound(f"Skill {name} not found")
        skills = data.get("skills")
        if not isinstance(skills, dict):
            skills = {}
        entry = skills.get(name)
        updated = dict(entry) if isinstance(entry, dict) else {}
        updated["enabled"] = bool(enabled)
        skills[name] = updated
        data["skills"] = skills

    update_extensions_config(mutate)
    clear_skill_prompt_cache()
    return _legacy_response_for(name, user_id=None, include_global=True)


def reload_skill_caches() -> None:
    """filesystem 来源：清本进程的 prompt 段落 LRU 与配置缓存。"""
    clear_skill_prompt_cache()
    reset_extensions_config_cache()


# ---------------------------------------------------------------------------
# Git 来源：列表 / 个人偏好 / 全局开关 / reload
# ---------------------------------------------------------------------------


def _scope_value(scope: object) -> str | None:
    if scope is None:
        return None
    return str(getattr(scope, "value", scope))


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


def visible_catalog_rows(snapshot: SkillSnapshot, user_id: str | None) -> list[Skill]:
    """Public plus the caller's private skills, private winning on a shared skill id.

    Unlike SkillSnapshot.visible_for this does **not** filter disabled or
    personally-switched-off skills: the settings page has to render both
    switches, so it needs the row with its effective state.
    """
    private = tuple(snapshot.private_by_owner.get(user_id, ())) if user_id else ()
    private_ids = {skill.skill_id for skill in private if skill.skill_id is not None}
    public = [
        skill
        for skill in snapshot.public
        if skill.skill_id is None or skill.skill_id not in private_ids
    ]
    rows = [*public, *private]
    return sorted(
        rows,
        key=lambda skill: (
            skill.name,
            0 if _scope_value(skill.scope) == "public" else 1,
            skill.skill_id or "",
        ),
    )


def _catalog_response(
    skill: Skill,
    *,
    user_id: str | None,
    include_global: bool,
    is_admin: bool,
    status: str,
) -> SkillResponse:
    scope = _scope_value(skill.scope)
    owner_visible = is_admin or (user_id is not None and skill.owner_user_id == user_id)
    if scope == "private":
        category = "custom"
        global_enabled = None
        container_file = skill.container_file if _private_mounts_supported() else None
    else:
        category = "public"
        global_enabled = skill.enabled if include_global else None
        container_file = skill.container_file
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        category=category,
        enabled=effective_enabled(skill, read_user_skill_prefs(user_id)),
        global_enabled=global_enabled,
        skill_id=skill.skill_id,
        scope=scope,  # type: ignore[arg-type]
        skill_version=skill.skill_version,
        version_id=skill.version_id,
        git_commit=skill.git_commit,
        skill_path=skill.repo_path,
        status=status,
        owner_user_id=skill.owner_user_id if owner_visible else None,
        container_file=container_file,
    )


async def _status_for(runtime: SkillRuntime | None, skill: Skill) -> str:
    if _scope_value(skill.scope) == "public":
        return "approved"
    latest = getattr(getattr(runtime, "store", None), "latest_submission", None)
    if callable(latest) and skill.skill_id:
        try:
            submission = await latest(skill.skill_id)
        except Exception as exc:  # noqa: BLE001 - status is cosmetic, never a 500
            logger.warning("Could not read submission state for %s: %s", skill.skill_id, exc)
        else:
            if submission is not None:
                return submission.status
    return "draft"


async def _catalog_responses(
    snapshot: SkillSnapshot,
    *,
    runtime: SkillRuntime | None,
    user_id: str | None,
    include_global: bool,
    is_admin: bool,
) -> list[SkillResponse]:
    responses: list[SkillResponse] = []
    for skill in visible_catalog_rows(snapshot, user_id):
        responses.append(
            _catalog_response(
                skill,
                user_id=user_id,
                include_global=include_global,
                is_admin=is_admin,
                status=await _status_for(runtime, skill),
            )
        )
    return responses


async def list_skills_for_api(
    *,
    runtime: SkillRuntime | None,
    user_id: str | None,
    include_global: bool = False,
    is_admin: bool = False,
) -> list[SkillResponse]:
    """GET /api/skills: filesystem scans disk; git reads the published snapshot."""
    if _skill_source() != GIT_SOURCE:
        return await asyncio.to_thread(
            scan_skills_for_api, user_id=user_id, include_global=include_global
        )
    if runtime is None:
        # 没有运行时 = 没有权威目录。已发布的快照只是上一次刷新留下的残影，
        # 拿它当答案等于把删除的技能继续列出来，所以 git 来源 fail-closed。
        logger.error("Git skill catalog has no runtime; failing closed with an empty list")
        return []
    snapshot = get_skill_snapshot()
    return await _catalog_responses(
        snapshot,
        runtime=runtime,
        user_id=user_id,
        include_global=include_global,
        is_admin=is_admin,
    )


async def set_skill_preference(
    *,
    runtime: SkillRuntime | None,
    user_id: str | None,
    skill_id: str,
    enabled: bool,
) -> SkillResponse:
    """PUT /api/skills/{skill_id}/preference: JSON, keyed by internal skill_id."""
    if _skill_source() != GIT_SOURCE:
        return await asyncio.to_thread(set_user_skill_pref, user_id, skill_id, enabled)

    snapshot = get_skill_snapshot()
    skill = next(
        (row for row in visible_catalog_rows(snapshot, user_id) if row.skill_id == skill_id),
        None,
    )
    if skill is None:
        raise SkillNotFound(f"Skill {skill_id} not found")
    await asyncio.to_thread(_write_catalog_pref, user_id, skill, enabled)
    return _catalog_response(
        skill,
        user_id=user_id,
        include_global=False,
        is_admin=False,
        status=await _status_for(runtime, skill),
    )


def _write_catalog_pref(user_id: str | None, skill: Skill, enabled: bool) -> None:
    key = skill.skill_id or skill.name
    write_catalog_skill_pref(user_id, key, enabled)


async def set_global_skill_enabled(
    *, runtime: SkillRuntime | None, skill_id: str, enabled: bool, actor: str | None
) -> SkillResponse:
    """PATCH /api/skills/{skill_id}: admin-only global switch for a public skill."""
    if _skill_source() != GIT_SOURCE:
        return await asyncio.to_thread(patch_skill_enabled, skill_id, enabled)

    if runtime is None:
        logger.error("Refusing a global skill toggle: the skill runtime is not configured")
        raise SkillNotFound(f"Skill {skill_id} not found")
    snapshot = get_skill_snapshot()
    target = next((row for row in snapshot.public if row.skill_id == skill_id), None)
    if target is None:
        raise SkillNotFound(f"Skill {skill_id} not found")

    await runtime.store.set_global_enabled(skill_id, bool(enabled), actor or "admin")
    # 全局开关也是目录状态：先 bump 再刷新，否则别的 worker 还按旧开关
    # 过滤 prompt 和列表。返回值和 prompt 缓存行为保持原样。
    await _bump_catalog_epoch(runtime)
    await runtime.refresh()
    refreshed = get_skill_snapshot()
    updated = next((row for row in refreshed.public if row.skill_id == skill_id), target)
    clear_skill_prompt_cache()
    return _catalog_response(
        updated,
        user_id=actor,
        include_global=True,
        is_admin=True,
        status="approved",
    )


async def reload_skills(*, runtime: SkillRuntime | None) -> None:
    """POST /api/skills/reload: rebuild this process's snapshot (git) or caches."""
    if _skill_source() != GIT_SOURCE:
        await asyncio.to_thread(reload_skill_caches)
        return
    if runtime is None:
        # 报 200 却什么都没刷新，比失败更糟：调用方会以为新技能已经生效。
        logger.error("Refusing to reload: the skill runtime is not configured")
        raise SkillRuntimeUnavailable(
            "skills catalog runtime is not configured; reload cannot refresh the snapshot"
        )
    await runtime.refresh()
    # 一次 reload 也承担跨 worker 失效：否则管理员得在每个 worker 上重复
    # 调用，和路由文档里承诺的单次修复不一致。
    await _bump_catalog_epoch(runtime)
    clear_skill_prompt_cache()
