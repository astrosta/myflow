"""0019_mcp_tools: create the derived tool snapshot table.

The chain is re-entrant like every other DDL revision here: an empty database is built
by `Base.metadata.create_all` and stamped at head, so the table may already exist when
`upgrade()` runs.  There is no backfill - MyFlow-owned MCP servers have not shipped, so
no tool row predates this revision - and the retired external-gateway tool table is
dropped by the cleanup revision, not here.
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
REVISION_FILE = MIGRATIONS_DIR / "versions" / "0019_mcp_tools.py"
TABLE = "mcp_tools"
PARENT_TABLE = "mcp_servers"
#: Task 10 drops this one; this revision must leave it alone.
RETIRED_TABLE = "contextforge_tools"


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _revision_module():
    spec = importlib.util.spec_from_file_location("migration_0019_mcp_tools", REVISION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0019_follows_0018() -> None:
    """0019 is a chain member, not the head: the next DDL revision will follow it."""
    module = _revision_module()

    assert module.revision == "0019_mcp_tools"
    assert module.down_revision == "0018_mcp_servers"
    assert "0019_mcp_tools" in _local_revisions()


def test_0019_creates_and_drops_the_table(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    command.upgrade(_config(db_path), "0019_mcp_tools")
    with engine.connect() as conn:
        assert TABLE in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0019_mcp_tools"
        )

    command.downgrade(_config(db_path), "0018_mcp_servers")
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert TABLE not in names
        # The parent server table arrives with 0018 and must survive this downgrade.
        assert PARENT_TABLE in names
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0018_mcp_servers"
        )
    engine.dispose()


def test_0019_leaves_the_retired_tool_table_in_place(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    command.upgrade(_config(db_path), "0019_mcp_tools")

    with engine.connect() as conn:
        # No backfill and no drop: removing the old catalog is the cleanup revision.
        assert RETIRED_TABLE in set(inspect(conn).get_table_names())
        assert conn.scalar(text(f"SELECT COUNT(*) FROM {TABLE}")) == 0
    engine.dispose()


def test_0019_is_reentrant_on_a_created_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0018_mcp_servers')")
        )

    # create_all already built the table; the upgrade must not blow up on it.
    command.upgrade(_config(db_path), "0019_mcp_tools")

    with engine.connect() as conn:
        assert TABLE in set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0019_mcp_tools"
        )
    engine.dispose()


def test_0019_upgrade_and_downgrade_can_run_twice(tmp_path: Path) -> None:
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


def test_0019_builds_the_table_the_orm_expects(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    command.upgrade(_config(db_path), "0019_mcp_tools")

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    indexes = {index["name"] for index in inspector.get_indexes(TABLE)}
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(TABLE)
    }
    assert set(Base.metadata.tables[TABLE].columns.keys()) <= columns
    assert "ix_mcp_tools_owner_callable" in indexes
    assert "ix_mcp_tools_server_callable" in indexes
    assert ("server_id", "name") in unique_columns
    assert ("owner_user_id", "call_name") in unique_columns
    engine.dispose()
