from __future__ import annotations

import atexit
import contextlib
import logging
import threading
from collections.abc import Iterator

from langgraph.types import Checkpointer

from agentkit.config.checkpointer_config import CheckpointerConfig

logger = logging.getLogger(__name__)

SQLITE_INSTALL_HINT = (
    "checkpointer.type is sqlite but langgraph-checkpoint-sqlite is not installed.\n"
    "    cd backend && uv sync --extra sqlite"
)
POSTGRES_INSTALL_HINT = (
    "checkpointer.type is postgres but langgraph-checkpoint-postgres is not installed.\n"
    "    cd backend && uv sync --extra postgres"
)


_checkpointer_lock = threading.Lock()
_checkpointer: Checkpointer | None = None
_atexit_registered = False


def get_checkpointer() -> Checkpointer:
    """进程级单例。首次调用时创建，进程退出时关闭。Gateway / 长驻服务用这个。"""
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    # 在锁外解析配置：get_app_config() 可能触发配置重载，
    # 而重载的副作用会调用 reset_checkpointer()，那个函数要拿同一把非重入锁。
    config = _resolve_checkpointer_config()

    with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer
        _checkpointer = _make_checkpointer(config)
        _register_atexit()
    return _checkpointer


@contextlib.contextmanager
def checkpointer_context() -> Iterator[Checkpointer]:
    """一次性上下文。每个 with 块自己建连接、退出时销毁。CLI / 测试用这个。"""
    saver = _make_checkpointer(_resolve_checkpointer_config())
    try:
        yield saver
    finally:
        _close_checkpointer(saver)


def reset_checkpointer() -> None:
    """重置单例，关闭连接。测试 teardown 和配置变更后用。"""
    global _checkpointer
    with _checkpointer_lock:
        saver = _checkpointer
        if saver is None:
            return
        _close_checkpointer(saver)
        _checkpointer = None


def _resolve_checkpointer_config() -> CheckpointerConfig:
    from agentkit.config.app_config import get_app_config

    return get_app_config().checkpointer


def _make_checkpointer(config: CheckpointerConfig) -> Checkpointer:
    if config.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Checkpointer: using InMemorySaver (in-process, not persistent)")
        return InMemorySaver()

    if config.type == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL_HINT) from exc
        raise NotImplementedError(
            f"sqlite checkpointer ({SqliteSaver.__name__}) is not implemented until M4"
        )

    if config.type == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise ImportError(POSTGRES_INSTALL_HINT) from exc
        raise NotImplementedError(
            f"postgres checkpointer ({PostgresSaver.__name__}) is not implemented until M4"
        )

    raise ValueError(
        f"Unknown checkpointer type: {config.type!r}. Expected memory, sqlite, or postgres."
    )


def _close_checkpointer(saver: Checkpointer) -> None:
    close = getattr(saver, "close", None)
    if callable(close):
        close()
        return
    exit_ = getattr(saver, "__exit__", None)
    if callable(exit_):
        exit_(None, None, None)


def _register_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(reset_checkpointer)
    _atexit_registered = True
