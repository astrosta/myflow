"""Explicit forget writes a tombstone; old extraction cannot resurrect it."""

from __future__ import annotations

from langgraph.store.memory import InMemoryStore

from agentkit.agents.memory.backends.langmem.backend import LangMemBackend
from agentkit.agents.memory.manager import MemoryManager
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.semantic import SemanticMemoryService
from agentkit.agents.memory.types import MemoryProposal, MemoryQuery, MemoryScope
from agentkit.config.memory_config import MemoryConfig


def test_forgotten_fact_cannot_be_extracted_again() -> None:
    backend = LangMemBackend(InMemoryStore())
    resolver = MemoryResolver()
    semantic = SemanticMemoryService(backend, resolver)
    manager = MemoryManager(backend, [semantic], config=MemoryConfig())
    scope = MemoryScope("alice")
    proposal = MemoryProposal(
        id="p1",
        kind="semantic",
        payload={"content": "likes pytest", "topic_key": "python.test", "source": "inferred"},
        evidence="pytest",
    )
    record = semantic.write(scope, [proposal])[0]
    semantic.forget(scope, record.id)
    assert backend.get_record(scope, "semantic", record.id).status == "deleted"
    tombstones = backend.search_records(
        scope,
        MemoryQuery(kinds=["tombstone"], include_history=True),
    )
    assert len(tombstones.items) == 1
    again = semantic.write(scope, [proposal])
    assert again == []
    assert semantic.search(scope, MemoryQuery(text="pytest")).items == []
    assert manager.list_records(scope, kind="semantic", include_history=True).items[0].record.status == "deleted"
