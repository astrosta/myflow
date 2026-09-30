"""
StreamBridge ABC、StreamEvent、StreamGap、哨兵
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, AsyncIterator

HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL       = StreamEvent(id="", event="__end__",       data=None)

@dataclass(frozen=True, slots=True)
class StreamGap:
    requested_event_id: str | None # 客户端请求的游标，或落后订阅者最后收到的那条
    earliest_available_event_id: str | None # 当前保留窗口下界
    latest_available_event_id: str | None # 当前保留窗口上界

@dataclass(frozen=True, slots=True)
class StreamEvent:
    id: str # 事件id
    event: str # 事件类型
    data: Any # 事件数据

class StreamBridge(abc.ABC):
    supports_cross_process: bool = False

    @abc.abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """Enqueue a single event for *run_id* (producer side)."""

    @abc.abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """Signal that no more events will be produced for *run_id*."""

    @abc.abstractmethod
    def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float | None = None,
    ) -> AsyncIterator[StreamEvent | StreamGap]:
        """Async iterator that yields events for *run_id* (consumer side).

        Yields :data:`HEARTBEAT_SENTINEL` when no event arrives within
        *heartbeat_interval* seconds, or the bridge's configured default when
        omitted. Yields :data:`END_SENTINEL` once the producer calls
        :meth:`publish_end`. Yields :class:`StreamGap` and stops when the
        subscriber has fallen behind retained history.
        """ 

    @abc.abstractmethod
    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """Release resources associated with *run_id*.

        If *delay* > 0 the implementation should wait before releasing,
        giving late subscribers a chance to drain remaining events.
        """

