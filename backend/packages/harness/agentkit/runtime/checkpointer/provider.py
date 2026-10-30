from __future__ import annotations

import atexit
import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from langgraph.types import Checkpointer

from agentkit.config import CheckpointerConfig, DatabaseConfig

logger = logging.getLogger(__name__)

SQLITE_INSTALL_HINT = (
    "checkpointer.type is sqlite but langgraph-checkpoint-sqlite is not installed.\n"
    "    cd backend && uv sync --extra sqlite"
)
POSTGRES_INSTALL_HINT = (
    "checkpointer.type is postgres but langgraph-checkpoint-postgres is not installed.\n"
    "    cd backend && uv sync --extra postgres"
)

_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=30000;",
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
    config, database = _resolve_checkpointer_settings()

    with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer
        _checkpointer = _make_checkpointer(config, database)
        _register_atexit()
    return _checkpointer


@contextlib.contextmanager
def checkpointer_context() -> Iterator[Checkpointer]:
    """一次性上下文。每个 with 块自己建连接、退出时销毁。CLI / 测试用这个。"""
    config, database = _resolve_checkpointer_settings()
    saver = _make_checkpointer(config, database)
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


def _resolve_checkpointer_settings() -> tuple[CheckpointerConfig, DatabaseConfig]:
    """一次读取两个配置段，避免重载后 checkpointer 与 database 来自不同快照。"""
    from agentkit.config import get_app_config

    app_config = get_app_config()
    return app_config.checkpointer, app_config.database


def _make_checkpointer(
    config: CheckpointerConfig,
    database: DatabaseConfig | None = None,
) -> Checkpointer:
    if config.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Checkpointer: using InMemorySaver (in-process, not persistent)")
        return InMemorySaver()

    if config.type == "sqlite":
        return _make_sqlite_checkpointer(database)

    if config.type == "postgres":
        return _make_postgres_checkpointer(database)

    raise ValueError(
        f"Unknown checkpointer type: {config.type!r}. Expected memory, sqlite, or postgres."
    )


def _make_sqlite_checkpointer(database: DatabaseConfig | None) -> Checkpointer:
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise ImportError(SQLITE_INSTALL_HINT) from exc

    if database is None:
        database = _resolve_checkpointer_settings()[1]
    path = Path(database.sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        for statement in _SQLITE_PRAGMAS:
            conn.execute(statement)
        saver = SqliteSaver(conn)
        saver.setup()
    except Exception:
        conn.close()
        raise
    logger.info("Checkpointer: using SqliteSaver at %s", path)
    return saver


def _make_postgres_checkpointer(database: DatabaseConfig | None) -> Checkpointer:
    PostgresSaver, Connection, dict_row = _postgres_components()
    if database is None:
        database = _resolve_checkpointer_settings()[1]
    if database.backend != "postgres":
        raise ValueError(
            "checkpointer.type is postgres but database.backend is "
            f"{database.backend!r}; set database.backend: postgres and database.url"
        )
    if not database.postgres_url:
        raise ValueError("database.url is required when checkpointer.type is postgres")
    dsn = database.checkpointer_dsn

    conn = Connection.connect(
        dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        keepalives=1,
        keepalives_idle=60,
    )
    try:
        if database.postgres_schema:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{database.postgres_schema}"')
        saver = PostgresSaver(conn=conn)
        saver.setup()
    except Exception:
        conn.close()
        raise
    logger.info(
        "Checkpointer: using PostgresSaver (schema=%s)",
        database.postgres_schema or "public",
    )
    return saver


def _postgres_components():
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ImportError(POSTGRES_INSTALL_HINT) from exc
    return PostgresSaver, Connection, dict_row


def _close_checkpointer(saver: Checkpointer) -> None:
    close = getattr(saver, "close", None)
    if callable(close):
        close()
        return
    conn = getattr(saver, "conn", None)
    conn_close = getattr(conn, "close", None)
    if callable(conn_close):
        conn_close()
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
