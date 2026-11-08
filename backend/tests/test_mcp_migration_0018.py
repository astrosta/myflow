"""0018_mcp_servers: create the owner-private MCP server table.

The chain is re-entrant like every other DDL revision here: an empty database is
built by `Base.metadata.create_all` and stamped at head, so the table may already
exist when `upgrade()` runs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from agentkit.persistence.bootstrap import _local_revisions
from agentkit.persistence.migrations import __file__ as migrations_file
from agentkit.persistence.schema import Base

MIGRATIONS_DIR = Path(migrations_file).resolve().parent
REVISION_FILE = MIGRATIONS_DIR / "versions" / "0018_mcp_servers.py"
TABLE = "mcp_servers"


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _revision_module():
    spec = importlib.util.spec_from_file_location("migration_0018_mcp_servers", REVISION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0018_follows_0017() -> None:
    """0018 is a chain member, not the head: later revisions build on it."""
    module = _revision_module()

    assert module.revision == "0018_mcp_servers"
    assert module.down_revision == "0017_contextforge_tools"
    # The head moves with every new DDL revision; 0018 only has to be in the chain.
    assert "0018_mcp_servers" in _local_revisions()


def test_0018_creates_and_drops_the_table(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    command.upgrade(_config(db_path), "0018_mcp_servers")
    with engine.connect() as conn:
        assert TABLE in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0018_mcp_servers"
        )

    command.downgrade(_config(db_path), "0017_contextforge_tools")
    with engine.connect() as conn:
        assert TABLE not in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0017_contextforge_tools"
        )
    engine.dispose()


def test_0018_is_reentrant_on_a_created_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0017_contextforge_tools')")
        )

    # create_all already built the table; the upgrade must not blow up on it.
    command.upgrade(_config(db_path), "0018_mcp_servers")

    with engine.connect() as conn:
        assert TABLE in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0018_mcp_servers"
        )
    engine.dispose()


def test_0018_upgrade_and_downgrade_can_run_twice(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    module = _revision_module()

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()
            module.upgrade()

    with engine.connect() as conn:
        assert TABLE in set(inspect(conn).get_table_names())

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.downgrade()
            module.downgrade()

    with engine.connect() as conn:
        assert TABLE not in set(inspect(conn).get_table_names())
    engine.dispose()


def test_0018_builds_the_table_the_orm_expects(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    command.upgrade(_config(db_path), "0018_mcp_servers")

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    indexes = {index["name"] for index in inspector.get_indexes(TABLE)}
    assert set(Base.metadata.tables[TABLE].columns.keys()) <= columns
    assert "ix_mcp_servers_owner_enabled_status" in indexes
    engine.dispose()
