"""Per-user skill load counts: PostgreSQL durability + Redis ZSET ranking.

The agent prompt builder is synchronous, so it cannot await Redis.  This service
keeps a process-local top-K cache hydrated from Redis at startup, after each
load, and by a periodic refresh.  The cache is only a read model; PostgreSQL is
the durable count and Redis is the ranking source.  ``top_k=0`` (prompt_top_k)
still records loads but skips ranking-cache hydration.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from agentkit.skills.store.sql import SqlSkillStore

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class SkillUsageService:
    """Owns the per-user Redis ZSET and its synchronous top-K read cache."""

    def __init__(
        self,
        store: SqlSkillStore,
        redis: Any,
        *,
        key_prefix: str = "myflow:skills:usage",
        top_k: int = 10,
        refresh_seconds: float = 30.0,
    ) -> None:
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be blank")
        # 0 = prompt 不需要 top-K（skills.usage.prompt_top_k=0），但仍记录使用次数。
        if top_k < 0:
            raise ValueError("top_k must not be negative")
        if refresh_seconds < 0:
            raise ValueError("refresh_seconds must not be negative")
        self._store = store
        self._redis = redis
        self._prefix = key_prefix.rstrip(":")
        self._top_k = int(top_k)
        self._refresh_seconds = float(refresh_seconds)
        self._cache: dict[str, tuple[str, ...]] = {}
        self._known_users: set[str] = set()
        self._lock = threading.Lock()
        self._seed_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def top_k(self) -> int:
        return self._top_k

    def key(self, user_id: str) -> str:
        return f"{self._prefix}:{user_id}"

    def top_ids(self, user_id: str | None) -> tuple[str, ...] | None:
        """Synchronous cache read; None means this process has not warmed it."""
        if not user_id:
            return None
        with self._lock:
            return self._cache.get(user_id)

    def _remember(self, user_id: str, ids: tuple[str, ...]) -> None:
        with self._lock:
            self._cache[user_id] = ids
            self._known_users.add(user_id)

    async def start(self) -> None:
        """Schedule DB -> Redis seeding and periodic cache refresh without blocking."""
        if self._seed_task is None or self._seed_task.done():
            self._seed_task = asyncio.create_task(
                self.seed_from_database(), name="skill-usage-seed"
            )
        # top_k=0 时没有 prompt 排序可读，不必周期刷缓存。
        if self._top_k > 0 and self._refresh_seconds > 0 and (
            self._refresh_task is None or self._refresh_task.done()
        ):
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name="skill-usage-refresh"
            )

    async def stop(self) -> None:
        for task in (self._seed_task, self._refresh_task):
            if task is not None:
                task.cancel()
        for task in (self._seed_task, self._refresh_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._seed_task = None
        self._refresh_task = None

    async def seed_from_database(self) -> None:
        try:
            rows = await self._store.list_skill_load_counts()
        except Exception as exc:  # noqa: BLE001 - Redis ranking is best effort
            logger.warning("Could not read skill load counts for Redis seeding: %s", exc)
            return
        grouped: dict[str, dict[str, int]] = {}
        for owner, skill_id, count in rows:
            if count > 0:
                grouped.setdefault(owner, {})[skill_id] = int(count)
        for user_id, mapping in grouped.items():
            try:
                # GT prevents a startup backfill from lowering a value another
                # worker already incremented after the DB read.
                await self._redis.zadd(self.key(user_id), mapping, gt=True)
                await self._warm(user_id)
            except Exception as exc:  # noqa: BLE001 - keep seeding other users
                logger.warning(
                    "Could not seed skill usage for one user (%s); continuing",
                    type(exc).__name__,
                )
        logger.info(
            "Seeded skill usage counts for %s user(s) into Redis", len(grouped)
        )

    async def record_load(self, user_id: str, skill_id: str) -> None:
        """Best-effort, non-atomic Redis + PostgreSQL increments."""
        if not user_id or not skill_id:
            return
        try:
            await self._redis.zincrby(self.key(user_id), 1, skill_id)
        except Exception as exc:  # noqa: BLE001 - DB fallback still records the count
            logger.warning(
                "Could not increment Redis skill usage for %s (%s)",
                skill_id,
                type(exc).__name__,
            )
        try:
            await self._store.increment_skill_load_count(skill_id)
        except Exception as exc:  # noqa: BLE001 - loading the skill already succeeded
            logger.warning(
                "Could not persist skill load count for %s (%s)",
                skill_id,
                type(exc).__name__,
            )
        try:
            await self._warm(user_id)
        except Exception as exc:  # noqa: BLE001 - prompt has a DB fallback
            logger.warning(
                "Could not refresh skill usage cache for one user (%s)",
                type(exc).__name__,
            )

    async def refresh_user(self, user_id: str) -> None:
        await self._warm(user_id)

    async def _warm(self, user_id: str) -> None:
        if self._top_k <= 0:
            # Redis zrevrange end=-1 会返回全集；prompt_top_k=0 时直接记空缓存。
            self._remember(user_id, ())
            return
        rows = await self._redis.zrevrange(
            self.key(user_id), 0, self._top_k - 1, withscores=True
        )
        ids: list[str] = []
        for item in rows:
            member = item[0] if isinstance(item, (tuple, list)) and item else item
            value = _text(member)
            if value:
                ids.append(value)
        self._remember(user_id, tuple(ids[: self._top_k]))

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            with self._lock:
                users = list(self._known_users)
            for user_id in users:
                try:
                    await self._warm(user_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one user must not stop the loop
                    logger.warning(
                        "Skill usage cache refresh failed for one user (%s)",
                        type(exc).__name__,
                    )


_usage_service: SkillUsageService | None = None


def configure_skill_usage(service: SkillUsageService | None) -> None:
    global _usage_service
    _usage_service = service


def get_skill_usage_service() -> SkillUsageService | None:
    return _usage_service


def top_skill_ids(user_id: str | None) -> tuple[str, ...] | None:
    service = get_skill_usage_service()
    return service.top_ids(user_id) if service is not None else None


__all__ = [
    "SkillUsageService",
    "configure_skill_usage",
    "get_skill_usage_service",
    "top_skill_ids",
]
