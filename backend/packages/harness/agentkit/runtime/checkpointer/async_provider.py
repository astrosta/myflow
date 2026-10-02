"""Checkpointer connections are not the ORM engine. setup() runs before the saver is used."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from agentkit.config.app_config import AppConfig

logger = logging.getLogger(__name__)

POSTGRES_CHECKPOINT_HINT = (
    "database.backend is postgres but langgraph-checkpoint-postgres is not installed.\n"
    "    cd backend && uv sync --extra postgres"
)

_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=30000;",
)


def translate_checkpoint_import_error(exc: BaseException) -> None:
    """Turn a missing Postgres extra into the same hint the ORM prints for asyncpg."""
    name = getattr(exc, "name", "") or ""
    if isinstance(exc, ImportError) and (
        str(name).startswith("langgraph.checkpoint.postgres") or name in {"psycopg", "psycopg_pool"}
    ):
        raise ImportError(POSTGRES_CHECKPOINT_HINT) from exc
    raise exc


def _postgres_components():
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        translate_checkpoint_import_error(exc)
        raise
    return AsyncPostgresSaver, AsyncConnectionPool, dict_row


@asynccontextmanager
async def make_checkpointer(app_config: AppConfig) -> AsyncIterator[BaseCheckpointSaver]:
    """Lifespan holds this for the process. ``setup()`` finishes before the saver is yielded."""
    database = app_config.database
    if database.backend == "memory":
        yield InMemorySaver()
        return
    if database.backend == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        conn_str = await asyncio.to_thread(_prepare_sqlite_path, database.sqlite_path)
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            await _apply_sqlite_pragmas(saver.conn)
            await saver.setup()
            yield saver
        return
    if database.backend == "postgres":
        saver_cls, pool_cls, dict_row = _postgres_components()
        pool = pool_cls(
            database.checkpointer_dsn,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "keepalives": 1,
                "keepalives_idle": 60,
            },
            check=pool_cls.check_connection,
        )
        async with pool:
            await _create_schema_if_needed(pool, database.postgres_schema)
            saver = saver_cls(conn=pool)
            await saver.setup()
            yield saver
        return
    raise ValueError(
        f"Unknown database.backend {database.backend!r}. Expected memory, sqlite, or postgres."
    )


async def delete_thread_checkpoints(saver: BaseCheckpointSaver, thread_id: str) -> None:
    """Drop every checkpoint_ns for this thread, including subgraphs."""
    await saver.adelete_thread(thread_id)


def _prepare_sqlite_path(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


async def _apply_sqlite_pragmas(conn) -> None:
    for statement in _SQLITE_PRAGMAS:
        cursor = await conn.execute(statement)
        await cursor.fetchall()


async def _create_schema_if_needed(pool, schema: str) -> None:
    if not schema:
        return
    async with pool.connection() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        logger.info("checkpointer schema %s is present", schema)
