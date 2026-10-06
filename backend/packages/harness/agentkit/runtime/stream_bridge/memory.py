"""进程内 StreamBridge。缓冲有界，落后的订阅者拿 gap 然后停（M3 §7.3–§7.4）。

三条纪律（§7 开头）：不认识 HTTP、不持久化、不反压 producer。
所以这里没有任何跨 await 的中间状态：`publish` 的同步段一旦插入 await，
就会出现"事件已在窗口里、但订阅者既没在重放列表也没在队列里"的沉默丢帧。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from agentkit.runtime.stream_bridge.base import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamBridge,
    StreamEvent,
    StreamGap,
)

SUBSCRIBER_QUEUE_MAXSIZE = 64  # 投递队列：不可重放，满了就是这个订阅者落后了
TOMBSTONE_LIMIT = 4096  # 只保留最近这么多条墓碑（约 1 MB 上限，见 §7.4）

logger = logging.getLogger(__name__)


@dataclass(slots=True, eq=False)  # eq=False：订阅者按身份区分，set/discard 靠它
class _Subscriber:
    queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)
    )
    cursor: str | None = None  # 已经交付给它的最后一条事件 id（gap 的 requested_event_id）
    lagged: bool = False  # 投递队列溢出：下次唤醒时发 gap 然后停
    pending_end: bool = False  # END 因队列满没能入队：下次唤醒时直接结束


@dataclass(slots=True)
class _RunBuffer:
    events: deque[StreamEvent] = field(default_factory=deque)  # 保留窗口，左端最老
    next_id: int = 1  # 下一条数据事件的 id
    ended: bool = False  # producer 调过 publish_end
    reclaimed: bool = False  # cleanup 回收过事件：墓碑，订阅它立刻得到 END
    subscribers: set[_Subscriber] = field(default_factory=set)

    @property
    def start_offset(self) -> int:  # watermark：窗口内最老事件的 id；空窗口时等于"下一条"
        return int(self.events[0].id) if self.events else self.next_id

    @property
    def latest_offset(self) -> int:  # 上界：已经分配出去的最后一个 id
        return self.next_id - 1


class MemoryStreamBridge(StreamBridge):
    supports_cross_process: bool = False

    def __init__(self, *, queue_maxsize: int = 256, heartbeat_interval: float = 15.0) -> None:
        self._queue_maxsize = queue_maxsize
        self._heartbeat_interval = heartbeat_interval
        self._buffers: dict[str, _RunBuffer] = {}
        self._tombstones: deque[str] = deque()  # 有界：墓碑被挤出去之后，那个 id 又变回"未知"

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        # 函数体内没有 await，事件循环不会在中间切走——这就是全部的原子性（同 §5.1 的 MemoryRunStore）。
        # 以后谁要在这里加 await（比如 M4 顺手写账本），必须先加一把锁。
        buffer = self._buffers.setdefault(run_id, _RunBuffer())
        if buffer.ended:
            logger.warning("Dropping %r published after end for run %s", event, run_id)
            return

        item = StreamEvent(id=str(buffer.next_id), event=event, data=data)
        buffer.next_id += 1
        buffer.events.append(item)
        while len(buffer.events) > self._queue_maxsize:
            buffer.events.popleft()  # 裁剪最老的：watermark 跟着前进
        self._fanout(buffer, item)

    async def publish_end(self, run_id: str) -> None:
        buffer = self._buffers.setdefault(run_id, _RunBuffer())  # 一条事件都没发过的 run 也留缓冲
        if buffer.ended:
            return  # 幂等：worker 只该调一次，重复不致命
        buffer.ended = True
        self._fanout(buffer, END_SENTINEL)

    async def cleanup(self, run_id: str, *, delay: float = 0.0) -> None:
        """延迟回收。只丢事件载荷，留下一个墓碑——"这条 run 已经结束"这个事实必须保留，
        否则之后订阅它的人会退回"未知 run"分支（§7.4 的三种状态）。"""
        if delay > 0:
            await asyncio.sleep(delay)
        buffer = self._buffers.get(run_id)
        if buffer is None:
            return
        already_reclaimed = buffer.reclaimed
        buffer.events.clear()  # 载荷是大头，先扔
        buffer.ended = True  # 从没调过 publish_end 的 run（强制取消）也在这里收口
        buffer.reclaimed = True
        for subscriber in list(buffer.subscribers):
            try:
                subscriber.queue.put_nowait(END_SENTINEL)
            except asyncio.QueueFull:
                subscriber.pending_end = True  # 还挂着的订阅者不能被吊着（§7.3）
        if not already_reclaimed:  # 重复调用无害，但墓碑只记一次
            self._remember_tombstone(run_id)

    def subscribe(
        self, run_id: str, *, last_event_id: str | None = None, heartbeat_interval: float | None = None
    ) -> AsyncIterator[StreamEvent | StreamGap]:
        """返回异步生成器：注册发生在第一次 __anext__，不在这里（§7.4）。"""

        async def _stream() -> AsyncIterator[Any]:
            # ★ 缓冲按需创建，不能把"没有缓冲"当成"未知 run / 已结束"：
            #   start_run 返回 → 路由建 SSE → worker 才发出第一帧，这段时间里缓冲是空的、
            #   但 run 明明活着。用 .get() 判未知，客户端会在第一帧到来之前就收到 end。
            #   真正的"已结束"由墓碑（reclaimed）表达；存在性判定归调用方（先查 RunManager）。
            buffer = self._buffers.setdefault(run_id, _RunBuffer())
            if buffer.reclaimed:  # 墓碑：这条 run 结束且事件已被回收
                yield END_SENTINEL
                return

            timeout = self._heartbeat_interval if heartbeat_interval is None else heartbeat_interval
            subscriber = _Subscriber()
            # ★ 注册与快照必须在同一个同步段里：中间只要有一个 await，
            #   那期间 publish 的事件就既不在重放列表里、也不在投递队列里，而且不会报任何错。
            buffer.subscribers.add(subscriber)
            replay, ended = _resolve_replay(buffer, last_event_id), buffer.ended
            try:
                if replay.gap is not None:
                    yield replay.gap  # gap 是这条订阅的终点
                    return
                for item in replay.items:
                    subscriber.cursor = item.id  # cursor 必须在 yield 之前推进
                    yield item
                if ended:
                    yield END_SENTINEL  # 订阅发生在 END 之后：先给窗口，再立刻给 END
                    return

                while True:
                    if subscriber.pending_end:  # ★ 先看 END：队列满了，但流确实已经结束
                        yield END_SENTINEL
                        return
                    if subscriber.lagged:
                        yield StreamGap(
                            requested_event_id=subscriber.cursor,
                            earliest_available_event_id=_earliest(buffer),
                            latest_available_event_id=_latest(buffer),
                        )
                        return
                    try:
                        item = await asyncio.wait_for(
                            subscriber.queue.get(), timeout=timeout if timeout > 0 else None
                        )
                    except TimeoutError:
                        yield HEARTBEAT_SENTINEL  # 给消费循环一次检查"客户端还在不在"的机会
                        continue

                    if item is END_SENTINEL:
                        yield END_SENTINEL
                        return
                    subscriber.cursor = item.id
                    yield item
            finally:
                # 正常结束、gap、异常、aclose 四条路径都走这里
                buffer.subscribers.discard(subscriber)

        return _stream()

    def _fanout(self, buffer: _RunBuffer, item: Any) -> None:
        for subscriber in list(buffer.subscribers):  # 快照：finally 里的注销可能发生在这一轮迭代中
            try:
                subscriber.queue.put_nowait(item)
            except asyncio.QueueFull:
                if item is END_SENTINEL:
                    subscriber.pending_end = True  # ★ END 不能被"队列满"吞掉
                else:
                    subscriber.lagged = True  # 普通事件溢出：它下一次唤醒时拿 gap

    def _remember_tombstone(self, run_id: str) -> None:
        self._tombstones.append(run_id)
        while len(self._tombstones) > TOMBSTONE_LIMIT:
            self._buffers.pop(self._tombstones.popleft(), None)  # 淘汰最老的墓碑


def _earliest(buffer: _RunBuffer) -> str | None:
    return buffer.events[0].id if buffer.events else None


def _latest(buffer: _RunBuffer) -> str | None:
    return buffer.events[-1].id if buffer.events else None


@dataclass(frozen=True, slots=True)
class _Replay:
    items: tuple[StreamEvent, ...]  # 要重放的事件，保序
    gap: StreamGap | None  # 非 None 时 items 必为空


def _resolve_replay(buffer: _RunBuffer, last_event_id: str | None) -> _Replay:
    """纯函数，不 await。窗口的读写都在同一个同步段里，所以它看到的永远是自洽的一份。"""
    events = tuple(buffer.events)
    earliest, latest = _earliest(buffer), _latest(buffer)

    def _gap() -> _Replay:
        return _Replay(
            items=(),
            gap=StreamGap(
                requested_event_id=last_event_id,
                earliest_available_event_id=earliest,
                latest_available_event_id=latest,
            ),
        )

    if not events:  # 还没发过事件：等价于"从头订"，不是 gap（§7.3）
        return _Replay(items=(), gap=None)
    if last_event_id in (None, ""):  # 全新订阅：重放整个保留窗口
        return _Replay(items=events, gap=None)

    try:
        cursor = int(last_event_id)
    except ValueError:
        return _gap()  # 认不出的游标：无法核对，宁可让客户端 reload

    if cursor < int(earliest) or cursor > int(latest):
        return _gap()  # 低于 watermark（被裁了）或高于上界（不是这条 run 的）

    return _Replay(items=tuple(e for e in events if int(e.id) > cursor), gap=None)
