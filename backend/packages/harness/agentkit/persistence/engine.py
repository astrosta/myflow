"""Process-wide async SQLAlchemy engine for memory, sqlite, and postgres."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Literal
from weakref import WeakKeyDictionary

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from agentkit.config.database_config import DatabaseConfig

logger = logging.getLogger(__name__)

_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

POSTGRES_DRIVER_HINT = (
    "database.backend is postgres but asyncpg is not installed.\n"
    "    cd backend && uv sync --extra postgres"
)

_engine: AsyncEngine | None = None
_session_factory: LoopAwareSessionFactory | None = None
# Same-process bootstrap serialization. Cross-process exclusion is §3.2.
_sqlite_lock = asyncio.Lock()


def _json_serializer(obj: object) -> str:
    """Keep non-ASCII readable. ``default=str`` covers datetime and UUID."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _validate_postgres_schema(schema: str) -> str:
    if not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError(
            "database.postgres_schema must match [A-Za-z_][A-Za-z0-9_]* "
            f"(got {schema!r})"
        )
    return schema


def _require_asyncpg() -> None:
    try:
        import asyncpg  # noqa: F401
    except ImportError as exc:
        raise ImportError(POSTGRES_DRIVER_HINT) from exc


class LoopAwareSessionFactory:
    """Return a session bound to the running event loop.

    Subagents run on a process-level isolated loop. A single asyncpg pool is
    bound to the loop that opened its connections, so reusing the main-loop
    session factory there raises "Future attached to a different loop".
    The primary factory keeps the configured pool; other loops lazily get a
    NullPool engine, so a connection can never be checked back into a pool
    owned by another loop.

    Repositories only call the factory; kw and attribute access delegate to
    the primary factory so existing tests and typing expectations still work.
    """

    def __init__(
        self,
        *,
        primary: async_sessionmaker[AsyncSession],
        primary_loop: asyncio.AbstractEventLoop,
        build_isolated_engine: Callable[[], AsyncEngine],
    ) -> None:
        self._primary = primary
        self._primary_loop = primary_loop
        self._build_isolated_engine = build_isolated_engine
        self._factories: WeakKeyDictionary[
            asyncio.AbstractEventLoop, async_sessionmaker[AsyncSession]
        ] = WeakKeyDictionary()
        self._engines: WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncEngine] = (
            WeakKeyDictionary()
        )

    def __call__(self) -> AsyncSession:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._primary()
        if loop is self._primary_loop:
            return self._primary()
        factory = self._factories.get(loop)
        if factory is None:
            engine = self._build_isolated_engine()
            factory = async_sessionmaker(engine, expire_on_commit=False)
            self._engines[loop] = engine
            self._factories[loop] = factory
        return factory()

    @property
    def kw(self) -> dict[str, Any]:
        return dict(self._primary.kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)

    async def dispose_isolated_engines(self) -> None:
        engines = list(self._engines.items())
        self._engines.clear()
        self._factories.clear()
        current = asyncio.get_running_loop()
        for loop, engine in engines:
            try:
                if loop.is_running() and loop is not current:
                    future = asyncio.run_coroutine_threadsafe(engine.dispose(), loop)
                    await asyncio.wait_for(asyncio.wrap_future(future), timeout=5)
                else:
                    await engine.dispose()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logger.warning(
                    "Failed to dispose a loop-local DB engine", exc_info=True
                )


def _create_engine(
    backend: Literal["sqlite", "postgres"],
    *,
    url: str,
    echo: bool,
    pool_size: int,
    postgres_schema: str,
    isolated: bool,
) -> AsyncEngine:
    if backend == "sqlite":
        kwargs: dict[str, Any] = {
            "echo": echo,
            "json_serializer": _json_serializer,
        }
        if isolated:
            kwargs["poolclass"] = NullPool
        engine = create_async_engine(url, **kwargs)
        _install_sqlite_pragmas(engine)
        return engine

    _require_asyncpg()
    connect_args: dict[str, object] = {"command_timeout": 30}
    if postgres_schema:
        connect_args["server_settings"] = {"search_path": postgres_schema}
    kwargs = {
        "echo": echo,
        "json_serializer": _json_serializer,
        "connect_args": connect_args,
    }
    if isolated:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = pool_size
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 300
    return create_async_engine(url, **kwargs)


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """PRAGMAs are per connection. A one-shot at startup does not cover the pool."""

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_pragmas(dbapi_conn, _record) -> None:
        # Autocommit at the DBAPI so SQLAlchemy's begin event owns the transaction.
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _sqlite_begin_immediate(conn) -> None:
        """默认的 deferred 事务在先读后写时才升级锁。BEGIN IMMEDIATE 一开始就拿写锁。"""
        conn.exec_driver_sql("BEGIN IMMEDIATE")


async def _create_postgres_schema(engine: AsyncEngine, schema: str) -> None:
    name = _validate_postgres_schema(schema)
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {name}"))


async def _run_bootstrap(engine: AsyncEngine, *, backend: str) -> None:
    # Imported lazily so bootstrap can later import ``_sqlite_lock`` from here.
    from agentkit.persistence.bootstrap import bootstrap_schema

    await bootstrap_schema(engine, backend=backend)


async def init_engine(
    backend: Literal["memory", "sqlite", "postgres"],
    *,
    url: str = "",
    echo: bool = False,
    pool_size: int = 5,
    sqlite_dir: str = "",
    postgres_schema: str = "",
) -> None:
    """Create the process engine. ``memory`` leaves the session factory as None.

    ``url`` must already include the driver (``+aiosqlite`` / ``+asyncpg``).
    """
    global _engine, _session_factory
    if backend == "memory":
        return
    if backend not in ("sqlite", "postgres"):
        raise ValueError(
            f"Unknown database backend: {backend!r}. Expected memory, sqlite, or postgres."
        )
    if _engine is not None:
        raise RuntimeError(
            "persistence engine is already initialized; call close_engine() first"
        )

    if postgres_schema:
        _validate_postgres_schema(postgres_schema)

    if backend == "sqlite":
        await asyncio.to_thread(os.makedirs, sqlite_dir or ".", exist_ok=True)
    engine = _create_engine(
        backend,
        url=url,
        echo=echo,
        pool_size=pool_size,
        postgres_schema=postgres_schema,
        isolated=False,
    )

    _engine = engine
    try:
        if backend == "postgres" and postgres_schema:
            await _create_postgres_schema(engine, postgres_schema)
        _session_factory = LoopAwareSessionFactory(
            primary=async_sessionmaker(engine, expire_on_commit=False),
            primary_loop=asyncio.get_running_loop(),
            build_isolated_engine=lambda: _create_engine(
                backend,
                url=url,
                echo=echo,
                pool_size=pool_size,
                postgres_schema=postgres_schema,
                isolated=True,
            ),
        )
        await _run_bootstrap(engine, backend=backend)
    except BaseException:
        await close_engine()
        raise


async def init_engine_from_config(db: DatabaseConfig) -> None:
    """Called once from lifespan. ``backend=memory`` does not open a database."""
    await init_engine(
        db.backend,
        url=db.app_sqlalchemy_url,
        echo=db.echo_sql,
        pool_size=db.pool_size,
        sqlite_dir=db.sqlite_dir,
        postgres_schema=db.postgres_schema,
    )


def get_session_factory() -> LoopAwareSessionFactory | None:
    """None means repositories should use their in-process implementation."""
    return _session_factory


async def close_engine() -> None:
    """Clear the singleton, then dispose every engine it pointed at."""
    global _engine, _session_factory
    factory, _session_factory = _session_factory, None
    engine, _engine = _engine, None
    if factory is not None:
        with suppress(Exception):
            await factory.dispose_isolated_engines()
    if engine is not None:
        await engine.dispose()
