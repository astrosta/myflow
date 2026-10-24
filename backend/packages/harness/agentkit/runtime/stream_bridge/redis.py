"""Redis 版 StreamBridge：跨进程直播帧（M9 §3.6）。

M3 §7 的协议一字不改，只换存储：

- 每个 run 一条 Redis stream（`XADD ... MAXLEN N`）：保留窗口就是"能重放多久"，超限从左裁。
- END 是流里的一条特殊 entry，**不是**"暂时没有新条目"，所以它永远不会被当成一次空读。
- 心跳是**本地计时**，绝不拿 `XREAD BLOCK` 的超时冒充（§7.5 的验收单）。
- retained key 的 TTL 每次 `publish` / `publish_end` 刷新：泄漏安全网，不是 run 超时。
- `cleanup` 之后留一个带 TTL 的墓碑（`closed`）：订阅它立刻 END，不许退回"未知 run"。
- 三个 key 用 `{run_id}` 当 hash tag，集群下落在同一个 slot。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from agentkit.runtime.stream_bridge.base import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamBridge,
    StreamEvent,
    StreamGap,
)

if TYPE_CHECKING:  # 只在类型检查时认识配置模型，运行时保持依赖轻
    from agentkit.config.stream_config import StreamBridgeConfig

logger = logging.getLogger(__name__)

END_EVENT_NAME = END_SENTINEL.event  # "__end__"：控制对象的名字，业务事件不得占用
EVENT_FIELD = "event"
DATA_FIELD = "data"
FIRST_STREAM_ID = "0-0"  # XREAD 的"从头读"：比 "$" 安全，不会漏掉快照之后、订阅之前的那几帧
READ_COUNT = 128  # 一次 XREAD 最多取多少条，避免大回复
MIN_TTL_SECONDS = 1

_ID_PATTERN = re.compile(r"^(?P<ms>\d{1,20})(?:-(?P<seq>\d{1,20}))?$")

# publish：END 之后一律拒收（丢弃 + 告警），并顺手刷新泄漏安全网的 TTL。
_PUBLISH_LUA = """
local key, ended, closed = KEYS[1], KEYS[2], KEYS[3]
if redis.call('EXISTS', ended) == 1 then return 0 end
if redis.call('EXISTS', closed) == 1 then return 0 end
redis.call('XADD', key, 'MAXLEN', ARGV[1], '*', 'event', ARGV[2], 'data', ARGV[3])
redis.call('EXPIRE', key, ARGV[4])
return 1
"""

# publish_end：幂等；END 是流里的一条 entry，订阅者（含阻塞中的）都会读到它。
_END_LUA = """
local key, ended, closed = KEYS[1], KEYS[2], KEYS[3]
if redis.call('EXISTS', ended) == 1 then return 0 end
if redis.call('EXISTS', closed) == 1 then return 0 end
redis.call('XADD', key, 'MAXLEN', ARGV[1], '*', 'event', ARGV[2], 'data', '')
redis.call('SET', ended, '1', 'EX', ARGV[3])
redis.call('EXPIRE', key, ARGV[3])
return 1
"""

# cleanup：只丢载荷，留墓碑——"这条 run 已经结束"这个事实必须保留（同 §7.4 的三态表）。
_CLEANUP_LUA = """
redis.call('DEL', KEYS[1])
redis.call('DEL', KEYS[2])
redis.call('SET', KEYS[3], '1', 'EX', ARGV[1])
return 1
"""


class _Keys:
    """三个 key 共享 `{run_id}` hash tag：集群下同 slot，Lua 里可以一次操作。"""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def events(self, run_id: str) -> str:
        return f"{self._prefix}:{{{run_id}}}:events"

    def ended(self, run_id: str) -> str:
        return f"{self._prefix}:{{{run_id}}}:ended"

    def closed(self, run_id: str) -> str:
        return f"{self._prefix}:{{{run_id}}}:closed"


class RedisStreamBridge(StreamBridge):
    """跨进程实现。消费者在别的进程里也能订到同一条 run 的帧。"""

    supports_cross_process: bool = True

    def __init__(
        self,
        client: Any,
        *,
        queue_maxsize: int = 256,
        heartbeat_interval: float = 15.0,
        stream_ttl_seconds: float = 3600.0,
        tombstone_ttl_seconds: float = 300.0,
        key_prefix: str = "myflow:stream",
        read_block_seconds: float = 5.0,
        redis_url: str | None = None,
        owns_client: bool = False,
    ) -> None:
        self._client = client
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._heartbeat_interval = heartbeat_interval
        self._stream_ttl = max(MIN_TTL_SECONDS, int(round(stream_ttl_seconds)))
        self._tombstone_ttl = max(MIN_TTL_SECONDS, int(round(tombstone_ttl_seconds)))
        self._keys = _Keys(key_prefix)
        self._read_block = read_block_seconds
        self._redis_url = redis_url
        self._owns_client = owns_client
        self._config: StreamBridgeConfig | None = None
        self._publish_script = client.register_script(_PUBLISH_LUA)
        self._end_script = client.register_script(_END_LUA)
        self._cleanup_script = client.register_script(_CLEANUP_LUA)

    # ---- 构造与生命周期 ----

    @classmethod
    def from_config(cls, config: StreamBridgeConfig) -> "RedisStreamBridge":
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # 可选依赖：没装就在启动时明确报错，不要等第一帧
            raise RuntimeError(
                "stream_bridge.backend=redis requires the redis package; "
                "install it with: cd backend && uv sync --extra redis"
            ) from exc

        client = Redis.from_url(
            config.redis_url,
            decode_responses=True,  # 只读写字符串：payload 是我们自己 json.dumps 的
            socket_timeout=config.socket_timeout_seconds,
            socket_connect_timeout=config.socket_connect_timeout_seconds,
            health_check_interval=int(config.health_check_interval_seconds),
            max_connections=config.max_connections,
        )
        bridge = cls(
            client,
            queue_maxsize=config.queue_maxsize,
            heartbeat_interval=config.heartbeat_interval_seconds,
            stream_ttl_seconds=config.stream_ttl_seconds,
            tombstone_ttl_seconds=config.tombstone_ttl_seconds,
            key_prefix=config.key_prefix,
            read_block_seconds=config.read_block_seconds,
            redis_url=config.redis_url,
            owns_client=True,
        )
        bridge._config = config
        # 只打印抹掉密码的形态：URL 里通常带着凭据，不能原样进日志
        logger.info("StreamBridge backend=redis target=%s", bridge.redacted_url)
        return bridge

    async def aclose(self) -> None:
        """只关自己建的连接池：注入进来的客户端归调用方管。"""
        if self._owns_client:
            await self._client.aclose()

    @property
    def redacted_url(self) -> str:
        """日志/报错里用的 URL：密码必须抹掉。"""
        return _redact_url(self._redis_url or "")

    # ---- 生产侧 ----

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        accepted = await self._publish_script(
            keys=[self._keys.events(run_id), self._keys.ended(run_id), self._keys.closed(run_id)],
            args=[self._queue_maxsize, event, _encode(data), self._stream_ttl],
        )
        if not int(accepted):
            logger.warning("Dropping %r published after end for run %s", event, run_id)

    async def publish_end(self, run_id: str) -> None:
        await self._end_script(
            keys=[self._keys.events(run_id), self._keys.ended(run_id), self._keys.closed(run_id)],
            args=[self._queue_maxsize, END_EVENT_NAME, self._stream_ttl],
        )

    async def cleanup(self, run_id: str, *, delay: float = 0.0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        # 先补一条 END：还阻塞在 XREAD 上的订阅者立刻拿得到，而不是等下一次 block 超时
        await self.publish_end(run_id)
        await self._cleanup_script(
            keys=[
                self._keys.events(run_id),
                self._keys.ended(run_id),
                self._keys.closed(run_id),
            ],
            args=[self._tombstone_ttl],
        )

    # ---- 消费侧 ----

    def subscribe(
        self, run_id: str, *, last_event_id: str | None = None, heartbeat_interval: float | None = None
    ) -> AsyncIterator[StreamEvent | StreamGap]:
        """返回异步生成器：注册发生在第一次 __anext__（同 memory 实现）。"""

        async def _stream() -> AsyncIterator[Any]:
            events_key = self._keys.events(run_id)
            # 墓碑：这条 run 结束且载荷已被回收 → 立刻 END，不许退回"未知 run"
            if await self._client.exists(self._keys.closed(run_id)):
                yield END_SENTINEL
                return

            interval = self._heartbeat_interval if heartbeat_interval is None else heartbeat_interval
            earliest, latest = await self._window(events_key)

            replay_from, gap = _resolve_replay_start(last_event_id, earliest, latest)
            if gap is not None:
                yield gap
                return

            # 起点永远是"上一条已交付的 id"：并发裁剪让重放变空时也不会退回 0-0 重放一遍
            last_id = replay_from or last_event_id or FIRST_STREAM_ID
            if replay_from is not None:
                async for entry in self._replay(events_key, replay_from):
                    if entry is END_SENTINEL:
                        yield END_SENTINEL
                        return
                    last_id = entry.id
                    yield entry

            if await self._client.exists(self._keys.ended(run_id)):
                yield END_SENTINEL  # END 可能已经被窗口裁掉：以标记为准
                return

            async for entry in self._live(events_key, run_id, last_id, interval):
                yield entry

        return _stream()

    async def _window(self, events_key: str) -> tuple[str | None, str | None]:
        """返回保留窗口的 (earliest, latest)；空窗口时都是 None（§7.3 的 gap 字段语义）。"""
        first = await self._client.xrange(events_key, "-", "+", count=1)
        last = await self._client.xrevrange(events_key, "+", "-", count=1)
        return (
            first[0][0] if first else None,
            last[0][0] if last else None,
        )

    async def _replay(self, events_key: str, start: str) -> AsyncIterator[Any]:
        cursor = f"({start}" if start != FIRST_STREAM_ID else "-"
        while True:
            rows = await self._client.xrange(events_key, cursor, "+", count=READ_COUNT)
            if not rows:
                return
            for entry_id, fields in rows:
                if fields.get(EVENT_FIELD) == END_EVENT_NAME:
                    yield END_SENTINEL
                    return
                yield _to_event(entry_id, fields)
            if len(rows) < READ_COUNT:
                return
            cursor = f"({rows[-1][0]}"

    async def _live(
        self, events_key: str, run_id: str, last_id: str, heartbeat_interval: float
    ) -> AsyncIterator[Any]:
        block_ms = _block_ms(self._read_block, heartbeat_interval)
        last_beat = time.monotonic()
        closed_key = self._keys.closed(run_id)
        while True:
            reply = await self._client.xread({events_key: last_id}, block=block_ms, count=READ_COUNT)
            if not reply:
                # 空读：先看墓碑（cleanup 可能把流删掉了），再按本地计时发心跳
                if await self._client.exists(closed_key):
                    yield END_SENTINEL
                    return
                now = time.monotonic()
                if heartbeat_interval > 0 and now - last_beat >= heartbeat_interval:
                    last_beat = now
                    yield HEARTBEAT_SENTINEL
                continue

            for entry_id, fields in reply[0][1]:
                last_id = entry_id
                if fields.get(EVENT_FIELD) == END_EVENT_NAME:
                    yield END_SENTINEL
                    return
                yield _to_event(entry_id, fields)


def _to_event(entry_id: str, fields: dict[str, Any]) -> StreamEvent:
    return StreamEvent(
        id=str(entry_id),
        event=str(fields.get(EVENT_FIELD) or ""),
        data=_decode(fields.get(DATA_FIELD)),
    )


def _resolve_replay_start(
    last_event_id: str | None, earliest: str | None, latest: str | None
) -> tuple[str | None, StreamGap | None]:
    """纯函数：把 `Last-Event-ID` 翻译成"从头重放 / 从某条之后重放 / 发 gap"（§7.4）。"""
    if earliest is None:  # 空窗口：等价于"从头订"，不是 gap
        return None, None
    if last_event_id in (None, ""):  # 全新订阅：重放整个保留窗口
        return FIRST_STREAM_ID, None

    cursor = _parse_stream_id(last_event_id)
    if cursor is None:  # 认不出的游标：无法核对，宁可让客户端 reload
        return None, _gap(last_event_id, earliest, latest)

    if cursor < _parse_stream_id(earliest) or cursor > _parse_stream_id(latest):
        return None, _gap(last_event_id, earliest, latest)

    if last_event_id == latest:
        return None, None  # 已经在窗口尾部：只等新事件
    return last_event_id, None


def _gap(requested: str | None, earliest: str | None, latest: str | None) -> StreamGap:
    return StreamGap(
        requested_event_id=requested,
        earliest_available_event_id=earliest,
        latest_available_event_id=latest,
    )


def _block_ms(read_block_seconds: float, heartbeat_interval: float) -> int:
    """XREAD 的阻塞上限只负责"醒来"，心跳按本地计时；两者取小的那个。"""
    window = read_block_seconds
    if heartbeat_interval > 0:
        window = min(window, heartbeat_interval)
    return max(1, int(window * 1000))


def _parse_stream_id(value: str) -> tuple[int, int] | None:
    """Redis stream id 是 `<ms>-<seq>`。只有桥自己比较时解析，消费者不得解析（§7.2）。"""
    match = _ID_PATTERN.match(str(value))
    if match is None:
        return None
    return int(match.group("ms")), int(match.group("seq") or 0)


def _encode(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _decode(raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _redact_url(url: str) -> str:
    """把 `redis://user:password@host:port/db` 里的密码抹掉，其余原样。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "redis://***"
    if not parts.hostname:
        return urlunsplit(parts)
    netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}:***@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
