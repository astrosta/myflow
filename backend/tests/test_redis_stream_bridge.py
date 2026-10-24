"""M9 §3.6：Redis 版 StreamBridge 与 memory 版跑同一套协议（§11 test_redis_stream_bridge.py）。"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any

import pytest

from agentkit.config.stream_config import StreamBridgeConfig
from agentkit.runtime.stream_bridge import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    MemoryStreamBridge,
    StreamEvent,
    StreamGap,
)
from agentkit.runtime.stream_bridge.provider import create_stream_bridge

fakeredis = pytest.importorskip("fakeredis")
aioredis = pytest.importorskip("fakeredis.aioredis")
redis_module = pytest.importorskip("redis")

from agentkit.runtime.stream_bridge.redis import RedisStreamBridge  # noqa: E402  (RED 阶段不存在)

PREFIX = "test:stream"


def _run(scenario) -> None:
    asyncio.run(scenario())


async def _next(stream: Any, timeout: float = 1.0) -> Any:
    return await asyncio.wait_for(anext(stream), timeout=timeout)


async def _stream_ids(bridge: Any, run_id: str) -> list[str]:
    return [entry.id for entry in await _publish_many(bridge, run_id)]


async def _publish_many(bridge: Any, run_id: str, count: int = 0) -> list[Any]:
    entries = []
    stream = bridge.subscribe(run_id, last_event_id=None)
    for _ in range(count):
        await bridge.publish(run_id, "values", {"n": len(entries)})
    return stream


def _fake_client() -> Any:
    return aioredis.FakeRedis(decode_responses=True)


def _redis(**kwargs: Any) -> Any:
    kwargs.setdefault("queue_maxsize", 256)
    kwargs.setdefault("heartbeat_interval", 0)
    kwargs.setdefault("key_prefix", PREFIX)
    return RedisStreamBridge(_fake_client(), **kwargs)


@pytest.fixture(params=["memory", "redis"])
def make_bridge(request: pytest.FixtureRequest):
    def _make(**kwargs: Any) -> Any:
        kwargs.setdefault("heartbeat_interval", 0)
        if request.param == "memory":
            return MemoryStreamBridge(**kwargs)
        kwargs.setdefault("key_prefix", PREFIX)
        return RedisStreamBridge(_fake_client(), **kwargs)

    return _make


# ---------------------------------------------------------------- 共用协议


def test_replay_then_end(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge()
        await bridge.publish("run-1", "values", {"n": 0})
        await bridge.publish("run-1", "values", {"n": 1})
        await bridge.publish_end("run-1")
        await bridge.publish_end("run-1")  # 幂等

        stream = bridge.subscribe("run-1")
        entries = [await _next(stream) for _ in range(2)]
        assert [entry.data["n"] for entry in entries] == [0, 1]
        assert await _next(stream) is END_SENTINEL

    _run(scenario)


def test_last_event_id_resumes_after_the_cursor(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge()
        for n in range(4):
            await bridge.publish("run-1", "values", {"n": n})

        first = await _next(bridge.subscribe("run-1", last_event_id=None))  # 拿 id 用
        stream = bridge.subscribe("run-1", last_event_id=first.id)
        rest = [await _next(stream) for _ in range(3)]
        assert [entry.data["n"] for entry in rest] == [1, 2, 3]

    _run(scenario)


def test_cursor_below_the_watermark_gets_a_gap(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge(queue_maxsize=2)
        for n in range(5):
            await bridge.publish("run-1", "values", {"n": n})

        # 取第一条 id 当作"已经被裁掉的游标"
        stale = await _next(bridge.subscribe("run-1", last_event_id="not-a-cursor"))  # 先确认 gap 形状
        assert isinstance(stale, StreamGap)
        assert stale.requested_event_id == "not-a-cursor"

        window_stream = bridge.subscribe("run-1")  # 一条订阅里连读两条（每条新订阅都会从头重放）
        window = [await _next(window_stream) for _ in range(2)]
        assert [entry.data["n"] for entry in window] == [3, 4]  # 只剩最后两条
        await window_stream.aclose()

        gap = await _next(bridge.subscribe("run-1", last_event_id=gap_cursor(window)))
        assert isinstance(gap, StreamGap)
        assert gap.requested_event_id == gap_cursor(window)
        assert gap.earliest_available_event_id == window[0].id
        assert gap.latest_available_event_id == window[-1].id

    def gap_cursor(window: list[Any]) -> str:
        return "1" if ":" not in window[0].id else "1-1"

    _run(scenario)


def test_cursor_equal_to_latest_only_waits(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge()
        await bridge.publish("run-1", "values", {"n": 0})
        latest = (await _next(bridge.subscribe("run-1"))).id

        silent = bridge.subscribe("run-1", last_event_id=latest)
        await _expect_no_event(silent)

        await bridge.publish("run-1", "values", {"n": 1})
        after = await _next(bridge.subscribe("run-1", last_event_id=latest))
        assert after.data["n"] == 1

    _run(scenario)


@pytest.mark.parametrize("cursor", ["not-an-id", "99999999-0"])
def test_unknown_cursor_gets_a_gap(make_bridge, cursor: str) -> None:
    async def scenario() -> None:
        bridge = make_bridge()
        await bridge.publish("run-1", "values", {"n": 0})

        entry = await _next(bridge.subscribe("run-1", last_event_id=cursor))
        assert isinstance(entry, StreamGap)
        assert entry.requested_event_id == cursor
        assert entry.earliest_available_event_id is not None

    _run(scenario)


def test_publish_after_end_is_dropped(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge()
        await bridge.publish_end("run-1")
        await bridge.publish("run-1", "values", {"late": True})

        stream = bridge.subscribe("run-1")
        assert await _next(stream) is END_SENTINEL

    _run(scenario)


def test_heartbeat_carries_no_id(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge(heartbeat_interval=0.01)
        stream = bridge.subscribe("run-1")
        entry = await _next(stream)
        assert entry is HEARTBEAT_SENTINEL
        assert entry.id == ""
        await stream.aclose()

    _run(scenario)


def test_subscription_before_the_first_frame_waits(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge()
        stream = bridge.subscribe("run-1")
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        assert not task.done()

        await bridge.publish("run-1", "metadata", {"run_id": "run-1"})
        entry = await asyncio.wait_for(task, timeout=1)
        assert entry.event == "metadata"
        await stream.aclose()

    _run(scenario)


def test_unknown_run_waits_instead_of_ending(make_bridge) -> None:
    async def scenario() -> None:
        bridge = make_bridge()
        await _expect_no_event(bridge.subscribe("never-seen"))

    _run(scenario)


async def _expect_no_event(stream: Any, *, settle: float = 0.05) -> None:
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(settle)
    assert not task.done(), "订阅本不该产出任何东西"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await stream.aclose()


# ---------------------------------------------------------------- Redis 专属


def test_supports_cross_process_is_true() -> None:
    bridge = _redis()
    assert bridge.supports_cross_process is True


def test_event_ids_are_redis_stream_ids() -> None:
    async def scenario() -> None:
        bridge = _redis()
        client = bridge._client
        await bridge.publish("run-1", "values", {"n": 0})
        entry = await _next(bridge.subscribe("run-1"))
        assert entry.id.count("-") == 1, entry.id  # <ms>-<seq>，消费方只做相等比较
        assert await client.xlen(f"{PREFIX}:{{run-1}}:events") == 1

    _run(scenario)


def test_stream_ttl_is_refreshed_on_publish_and_end() -> None:
    async def scenario() -> None:
        bridge = _redis(stream_ttl_seconds=600)
        client = bridge._client
        await bridge.publish("run-1", "values", {"n": 0})
        key = f"{PREFIX}:{{run-1}}:events"
        assert 0 < await client.ttl(key) <= 600

        await client.expire(key, 5)  # 手动压低 TTL，模拟"上次 publish 之后过了很久"
        await bridge.publish("run-1", "values", {"n": 1})
        assert await client.ttl(key) > 5, "每次 publish 都要刷新泄漏安全网的 TTL"

        await client.expire(key, 5)
        await bridge.publish_end("run-1")
        assert await client.ttl(key) > 5

    _run(scenario)


def test_cleanup_drops_the_payload_and_leaves_a_tombstone() -> None:
    async def scenario() -> None:
        bridge = _redis(tombstone_ttl_seconds=120)
        client = bridge._client
        await bridge.publish("run-1", "values", {"n": 0})
        await bridge.cleanup("run-1")
        await bridge.cleanup("run-1")  # 重复调用无害

        assert await client.exists(f"{PREFIX}:{{run-1}}:events") == 0
        assert await client.exists(f"{PREFIX}:{{run-1}}:closed") == 1
        assert 0 < await client.ttl(f"{PREFIX}:{{run-1}}:closed") <= 120

        stream = bridge.subscribe("run-1")
        assert await _next(stream) is END_SENTINEL  # 墓碑：立刻 END

    _run(scenario)


def test_cleanup_wakes_a_blocked_reader() -> None:
    async def scenario() -> None:
        bridge = _redis(tombstone_ttl_seconds=60, read_block_seconds=0.01)
        await bridge.publish("run-1", "values", {"n": 0})
        stream = bridge.subscribe("run-1")
        assert (await _next(stream)).data["n"] == 0

        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)  # 让它阻塞在 XREAD 上
        await bridge.cleanup("run-1")

        assert await asyncio.wait_for(pending, timeout=2) is END_SENTINEL

    _run(scenario)


def test_data_round_trips_as_json() -> None:
    async def scenario() -> None:
        bridge = _redis()
        await bridge.publish("run-1", "values", {"text": "中文", "list": [1, 2], "none": None})
        await bridge.publish("run-1", "values", "纯字符串")
        stream = bridge.subscribe("run-1")
        first, second = await _next(stream), await _next(stream)
        assert first.data == {"text": "中文", "list": [1, 2], "none": None}
        assert second.data == "纯字符串"

    _run(scenario)


def test_factory_builds_a_redis_bridge_from_config() -> None:
    config = StreamBridgeConfig(backend="redis", redis_url="redis://127.0.0.1:6399/7")
    bridge = create_stream_bridge(config)
    assert isinstance(bridge, RedisStreamBridge)
    assert bridge.supports_cross_process is True
    assert bridge._config.redis_url == "redis://127.0.0.1:6399/7"


def test_factory_explains_a_missing_redis_package(monkeypatch: pytest.MonkeyPatch) -> None:
    # 可选依赖没装：from redis.asyncio import Redis 必须抛 ImportError，被工厂包成可读的 RuntimeError
    monkeypatch.setitem(sys.modules, "redis", None)
    monkeypatch.setitem(sys.modules, "redis.asyncio", None)
    with pytest.raises(RuntimeError, match="uv sync --extra redis"):
        create_stream_bridge(StreamBridgeConfig(backend="redis"))


def test_injected_client_is_not_closed_by_the_bridge() -> None:
    async def scenario() -> None:
        client = _fake_client()
        bridge = RedisStreamBridge(client, heartbeat_interval=0)
        await bridge.aclose()
        assert await client.ping() is True  # 谁建的谁关：注入的客户端不归桥管

    _run(scenario)


def test_config_defaults_and_validation() -> None:
    config = StreamBridgeConfig()
    assert config.backend == "memory"
    assert config.redis_url.startswith("redis://")
    assert config.stream_ttl_seconds > 0
    assert config.tombstone_ttl_seconds > 0
    assert config.queue_maxsize == 256

    with pytest.raises(ValueError):
        StreamBridgeConfig(stream_ttl_seconds=0)
    with pytest.raises(ValueError):
        StreamBridgeConfig(backend="pubsub")  # type: ignore[arg-type]


def test_redacted_url_hides_the_password() -> None:
    bridge = RedisStreamBridge(
        _fake_client(), heartbeat_interval=0, redis_url="redis://user:s3cret@127.0.0.1:6379/0"
    )
    target = getattr(bridge, "redacted_url", None)
    assert target is not None and "s3cret" not in target


# ---------------------------------------------------------------- 多 worker 的启动门（M9 §3.5）


def test_multi_worker_requires_the_redis_backend() -> None:
    with pytest.raises(RuntimeError, match="stream_bridge.backend=redis"):
        create_stream_bridge(StreamBridgeConfig(backend="memory"), gateway_workers=2)


def test_single_worker_may_stay_on_memory() -> None:
    bridge = create_stream_bridge(StreamBridgeConfig(backend="memory"), gateway_workers=1)
    assert isinstance(bridge, MemoryStreamBridge)


def test_multi_worker_with_redis_is_allowed() -> None:
    bridge = create_stream_bridge(
        StreamBridgeConfig(backend="redis", redis_url="redis://127.0.0.1:6399/0"), gateway_workers=4
    )
    assert isinstance(bridge, RedisStreamBridge)



def test_publish_after_cleanup_does_not_resurrect_the_run() -> None:
    async def scenario() -> None:
        bridge = _redis(tombstone_ttl_seconds=60)
        client = bridge._client
        await bridge.publish("run-1", "values", {"n": 0})
        await bridge.cleanup("run-1")

        await bridge.publish("run-1", "values", {"late": True})  # detached 任务迟到的帧
        await bridge.publish_end("run-1")

        assert await client.exists(f"{PREFIX}:{{run-1}}:events") == 0, "不许把已回收的 run 复活"
        assert (await _next(bridge.subscribe("run-1"))) is END_SENTINEL

    _run(scenario)

