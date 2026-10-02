"""Alembic runtime. The URL is injected by bootstrap; this file does not pick a database."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, event, pool

import agentkit.persistence.models  # noqa: F401
from agentkit.persistence.base import Base
from agentkit.persistence.migrations._env_filters import include_object

config = context.config
target_metadata = Base.metadata


def _enable_sqlite_pragmas(dbapi_conn, _record) -> None:
    """Alembic opens its own sqlite connection, so it must repeat the engine pragmas."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=30000;")
    cursor.close()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        render_as_batch=str(url).startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    connectable = create_engine(url, poolclass=pool.NullPool)
    if connectable.dialect.name == "sqlite":
        event.listen(connectable, "connect", _enable_sqlite_pragmas)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
