"""Migration 0009 removes the legacy canonical memory tables."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from agentkit.persistence.bootstrap import _head_revision
from agentkit.persistence.migrations import __file__ as migrations_file
from agentkit.persistence.schema import Base

MIGRATIONS_DIR = Path(migrations_file).resolve().parent
LEGACY_CANONICAL_MEMORY_TABLES = frozenset(
    {
        "memory_profiles",
        "memory_records",
        "memory_record_versions",
        "memory_episode_sessions",
        "memory_ingest_jobs",
        "memory_consolidation_jobs",
        "memory_skill_candidates",
        "memory_conflicts",
        "memory_mutation_ledger",
        "memory_index_outbox",
        "memory_scope_meta",
        "memory_projection_state",
    }
)


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_0009_drops_legacy_tables_and_keeps_openviking_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        for name in LEGACY_CANONICAL_MEMORY_TABLES:
            conn.execute(text(f"CREATE TABLE {name} (id INTEGER)"))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0008_openviking_memory')")
        )

    command.upgrade(_config(db_path), "head")

    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        version = conn.scalar(text("SELECT version_num FROM alembic_version"))

    assert version == _head_revision() == "0009_drop_canonical_memory"
    assert LEGACY_CANONICAL_MEMORY_TABLES.isdisjoint(names)
    assert {
        "memory_profile_documents",
        "memory_sync_state",
        "memory_capture_dedup",
    } <= names
    engine.dispose()
