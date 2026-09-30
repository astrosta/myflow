from typing import Any, AsyncIterator
import asyncio
from dataclasses import dataclass, field
from collections import deque
from agentkit.runtime.stream_bridge.base import StreamBridge, StreamGap, StreamEvent, END_SENTINEL, HEARTBEAT_SENTINEL
import logging

SUBSCRIBER_QUEUE_MAXSIZE = 64 # 投递队列：不可重放，满了就是这个订阅者落后了

logger = logging.getLogger(__name__)

@dataclass(slots=True)
class _Subscriber:
    queue: asyncio.Queue[Any] = field(default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE))
    cursor: str | None = None # 游标，最新订阅的事件id
    lagged: bool = False # 投递队列溢出：下次唤醒时发gap然后停止订阅

@dataclass(slots=True)
class _RunBuffer:
    events: deque[StreamEvent] = field(default_factory=deque) # 事件缓冲区
    next_id: int = 1 # 下一条数据事件的id
    ended: bool = False # producer 调过 publish_end
    subscribes: set[_Subscriber] = field(default_factory=set) # 订阅者集合

    @property
    def start_offset(self) -> int: # 窗口内最老事件的id；空窗口时等一self.next_id
        return int(self.events[0].id) if self.events else self.next_id
    
    @property
    def latest_offset(self) -> int: # 
        return self.next_id - 1

class MemoryStreamBridge(StreamBridge):
    supports_cross_process: bool = False

    def __init__(self, *, queue_maxsize: int = 256, heartbeat_interval: float = 15.0) -> None:
        self._queue_maxsize = queue_maxsize
        self._heartbeat_interval = heartbeat_interval
        self._buffers: dict[str, _RunBuffer] = {}

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        buffer = self._buffers.setdefault(run_id, _RunBuffer())
        if buffer.ended:
            logger.warning("Dropping %r published after end for run %s", event, run_id)
            return
        
        item = StreamEvent(id=str(buffer.next_id), event=event, data=data)
        buffer.next_id += 1
        buffer.events.append(item)
        while len(buffer.events) > self._queue_maxsize:
            buffer.events.popleft()
        self._fanout(buffer, item)

    async def publish_end(self, run_id: str) -> None:
        buffer = self._buffers.get(run_id)
        if buffer.ended:
            return
        buffer.ended = True
        self._fanout(buffer, END_SENTINEL)

    def _fanout(self, buffer: _RunBuffer, item: Any) -> None:
        for subscriber in list(buffer.subscribes):
            try:
                subscriber.queue.put_nowait(item)
            except asyncio.QueueFull:
                subscriber.lagged = True
    
    def subscribe(
        self, run_id: str, *, last_event_id: str | None = None, heartbeat_interval: float | None = None
    ) -> AsyncIterator[StreamEvent | StreamGap]:
        async def _stream() -> AsyncIterator[Any]:
            buffer = self._buffers.get(run_id)
            if buffer is None:
                yield END_SENTINEL
                return
            
            timeout = self._heartbeat_interval if heartbeat_interval is None else heartbeat_interval
            subscriber = _Subscriber()
            buffer.subscribes.add(subscriber)
            replay, ended = _resolve_replay(buffer, last_event_id), buffer.ended
            try:
                if replay.gap is not None:
                    yield replay.gap
                    return
                for item in replay.items:
                    subscriber.cursor = item.id
                    yield item
                if ended:
                    yield END_SENTINEL
                    return
                
                while True:
                    if subscriber.lagged:
                        yield StreamGap(
                            requested_event_id=subscriber.cursor,
                            earliest_available_event_id=buffer.start_offset,
                            latest_available_event_id=buffer.latest_offset
                        )
                        return
                    try:
                        item = await asyncio.wait_for(
                            subscriber.queue.get(), timeout=timeout if timeout > 0 else None
                        )
                    except TimeoutError:
                        yield HEARTBEAT_SENTINEL
                        continue
                    
                    if item is END_SENTINEL:
                        yield END_SENTINEL
                        return
                    subscriber.cursor = item.id
                    yield item
            finally:
                buffer.subscribes.discard(subscriber)
        return _stream()

def _earliest(buffer: _RunBuffer) -> str | None:
    return buffer.events[0].id if buffer.events else None

def _latest(buffer: _RunBuffer) -> str | None:
    return buffer.events[-1].id if buffer.events else None

@dataclass(frozen=True, slots=True)
class _Replay:
    items: tuple[StreamEvent, ...] #要重放的事件，保序
    gap: StreamGap | None  # 非None时，items必为空

def _resolve_replay(buffer: _RunBuffer, last_event_id: str | None) -> _Replay:
    events = tuple(buffer.events)

    earliest, latest = _earliest(buffer), _latest(buffer)

    def _gap() -> _Replay:
        return _Replay(items=(), gap=StreamGap(
            requested_event_id=last_event_id,
            earliest_available_event_id=earliest,
            latest_available_event_id=latest
        ))
    
    if not events:
        return _Replay(items=(), gap=None)
    if last_event_id in (None, ""):
        return _Replay(items=events, gap=None)

    try:
        cursor = int(last_event_id)
    except ValueError:
        return _gap()
    
    if cursor < earliest or cursor > latest:
        return _gap()
    
    return _Replay(items=tuple(e for e in events if int(e.id) > cursor), gap=None)

