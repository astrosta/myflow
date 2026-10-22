"""M10 section 2.2.3: generic MemoryBackend contract."""

from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from agentkit.agents.memory.backends.langmem.backend import LangMemBackend
from agentkit.agents.memory.types import (
    MemoryConflictError,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
)


@pytest.fixture
def backend():
    return LangMemBackend(InMemoryStore())


def _record(user_id: str = "alice", record_id: str = "r1", **overrides) -> MemoryRecord:
    values = dict(
        id=record_id,
        user_id=user_id,
        kind="semantic",
        subtype="fact",
        content="likes pytest",
        search_text="likes pytest",
        source="inferred",
    )
    values.update(overrides)
    return MemoryRecord(**values)


def test_namespace_isolation_and_stats(backend) -> None:
    alice = MemoryScope("alice")
    bob = MemoryScope("bob")
    backend.put_record(alice, _record())
    assert backend.get_record(bob, "semantic", "r1") is None
    assert backend.get_record(alice, "semantic", "r1") is not None
    stats = backend.get_stats(alice)
    assert stats.counts_by_kind["semantic"] == 1
    assert backend.get_stats(bob).counts_by_kind == {}


def test_revision_is_cas_checked(backend) -> None:
    scope = MemoryScope("alice")
    created = backend.put_record(scope, _record())
    assert created.revision == 0
    updated = backend.put_record(scope, _record(content="likes pytest"), expected_revision=0)
    assert updated.revision == 1
    with pytest.raises(MemoryConflictError):
        backend.put_record(scope, _record(), expected_revision=0)


def test_soft_delete_removes_from_search(backend) -> None:
    scope = MemoryScope("alice")
    backend.put_record(scope, _record())
    assert backend.search_records(scope, MemoryQuery(kinds=["semantic"])).items
    assert backend.delete_records(scope, ["r1"], mode="soft", reason="user_forget") == 1
    assert backend.search_records(scope, MemoryQuery(kinds=["semantic"])).items == []
    historical = backend.search_records(
        scope, MemoryQuery(kinds=["semantic"], include_history=True)
    )
    assert historical.items[0].record.status == "deleted"
    assert backend.delete_records(scope, ["r1"], mode="hard", reason="purge") == 1
    assert backend.get_record(scope, "semantic", "r1") is None


def test_profile_namespace_and_scope_listing(backend) -> None:
    scope = MemoryScope("alice")
    profile = MemoryRecord(
        id="profile", user_id="alice", kind=MemoryKind.PROFILE, content="", source="user_manual"
    )
    backend.put_record(scope, profile)
    assert backend.get_record(scope, MemoryKind.PROFILE, "profile") is not None
    assert [item.user_id for item in backend.list_scopes().items] == ["alice"]
    backend.delete_profile(scope)
    assert backend.get_record(scope, MemoryKind.PROFILE, "profile") is None
