"""0020_drop_old_contextforge_tools: drop the retired gateway catalog table.

The cleanup revision removes the table 0017 created.  Nothing is migrated: the old
rows belong to the retired external gateway, so the new catalog is rebuilt from each
MCP server's `tools/list`.  `upgrade()` must also be safe on a database that never
carried the table, and `downgrade()` recreates nothing.
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
REVISION_FILE = MIGRATIONS_DIR / "versions" / "0020_drop_old_contextforge_tools.py"
RETIRED_TABLE = "contextforge_tools"
NEW_TABLE = "mcp_tools"


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _revision_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0020_drop_old_contextforge_tools", REVISION_FILE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0020_follows_0019() -> None:
    module = _revision_module()

    assert module.revision == "0020_drop_old_contextforge_tools"
    assert module.down_revision == "0019_mcp_tools"
    assert "0020_drop_old_contextforge_tools" in _local_revisions()


def test_0020_drops_the_retired_table_and_keeps_the_new_one(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    command.upgrade(_config(db_path), "0019_mcp_tools")
    with engine.connect() as conn:
        assert RETIRED_TABLE in set(inspect(conn).get_table_names())

    command.upgrade(_config(db_path), "0020_drop_old_contextforge_tools")
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert RETIRED_TABLE not in names
        assert NEW_TABLE in names
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0020_drop_old_contextforge_tools"
        )
    engine.dispose()


def test_0020_downgrade_recreates_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    command.upgrade(_config(db_path), "0020_drop_old_contextforge_tools")
    command.downgrade(_config(db_path), "0019_mcp_tools")

    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        # 0020 only ever drops: stepping back to 0019 leaves the retired table
        # gone. Only downgrading past 0017 would recreate it, which is why the
        # old catalog is never resurrected by this revision.
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0019_mcp_tools"
        )
        assert RETIRED_TABLE not in names
        assert NEW_TABLE in names
    engine.dispose()


def test_0020_upgrade_is_reentrant_when_the_table_is_absent(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    module = _revision_module()

    with engine.begin() as conn:
        Base.metadata.create_all(conn)

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()
            module.upgrade()
            module.downgrade()
            module.downgrade()

    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert RETIRED_TABLE not in names
        assert NEW_TABLE in names
    engine.dispose()
