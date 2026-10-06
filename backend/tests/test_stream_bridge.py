"""M3 §7 StreamBridge：有界裁剪、游标规则、心跳、END 与墓碑。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

import agentkit.runtime.stream_bridge.memory as memory_module
from agentkit.config.stream_config import StreamBridgeConfig
from agentkit.runtime.stream_bridge import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamEvent,
    StreamGap,
)
from agentkit.runtime.stream_bridge.memory import MemoryStreamBridge
from agentkit.runtime.stream_bridge.provider import create_stream_bridge


def _run(scenario) -> None:
    asyncio.run(scenario())


async def _next(stream: Any, timeout: float = 1.0) -> Any:
    return await asyncio.wait_for(anext(stream), timeout=timeout)


async def _expect_no_event(stream: Any, *, settle: float = 0.02) -> None:
    """断言"这条订阅什么都不发"。注意不能用 wait_for 超时：取消会把生成器一起带走。"""
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(settle)
    assert not task.done(), "订阅本不该产出任何东西"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await stream.aclose()


def _bridge(**kwargs: Any) -> MemoryStreamBridge:
    kwargs.setdefault("heartbeat_interval", 0)  # 绝大多数用例只关心事件序列
    return MemoryStreamBridge(**kwargs)


async def _register(bridge: MemoryStreamBridge, run_id: str, **kwargs: Any) -> Any:
    """把订阅驱动到"注册完成"：返回 (stream, 拿到第一条事件的 task)。"""
    stream = bridge.subscribe(run_id, **kwargs)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)  # 让生成器跑到 await queue.get()，此时订阅者已在册
    return stream, task


def _first_subscriber(bridge: MemoryStreamBridge, run_id: str) -> Any:
    return next(iter(bridge._buffers[run_id].subscribers))


def _fill_queue(subscriber: Any) -> None:
    """把某个订阅者的投递队列塞到满（§7.7：慢订阅者就是这么造的）。"""
    while not subscriber.queue.full():
        subscriber.queue.put_nowait(StreamEvent(id="", event="filler", data=None))


def test_window_is_bounded_and_ids_are_assigned_in_order() -> None:
    async def scenario() -> None:
        bridge = _bridge(queue_maxsize=2)
        for i in range(5):
            await bridge.publish("run-1", "values", {"i": i})  # id 1..5，窗口只剩 [4, 5]

        stream = bridge.subscribe("run-1")
        entries = [await _next(stream) for _ in range(2)]
        assert [entry.id for entry in entries] == ["4", "5"]
        assert [entry.data for entry in entries] == [{"i": 3}, {"i": 4}]
        await stream.aclose()

    _run(scenario)


def test_cursor_below_the_watermark_gets_a_gap_with_three_ids() -> None:
    async def scenario() -> None:
        bridge = _bridge(queue_maxsize=2)
        for i in range(5):
            await bridge.publish("run-1", "values", {"i": i})

        stream = bridge.subscribe("run-1", last_event_id="1")  # 1 已经被裁掉
        entry = await _next(stream)
        assert isinstance(entry, StreamGap)
        assert entry.requested_event_id == "1"
        assert entry.earliest_available_event_id == "4"
        assert entry.latest_available_event_id == "5"
        with pytest.raises(StopAsyncIteration):  # gap 是这条订阅的终点
            await _next(stream)

    _run(scenario)


def test_cursor_in_the_window_replays_from_the_next_id() -> None:
    async def scenario() -> None:
        bridge = _bridge(queue_maxsize=8)
        for i in range(4):
            await bridge.publish("run-1", "values", {"i": i})  # id 1..4

        stream = bridge.subscribe("run-1", last_event_id="2")
        entries = [await _next(stream) for _ in range(2)]
        assert [entry.id for entry in entries] == ["3", "4"]
        await stream.aclose()

    _run(scenario)


def test_cursor_equal_to_the_latest_only_waits_for_new_events() -> None:
    async def scenario() -> None:
        bridge = _bridge()
        await bridge.publish("run-1", "values", {"i": 0})  # id 1

        await _expect_no_event(bridge.subscribe("run-1", last_event_id="1"))  # 不重放，只等

        await bridge.publish("run-1", "values", {"i": 1})
        stream = bridge.subscribe("run-1", last_event_id="1")
        assert (await _next(stream)).id == "2"
        await stream.aclose()

    _run(scenario)


@pytest.mark.parametrize("cursor", ["not-an-id", "99"])
def test_unknown_cursor_gets_a_gap(cursor: str) -> None:
    async def scenario() -> None:
        bridge = _bridge()
        await bridge.publish("run-1", "values", {"i": 0})

        entry = await _next(bridge.subscribe("run-1", last_event_id=cursor))
        assert isinstance(entry, StreamGap)
        assert entry.requested_event_id == cursor
        assert (entry.earliest_available_event_id, entry.latest_available_event_id) == ("1", "1")

    _run(scenario)


def test_empty_window_does_not_gap_and_subscription_before_the_first_frame_waits() -> None:
    """§7.4 的常态竞态：准入之后、第一帧之前订阅，必须等到那第一帧，而不是立刻 end。"""

    async def scenario() -> None:
        bridge = _bridge()
        stream, first = await _register(bridge, "run-1", last_event_id="anything")

        await asyncio.sleep(0.02)
        assert not first.done(), "空缓冲既不 gap 也不 END，它只是等"

        await bridge.publish("run-1", "metadata", {"run_id": "run-1"})
        entry = await first
        assert entry.id == "1" and entry.event == "metadata"
        await stream.aclose()

    _run(scenario)


def test_heartbeat_fires_when_idle_and_can_be_turned_off() -> None:
    async def scenario() -> None:
        bridge = MemoryStreamBridge(heartbeat_interval=0.01)
        stream = bridge.subscribe("run-1")
        assert await _next(stream) is HEARTBEAT_SENTINEL
        assert HEARTBEAT_SENTINEL.id == ""  # 不能当续传游标
        await stream.aclose()

        silent = MemoryStreamBridge(heartbeat_interval=0.01)
        await _expect_no_event(silent.subscribe("run-1", heartbeat_interval=0))

    _run(scenario)


def test_end_is_delivered_after_the_retained_window() -> None:
    async def scenario() -> None:
        bridge = _bridge()
        await bridge.publish("run-1", "values", {"i": 0})
        await bridge.publish_end("run-1")
        await bridge.publish_end("run-1")  # 幂等

        stream = bridge.subscribe("run-1")
        assert (await _next(stream)).id == "1"
        assert await _next(stream) is END_SENTINEL

    _run(scenario)


def test_publish_after_end_is_dropped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        bridge = _bridge()
        await bridge.publish_end("run-1")
        with caplog.at_level(logging.WARNING):
            await bridge.publish("run-1", "values", {"late": True})

        stream = bridge.subscribe("run-1")
        assert await _next(stream) is END_SENTINEL  # 迟到的帧没有插进流里

    _run(scenario)
    assert "published after end" in caplog.text


def test_lagged_subscriber_gets_a_gap_and_the_run_keeps_going() -> None:
    async def scenario() -> None:
        bridge = _bridge(queue_maxsize=8)
        stream, first = await _register(bridge, "run-1")
        await bridge.publish("run-1", "values", {"i": 0})
        assert (await first).id == "1"  # 之后生成器停在 yield 上，队列没人取

        _fill_queue(_first_subscriber(bridge, "run-1"))
        await bridge.publish("run-1", "values", {"i": 1})  # 队列满：标记 lagged

        entry = await _next(stream)
        assert isinstance(entry, StreamGap)
        assert entry.requested_event_id == "1"  # 它最后收到的那条
        assert (entry.earliest_available_event_id, entry.latest_available_event_id) == ("1", "2")

        await stream.aclose()

    _run(scenario)


def test_end_is_not_swallowed_by_a_full_queue() -> None:
    async def scenario() -> None:
        bridge = _bridge(queue_maxsize=8)
        stream, first = await _register(bridge, "run-1")
        await bridge.publish("run-1", "values", {"i": 0})
        assert (await first).id == "1"

        _fill_queue(_first_subscriber(bridge, "run-1"))
        await bridge.publish("run-1", "values", {"i": 1})  # lagged = True
        await bridge.publish_end("run-1")  # 队列满，只能记 pending_end

        # pending_end 排在 lagged 前面：流确实结束了，不能降级成"你缺了一段"
        assert await _next(stream) is END_SENTINEL

    _run(scenario)


def test_cleanup_leaves_a_tombstone_that_ends_a_later_subscription() -> None:
    async def scenario() -> None:
        bridge = _bridge()
        await bridge.publish("run-1", "values", {"i": 0})
        await bridge.cleanup("run-1")  # 事件被回收
        await bridge.cleanup("run-1")  # 重复调用无害

        stream = bridge.subscribe("run-1")
        assert await _next(stream) is END_SENTINEL

    _run(scenario)


def test_cleanup_wakes_a_live_subscriber() -> None:
    async def scenario() -> None:
        bridge = _bridge()
        stream, first = await _register(bridge, "run-1")
        await bridge.publish("run-1", "values", {"i": 0})
        assert (await first).id == "1"

        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await bridge.cleanup("run-1", delay=0)

        assert await asyncio.wait_for(pending, timeout=1) is END_SENTINEL

    _run(scenario)


def test_tombstones_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(memory_module, "TOMBSTONE_LIMIT", 1)
        bridge = _bridge()
        for run_id in ("run-a", "run-b"):
            await bridge.publish_end(run_id)
            await bridge.cleanup(run_id)  # run-a 的墓碑被 run-b 挤掉

        assert await _next(bridge.subscribe("run-b")) is END_SENTINEL  # 还是墓碑

        # 退回"从没见过这个 run"：等待，不报错
        await _expect_no_event(bridge.subscribe("run-a"))

    _run(scenario)


def test_subscribers_do_not_affect_each_other() -> None:
    async def scenario() -> None:
        bridge = _bridge(queue_maxsize=8)
        slow_stream, slow_first = await _register(bridge, "run-1")
        slow_subscriber = _first_subscriber(bridge, "run-1")
        fast_stream, fast_first = await _register(bridge, "run-1")

        for i in range(3):
            await bridge.publish("run-1", "values", {"i": i})

        # 快的拿到全部三条，顺序与 id 一致
        fast_entries = [await asyncio.wait_for(fast_first, timeout=1)]
        fast_entries += [await _next(fast_stream) for _ in range(2)]
        assert [entry.id for entry in fast_entries] == ["1", "2", "3"]
        assert [entry.data["i"] for entry in fast_entries] == [0, 1, 2]

        # 慢的落后了，只影响它自己
        _fill_queue(slow_subscriber)
        await bridge.publish("run-1", "values", {"i": 3})
        assert (await asyncio.wait_for(slow_first, timeout=1)).id == "1"
        assert isinstance(await _next(slow_stream), StreamGap)

        await slow_stream.aclose()
        await fast_stream.aclose()

    _run(scenario)


def test_events_are_shared_not_copied() -> None:
    async def scenario() -> None:
        bridge = _bridge()
        await bridge.publish("run-1", "values", {"i": 0})

        left = await _next(bridge.subscribe("run-1"))
        right = await _next(bridge.subscribe("run-1"))
        assert left is right, "fanout 共享同一个事件对象：data 已经是 JSON 值（§7.2）"

    _run(scenario)


def test_unknown_run_does_not_end_the_stream() -> None:
    async def scenario() -> None:
        bridge = _bridge()
        # 存在性判定归调用方：桥只会等（§7.4 的三态表第 4 行）
        await _expect_no_event(bridge.subscribe("never-seen"))

    _run(scenario)


def test_factory_honours_the_config() -> None:
    bridge = create_stream_bridge(StreamBridgeConfig(queue_maxsize=2, heartbeat_interval_seconds=0))
    assert isinstance(bridge, MemoryStreamBridge)
    assert bridge.supports_cross_process is False
    assert bridge._queue_maxsize == 2 and bridge._heartbeat_interval == 0
