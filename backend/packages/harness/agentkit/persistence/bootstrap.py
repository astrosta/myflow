"""Empty-database create_all+stamp versus upgrade of a revision this build knows."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_OWNED_TABLES = frozenset({"threads_meta", "runs", "run_events"})
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class SchemaStateError(RuntimeError):
    """库的 alembic 状态不是本代码能理解的。fail-closed：拒绝启动。"""


def _sync_sqlalchemy_url(engine: AsyncEngine) -> str:
    """Alembic's command API is synchronous and cannot use the async driver."""
    url = engine.url
    if url.drivername == "sqlite+aiosqlite":
        url = url.set(drivername="sqlite")
    elif url.drivername == "postgresql+asyncpg":
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def _alembic_config(engine: AsyncEngine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    # ConfigParser treats "%" as interpolation. Windows paths encode ":" as "%3A".
    url = _sync_sqlalchemy_url(engine).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg)


def _local_revisions() -> frozenset[str]:
    """Revisions compiled into this process, not whatever the database claims."""
    return frozenset(rev.revision for rev in _script_directory().walk_revisions())


def _head_revision() -> str:
    head = _script_directory().get_current_head()
    if head is None:
        raise SchemaStateError(
            "this build has no alembic head revision; "
            "refusing to start (see docs/plans/2026-09-16-m4-persistence-design.md §3.2)"
        )
    return head


async def _is_empty_database(engine: AsyncEngine) -> bool:
    """True when none of our tables exist. ``alembic_version`` does not count."""

    def _names(sync_conn) -> set[str]:
        return set(sa_inspect(sync_conn).get_table_names())

    async with engine.connect() as conn:
        names = await conn.run_sync(_names)
    return names.isdisjoint(_OWNED_TABLES)


async def _installed_revision(engine: AsyncEngine) -> str | None:
    """The single alembic_version row, or None when the table is missing, empty, or multi-row."""

    def _read(sync_conn) -> str | None:
        if not sa_inspect(sync_conn).has_table("alembic_version"):
            return None
        rows = sync_conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        if len(rows) != 1 or rows[0][0] is None:
            return None
        return str(rows[0][0])

    async with engine.connect() as conn:
        return await conn.run_sync(_read)


async def _create_all(engine: AsyncEngine) -> None:
    import agentkit.persistence.models  # noqa: F401
    from agentkit.persistence.base import Base

    def _create(sync_conn) -> None:
        Base.metadata.create_all(sync_conn)

    async with engine.begin() as conn:
        await conn.run_sync(_create)


async def _stamp_head(engine: AsyncEngine) -> None:
    cfg = _alembic_config(engine)
    await asyncio.to_thread(command.stamp, cfg, "head")


async def _upgrade_head(engine: AsyncEngine) -> None:
    cfg = _alembic_config(engine)
    await asyncio.to_thread(command.upgrade, cfg, "head")


@asynccontextmanager
async def _bootstrap_lock(engine: AsyncEngine, *, backend: str) -> AsyncIterator[None]:
    """Hold the lock across reflect-and-act, not only across create_all."""
    if backend == "postgres":
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('agentkit_bootstrap'))")
            )
            yield
        return

    from agentkit.persistence.engine import _sqlite_lock

    async with _sqlite_lock:
        yield


async def bootstrap_schema(engine: AsyncEngine, *, backend: str) -> None:
    """空库 → create_all + stamp head；已有库 → upgrade head；其余 → 抛 SchemaStateError。"""
    async with _bootstrap_lock(engine, backend=backend):
        if await _is_empty_database(engine):
            await _create_all(engine)
            await _stamp_head(engine)
            return
        revision = await _installed_revision(engine)
        if revision is None or revision not in _local_revisions():
            raise SchemaStateError(
                f"database is at revision {revision!r}, which is not in this build's alembic chain; "
                "refusing to start (see docs/plans/2026-09-16-m4-persistence-design.md §3.2)"
            )
        if revision != _head_revision():
            await _upgrade_head(engine)
