"""stdio 会话池：owner task 与精确驱逐（M7 §5.3）。

stdio 服务器是**有状态子进程**，会话按 (server_name, user_id:thread_id) 作用域。
两个硬约束：

* ClientSession 建立在 anyio task group 上，__aenter__ / __aexit__ 必须在**同一个 task**。
  同步工具包装每次 asyncio.run 一条新 loop，跨调用进出同一 session 会
  RuntimeError: Attempted to exit cancel scope in a different task。所以每个池化 session
  由专职 _run_session task 拥有：它 enter、把 session 交给调用方、等 close event；
  **所有关闭路径只 signal 那个 event**，由 owner 自己 __aexit__。
* owner 也是唯一能写池子的 writer：initialize() 成功后在同一临界区注册 + resolve future，
  调用方永远拿不到"还绑在某个可能被取消的 caller 上"的半成品 session。

池是**按事件循环**的（弱引用表）：session 绝不跨 loop 复用。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_SESSIONS = 8
DEFAULT_SESSION_INIT_TIMEOUT_SECONDS = 60.0

#: 驱逐时给 owner 自己退出的宽限期；超时就取消它（__aexit__ 仍然发生在 owner task 里）。
_EVICT_GRACE_SECONDS = 0.5

#: 明确的断连才算（§5.3）：普通 isError=true、协议超时、拦截器失败都不驱逐健康会话。
_DISCONNECT_MARKERS = (
    "connection closed",
    "closedresourceerror",
    "brokenresourceerror",
    "endofstream",
    "connection reset",
    "server disconnected",
)

PoolKey = tuple[str, str]  # (server_name, f"{user_id}:{thread_id}")


def pool_key(server: str, *, user_id: str | None, thread_id: str | None) -> PoolKey:
    return (server, f"{user_id}:{thread_id}")


def is_disconnect_error(exc: BaseException) -> bool:
    """只有明确的 Connection closed / AnyIO closed-stream 才算断连。"""
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(marker in text for marker in _DISCONNECT_MARKERS)


@asynccontextmanager
async def _default_connect(params: dict[str, Any]):
    from langchain_mcp_adapters.sessions import create_session

    async with create_session(params) as session:
        await session.initialize()
        yield session


@dataclass
class _PooledSession:
    key: PoolKey
    close_event: asyncio.Event
    owner: asyncio.Task | None = None
    session: Any = None


class McpStdioSessionPool:
    """按 (server, user:thread) 池化 stdio 会话；池满按 LRU 驱逐。"""

    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        session_init_timeout: float | None = DEFAULT_SESSION_INIT_TIMEOUT_SECONDS,
        connect: Any = None,
    ) -> None:
        self._max_sessions = max(1, int(max_sessions))
        self._session_init_timeout = session_init_timeout
        self._connect = connect or _default_connect
        self._sessions: dict[PoolKey, _PooledSession] = {}
        self._closed = False

    # ---- 取用 --------------------------------------------------------------

    async def acquire(
        self,
        server: str,
        *,
        user_id: str | None,
        thread_id: str | None,
        params: dict[str, Any],
    ):
        if self._closed:
            raise RuntimeError("MCP stdio session pool is closed")
        key = pool_key(server, user_id=user_id, thread_id=thread_id)
        pooled = self._sessions.get(key)
        if pooled is not None and pooled.session is not None and not _done(pooled.owner):
            self._touch(key)  # LRU：记在 acquire 上，不记在调用上
            return pooled.session
        if pooled is not None:
            logger.debug("Replacing a dead MCP session for %s", key)
            self._forget(key, pooled)

        while len(self._sessions) >= self._max_sessions:
            await self._evict_lru()

        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        pooled = _PooledSession(key=key, close_event=asyncio.Event())
        pooled.owner = asyncio.create_task(
            self._run_session(key, params, ready, pooled), name=f"mcp-session:{server}"
        )
        self._sessions[key] = pooled
        try:
            if self._session_init_timeout is None:
                return await ready
            return await asyncio.wait_for(ready, timeout=float(self._session_init_timeout))
        except BaseException:
            # 建连失败或超时：只清掉自己这一条，别碰别人的会话
            await self._evict(key, pooled)
            raise

    async def _run_session(
        self,
        key: PoolKey,
        params: dict[str, Any],
        ready: asyncio.Future,
        pooled: _PooledSession,
    ) -> None:
        """owner：enter → 把 session 交出去 → 等 close event → 自己退出。"""
        session: Any = None
        try:
            async with self._connect(params) as session:
                if not ready.done():
                    # 只有 owner 写池子 + resolve future：调用方拿不到半成品
                    pooled.session = session
                    ready.set_result(session)
                await pooled.close_event.wait()
        except BaseException as exc:  # 建连失败也要让等待者醒过来
            if not ready.done():
                if isinstance(exc, asyncio.CancelledError):
                    ready.cancel()
                else:
                    ready.set_exception(exc)
            else:
                logger.debug("MCP session for %s ended: %s", key, exc)
        finally:
            self._forget(key, pooled)

    # ---- 驱逐 --------------------------------------------------------------

    def evict_on_disconnect(self, key: PoolKey, failed_session: Any) -> bool:
        """**只在注册表里的条目仍然是失败的那个 session 对象时**才驱逐（§5.3）。

        晚到的旧错误不能把刚建的替代会话干掉。
        """
        pooled = self._sessions.get(key)
        if pooled is None or pooled.session is not failed_session:
            return False
        self._forget(key, pooled)
        return True

    async def _evict(self, key: PoolKey, pooled: _PooledSession) -> None:
        self._forget(key, pooled)
        owner = pooled.owner
        if owner is None or owner is asyncio.current_task() or owner.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout=_EVICT_GRACE_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        except Exception:  # owner 自己失败过：已经报给等待者了
            logger.debug("MCP session owner for %s ended with an error", key, exc_info=True)

    async def _evict_lru(self) -> None:
        key = next(iter(self._sessions), None)
        if key is None:
            return
        await self._evict(key, self._sessions[key])

    def _forget(self, key: PoolKey, pooled: _PooledSession) -> None:
        """signal close event；**绝不从别的 task __aexit__**（§5.3）。"""
        if self._sessions.get(key) is pooled:
            self._sessions.pop(key, None)
        pooled.close_event.set()

    def _touch(self, key: PoolKey) -> None:
        pooled = self._sessions.pop(key, None)
        if pooled is not None:
            self._sessions[key] = pooled

    # ---- 退役 --------------------------------------------------------------

    def retire(self, reason: str) -> None:
        """§2.3 的会话退役：配置失效 / 用户点了 cache/reset 时把本 loop 的会话全关掉。"""
        logger.debug("Retiring %s MCP stdio session(s): %s", len(self._sessions), reason)
        entries = list(self._sessions.items())
        self._sessions.clear()
        for _, pooled in entries:
            pooled.close_event.set()

    async def aclose(self) -> None:
        self._closed = True
        owners = [pooled.owner for pooled in self._sessions.values() if pooled.owner is not None]
        self.retire("close")
        for owner in owners:
            owner.cancel()
        if owners:
            await asyncio.gather(*owners, return_exceptions=True)

    def session_count(self) -> int:
        return len(self._sessions)


def _done(task: asyncio.Task | None) -> bool:
    return task is None or task.done()


_pools: "weakref.WeakKeyDictionary[Any, McpStdioSessionPool]" = weakref.WeakKeyDictionary()
_pools_guard = threading.Lock()


def get_stdio_pool() -> McpStdioSessionPool:
    """当前事件循环的池。session 绝不跨 loop 复用：别的 loop 上是另一个池。"""
    loop = asyncio.get_running_loop()
    with _pools_guard:
        pool = _pools.get(loop)
        if pool is None:
            pool = McpStdioSessionPool()
            _pools[loop] = pool
            # §2.3：配置失效要退役会话，否则被放弃的加载会把连接留在池子里
            # （函数内 import：cache → tools → session_pool 的模块级反向依赖会成环）
            from agentkit.mcp.cache import register_session_retirer

            register_session_retirer(pool.retire)
        return pool
