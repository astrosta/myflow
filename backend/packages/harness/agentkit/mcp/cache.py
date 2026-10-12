"""MCP 工具缓存的失效开关（M7 §2.3）。

工具的加载本身在 §5（D6–8）。这里只落地"什么时候必须重新加载"：内容签名 +
generation + threading.Condition 的 single-flight，以及会话退役回调。

三条纪律：

1. 判据是内容签名（解析后的路径 + (mtime, size, sha256)），不是 mtime 大小比较。
   严格 mtime > 会漏：同一秒内两次编辑、git checkout 让 mtime 倒退、cp -p / tar
   恢复、网络挂载、切换到另一个 mtime 更老的文件。
2. 热路径上的路径解析把 FileNotFoundError 当"未配置"：显式路径中途消失不该把每次
   get_cached_mcp_tools() 打成 500。真正的 from_file() 调用者仍然要大声失败。
3. 失效只影响**本进程**。多 worker 要逐个打 cache/reset，或者等签名自己失效。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentkit.config.extensions_config import ExtensionsConfig, resolve_extensions_config_path
from agentkit.config.file_signature import ConfigSignature, get_config_signature

logger = logging.getLogger(__name__)

_condition = threading.Condition()
_generation = 0
_session_retirers: list[Callable[[str], None]] = []

#: 加载用的线程锁（纯线程：加载跑在 worker 线程里，§2.2）。和 _condition 分开：
#: 一个保护"谁在加载"，一个播 generation 变化给 waiter。
_cache_lock = threading.Condition()


def mcp_cache_generation() -> int:
    """当前 generation。缓存槽记着它建好时的值。"""
    with _condition:
        return _generation


def register_session_retirer(retirer: Callable[[str], None]) -> Callable[[], None]:
    """§5 的 stdio 会话池把退役动作登记在这里；返回反注册函数。

    退役由 generation 变化触发，所以"加载中签名变了"和"用户点了 cache/reset"走
    同一条路：被放弃的加载不会把连接留在池子里。
    """
    with _condition:
        _session_retirers.append(retirer)

    def _unregister() -> None:
        with _condition:
            try:
                _session_retirers.remove(retirer)
            except ValueError:
                pass

    return _unregister


def invalidate_mcp_cache(*, reason: str) -> int:
    """失效：generation += 1、唤醒 waiter、退役旧会话；返回新的 generation。"""
    global _generation
    with _condition:
        _generation += 1
        generation = _generation
        retirers = list(_session_retirers)
        _condition.notify_all()
    for retirer in retirers:
        try:
            retirer(reason)
        except Exception:  # 退役失败不能挡住失效本身
            logger.exception("MCP session retirer failed during invalidation (%s)", reason)
    return generation


def reset_local_mcp_cache(*, reason: str = "api") -> int:
    """POST /api/mcp/cache/reset 的落点：只清本进程，不广播给别的 worker。"""
    global _cache
    with _cache_lock:
        _cache = None
    return invalidate_mcp_cache(reason=reason)


def wait_for_generation_change(generation: int, *, timeout: float | None = None) -> int:
    """阻塞到 generation 变了（或超时），返回当前值：single-flight 的 waiter 用。

    并发请求共用一次加载；加载期间被失效时，waiter 看到 generation 变了就重来。
    """
    with _condition:
        _condition.wait_for(lambda: _generation != generation, timeout)
        return _generation


@dataclass(frozen=True, slots=True)
class ExtensionsConfigSnapshot:
    """(解析后的路径, 内容签名)。两者都为 None 就是"没配扩展"。"""

    path: Path | None
    signature: ConfigSignature | None


def extensions_config_snapshot(explicit: str | Path | None = None) -> ExtensionsConfigSnapshot:
    """热路径快照：显式/环境变量路径中途消失当"未配置"，不抛。"""
    try:
        path = resolve_extensions_config_path(explicit)
    except FileNotFoundError:
        logger.debug(
            "extensions config disappeared (%s); treating it as unconfigured",
            explicit,
            exc_info=True,
        )
        return ExtensionsConfigSnapshot(path=None, signature=None)
    return ExtensionsConfigSnapshot(
        path=path,
        signature=get_config_signature(path) if path is not None else None,
    )


def extensions_config_changed(
    previous: ExtensionsConfigSnapshot, explicit: str | Path | None = None
) -> bool:
    """加载前后各取一次快照：不一致说明加载结果已经过期，必须丢弃并走退役路径。"""
    return extensions_config_snapshot(explicit) != previous

# --------------------------------------------------------------------------
# §5.1 工具懒加载：single-flight + 内容签名失效
# --------------------------------------------------------------------------

_LOAD_WAIT_SECONDS = 120.0


#: 有服务器挂掉时，多久之后允许再试一次（签名没变也不该把一次抽风钉死一整个进程）。
RETRY_AFTER_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class CacheEntry:
    signature: ExtensionsConfigSnapshot
    tools: tuple[Any, ...]
    failed: tuple[str, ...] = ()
    retry_after: float = 0.0


_cache: CacheEntry | None = None
_loading = False
_missing_adapter_warned = False


def get_cached_mcp_tools() -> list[Any]:
    """第一次用时加载；内容签名变了重新加载。并发请求共用一次加载（§2.3/§5.1）。

    签名不匹配就重来，不"用旧的凑一次"：旧工具绑的是旧配置（可能已经指向另一个
    服务器），用一次就可能把请求发到错的端点。
    """
    global _cache, _loading
    while True:
        signature = extensions_config_snapshot()  # FileNotFoundError 在这里当"未配置"
        with _cache_lock:
            if _cache is not None and _cache.signature == signature and not _should_retry(_cache):
                return list(_cache.tools)
            if _loading:
                _cache_lock.wait(timeout=_LOAD_WAIT_SECONDS)  # 别人在加载：等它，醒来重算签名
                continue
            _loading = True
        try:
            result = _load_tools_from_scratch()
        except BaseException:
            with _cache_lock:
                _loading = False
                _cache_lock.notify_all()
            logger.exception("MCP tool loading failed; continuing without MCP tools")
            return []
        with _cache_lock:
            # finally 兜底：这一段里任何异常都不能把 _loading 永久留在 True 上
            # （留下的 waiter 会一直等到 _LOAD_WAIT_SECONDS 才醒）
            try:
                if extensions_config_snapshot() != signature:
                    # 加载期间配置又变了 → 丢掉这份结果，并**走和正常失效一样的会话退役**（§2.3）
                    logger.info("Extensions config changed while loading MCP tools; discarding")
                    invalidate_mcp_cache(reason="config_changed_during_load")
                    _cache = None
                    continue  # 重来一轮；签名稳定后必然收敛
                if _cache is not None and _cache.signature != signature:
                    invalidate_mcp_cache(reason="config_changed")  # 正常失效：旧会话退役
                _cache = CacheEntry(
                    signature=signature,
                    tools=tuple(result.tools),
                    failed=tuple(result.failed),
                    retry_after=time.monotonic() + RETRY_AFTER_SECONDS,
                )
                return list(result.tools)
            finally:
                if _loading:
                    _loading = False
                    _cache_lock.notify_all()


def _should_retry(entry: CacheEntry) -> bool:
    """有服务器挂了且过了重试窗口 → 允许再连一次（否则一次抽风会钉死一整个进程）。"""
    return bool(entry.failed) and time.monotonic() >= entry.retry_after


@dataclass(frozen=True, slots=True)
class _LoadResult:
    tools: list[Any]
    failed: list[str]


def _load_tools_from_scratch() -> _LoadResult:
    """把签名对应的服务器全部连一遍。跑在**独立 worker 线程**的 loop 里（§2.2）。"""
    config = _extensions_config_lenient()
    if not config.mcp_servers:
        return _LoadResult(tools=[], failed=[])
    try:
        return _run_in_worker(lambda: _load_all_servers(config))
    except ImportError as exc:
        global _missing_adapter_warned
        if not _missing_adapter_warned:
            _missing_adapter_warned = True
            logger.warning(
                "MCP servers are configured but langchain-mcp-adapters is not installed "
                "(uv sync --extra mcp); skipping MCP tools: %s",
                exc,
            )
        else:
            logger.debug("MCP adapter still missing: %s", exc)
        return _LoadResult(tools=[], failed=[name for name in config.mcp_servers])


async def _load_all_servers(config: ExtensionsConfig) -> _LoadResult:
    from agentkit.mcp.client import build_servers_config, enabled_servers
    from agentkit.mcp.interceptors import build_interceptors
    from agentkit.mcp.tools import load_server_tools

    servers = build_servers_config(config, scratch=True)
    tools: list[Any] = []
    failed: list[str] = [
        name for name in enabled_servers(config) if name not in servers
    ]  # 静态配置就被拒的那几台
    for name, params in servers.items():
        server = enabled_servers(config)[name]
        transport = str(params.get("transport"))
        interceptors = build_interceptors(server, config=config, transport=transport)
        try:
            loaded = await _with_timeout(
                load_server_tools(
                    name,
                    params,
                    prefix_enabled=server.tool_name_prefix,
                    interceptors=interceptors,
                    stdio=transport == "stdio",
                    tool_call_timeout=server.tool_call_timeout,
                    server_config=server,
                ),
                server.session_init_timeout,
            )
        except Exception as exc:
            # 挂掉的服务器不能阻塞 make_lead_agent：日志 + 跳过，其它服务器照常（§5.2）
            logger.warning("Skipping MCP server %s: %s: %s", name, type(exc).__name__, exc)
            failed.append(name)
            continue
        tools.extend(loaded)
    return _LoadResult(tools=tools, failed=failed)


async def _with_timeout(awaitable: Any, timeout: float | None) -> Any:
    if timeout is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=float(timeout))


def _run_in_worker(factory: Callable[[], Any]) -> Any:
    """独立线程 + 自己的 event loop：调用方可能在 running loop 里（assemble_agent 就是）。"""
    box: dict[str, Any] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            box["result"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001 - 原样带回调用线程
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_target, name="mcp-tools-load", daemon=True)
    thread.start()
    done.wait()
    if "error" in box:
        raise box["error"]
    return box["result"]


def _extensions_config_lenient() -> ExtensionsConfig:
    """热路径：显式路径中途消失当"未配置"，不打 500（§2.3）。"""
    path = extensions_config_snapshot().path
    if path is None:
        return ExtensionsConfig()
    return ExtensionsConfig.from_file(path)