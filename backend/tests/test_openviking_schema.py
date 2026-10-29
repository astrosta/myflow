"""Schema registration for the five OpenViking memory tables."""

from __future__ import annotations

import agentkit.agents.memory.openviking.models  # noqa: F401
from agentkit.persistence.base import Base


def test_openviking_tables_are_registered() -> None:
    names = set(Base.metadata.tables)
    assert {
        "memory_profile_documents",
        "memory_sync_state",
        "memory_profile_onboarding",
        "memory_profile_suggestions",
        "memory_capture_dedup",
    } <= names


def test_dedup_state_check_exists() -> None:
    table = Base.metadata.tables["memory_capture_dedup"]
    checks = [str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext")]
    assert any("state" in sql for sql in checks)
    assert any("appended_at" in sql for sql in checks)


def test_suggestion_pending_unique_index() -> None:
    table = Base.metadata.tables["memory_profile_suggestions"]
    index_names = {index.name for index in table.indexes}
    assert "uq_pending_suggestion" in index_names
