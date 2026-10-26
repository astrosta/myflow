"""Task 2: canonical memory tables and 0006 migration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from agentkit.persistence.schema import Base

EXPECTED = {
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
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_all_memory_tables_are_registered() -> None:
    assert EXPECTED <= set(Base.metadata.tables)


def test_only_one_open_session_per_user_thread(engine) -> None:
    table = Base.metadata.tables["memory_episode_sessions"]
    with engine.begin() as connection:
        connection.execute(
            table.insert().values(
                id="s1",
                user_id="u1",
                thread_id="t1",
                status="open",
                turn_refs=[],
                last_activity_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                table.insert().values(
                    id="s2",
                    user_id="u1",
                    thread_id="t1",
                    status="open",
                    turn_refs=[],
                    last_activity_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


def test_outbox_revision_is_unique(engine) -> None:
    table = Base.metadata.tables["memory_index_outbox"]
    values = dict(
        id="e1",
        user_id="u1",
        pk="u1:m1",
        memory_id="m1",
        op="upsert",
        revision=1,
        epoch=0,
        state="pending",
        attempts=0,
        next_attempt_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    with engine.begin() as connection:
        connection.execute(table.insert().values(values))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(table.insert().values({**values, "id": "e2"}))


def test_migration_points_at_previous_head() -> None:
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "packages/harness/agentkit/persistence/migrations/versions/0006_agent_memory.py"
    )
    spec = importlib.util.spec_from_file_location("memory_migration_0006", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0006_agent_memory"
    assert module.down_revision == "0005_skill_catalog"
