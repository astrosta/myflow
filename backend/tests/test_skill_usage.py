"""技能使用次数：PostgreSQL 是权威计数，Redis ZSET 是排序源。

提示词构建是同步的，所以本进程另有一份 top-K 缓存；这些用例钉住四件事：
播种不降值（ZADD GT）、计数两边各写一次且互不阻塞、缓存只留 top-K、没有服务时
返回 None 交给 DB 兜底。
"""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from agentkit.skills.usage import (
    SkillUsageService,
    configure_skill_usage,
    get_skill_usage_service,
    top_skill_ids,
)


class FakeStore:
    def __init__(self, rows=(), *, seed_error: Exception | None = None) -> None:
        self.rows = list(rows)
        self.seed_error = seed_error
        self.incremented: list[str] = []

    async def list_skill_load_counts(self):
        if self.seed_error is not None:
            raise self.seed_error
        return list(self.rows)

    async def increment_skill_load_count(self, skill_id: str) -> int:
        self.incremented.append(skill_id)
        return 1


class BrokenRedis:
    async def zadd(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    async def zincrby(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    async def zrevrange(self, *args, **kwargs):
        raise ConnectionError("redis is down")


class BrokenStore(FakeStore):
    async def increment_skill_load_count(self, skill_id: str) -> int:
        raise RuntimeError("postgres is down")


@pytest.fixture(autouse=True)
def _clean_registry():
    configure_skill_usage(None)
    yield
    configure_skill_usage(None)


@pytest.mark.asyncio
async def test_seeding_copies_database_counts_into_the_redis_zset() -> None:
    redis = FakeRedis()
    store = FakeStore(
        rows=[("u1", "skill-a", 3), ("u1", "skill-b", 9), ("u2", "skill-c", 1)]
    )
    service = SkillUsageService(store, redis)

    await service.seed_from_database()

    assert await redis.zscore(service.key("u1"), "skill-b") == 9
    assert service.top_ids("u1") == ("skill-b", "skill-a")
    assert service.top_ids("u2") == ("skill-c",)
    assert service.top_ids("nobody") is None


@pytest.mark.asyncio
async def test_seeding_never_lowers_a_count_a_worker_already_raised() -> None:
    """启动播种和线上计数会同时发生：GT 保证晚到的旧快照不会把数字改小。"""
    redis = FakeRedis()
    store = FakeStore(rows=[("u1", "skill-a", 2)])
    service = SkillUsageService(store, redis)
    await redis.zadd(service.key("u1"), {"skill-a": 7})

    await service.seed_from_database()

    assert await redis.zscore(service.key("u1"), "skill-a") == 7


@pytest.mark.asyncio
async def test_a_load_increments_redis_and_postgres_and_refreshes_the_cache() -> None:
    redis = FakeRedis()
    store = FakeStore(rows=[("u1", "skill-b", 5)])
    service = SkillUsageService(store, redis)
    await service.seed_from_database()

    await service.record_load("u1", "skill-a")

    assert await redis.zscore(service.key("u1"), "skill-a") == 1
    assert store.incremented == ["skill-a"]
    # 缓存重算：新加载的技能在 5 分之下，但已经在 ZSET 里
    assert service.top_ids("u1") == ("skill-b", "skill-a")


@pytest.mark.asyncio
async def test_a_redis_outage_still_persists_the_database_count() -> None:
    store = FakeStore()
    service = SkillUsageService(store, BrokenRedis())

    await service.record_load("u1", "skill-a")

    assert store.incremented == ["skill-a"]


@pytest.mark.asyncio
async def test_a_database_outage_still_counts_in_redis() -> None:
    redis = FakeRedis()
    service = SkillUsageService(BrokenStore(), redis)

    await service.record_load("u1", "skill-a")

    assert await redis.zscore(service.key("u1"), "skill-a") == 1


@pytest.mark.asyncio
async def test_a_failed_seed_is_not_fatal() -> None:
    redis = FakeRedis()
    service = SkillUsageService(FakeStore(seed_error=RuntimeError("no database")), redis)

    await service.seed_from_database()

    assert service.top_ids("u1") is None


@pytest.mark.asyncio
async def test_the_cache_holds_only_the_top_k() -> None:
    redis = FakeRedis()
    rows = [("u1", f"skill-{index}", index) for index in range(1, 13)]
    service = SkillUsageService(FakeStore(rows=rows), redis, top_k=10)

    await service.seed_from_database()

    cached = service.top_ids("u1")
    assert cached is not None
    assert len(cached) == 10
    assert cached[0] == "skill-12"
    assert "skill-1" not in cached


@pytest.mark.asyncio
async def test_start_seeds_once_and_stop_cancels_the_background_tasks() -> None:
    redis = FakeRedis()
    store = FakeStore(rows=[("u1", "skill-a", 2)])
    service = SkillUsageService(store, redis, refresh_seconds=0)

    await service.start()
    await service._seed_task
    await service.stop()

    assert service.top_ids("u1") == ("skill-a",)
    assert service._seed_task is None
    assert service._refresh_task is None


def test_a_missing_user_or_service_reads_as_unknown() -> None:
    assert top_skill_ids("u1") is None
    configure_skill_usage(SkillUsageService(FakeStore(), BrokenRedis()))
    assert get_skill_usage_service() is not None
    assert top_skill_ids(None) is None
    assert top_skill_ids("never-warmed") is None


def test_the_service_validates_its_configuration() -> None:
    with pytest.raises(ValueError):
        SkillUsageService(FakeStore(), BrokenRedis(), key_prefix="  ")
    with pytest.raises(ValueError):
        SkillUsageService(FakeStore(), BrokenRedis(), top_k=0)
    with pytest.raises(ValueError):
        SkillUsageService(FakeStore(), BrokenRedis(), refresh_seconds=-1)


def test_the_default_key_prefix_is_the_documented_one() -> None:
    service = SkillUsageService(FakeStore(), BrokenRedis())
    assert service.key("u1") == "myflow:skills:usage:u1"
    assert service.top_k == 10

def test_the_usage_config_block_has_the_documented_defaults() -> None:
    from agentkit.config.skills_config import SkillsConfig

    usage = SkillsConfig().usage
    assert usage.enabled is True
    assert usage.redis_url == ""
    assert usage.key_prefix == "myflow:skills:usage"
    assert usage.prompt_top_k == 10
    assert usage.refresh_seconds == 30.0


def test_the_example_config_publishes_the_usage_block() -> None:
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[2] / "config.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["skills"]["usage"] == {
        "enabled": True,
        "redis_url": "$REDIS_URL",
        "key_prefix": "myflow:skills:usage",
        "prompt_top_k": 10,
        "refresh_seconds": 30,
    }
