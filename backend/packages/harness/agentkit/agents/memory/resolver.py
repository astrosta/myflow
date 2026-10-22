"""Per-kind conflict resolution (M10 section 2.7).

Strategies decide accept / reject / conflict / supersede. They never write.
The resolver dispatches by kind, so a new type registers a strategy instead of
editing this class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from agentkit.agents.memory.types import (
    MemoryKind,
    MemoryProposal,
    MemoryRecord,
    MemoryScope,
    Resolution,
    source_rank,
)


class ConflictStrategy(ABC):
    kind: str

    @abstractmethod
    def resolve(
        self,
        scope: MemoryScope,
        current: MemoryRecord | None,
        proposal: MemoryProposal,
    ) -> Resolution:
        raise NotImplementedError


class ProfileConflictStrategy(ConflictStrategy):
    kind = MemoryKind.PROFILE

    def resolve(self, scope, current, proposal):
        del scope
        if current is None:
            return Resolution(action="accept")
        candidate_source = str(proposal.payload.get("source") or current.source)
        current_source = current.source
        candidate_value = proposal.payload.get("value")
        current_value = current.structured.get("value")
        if candidate_value is not None and _normalize(str(candidate_value)) == _normalize(str(current_value)):
            return Resolution(action="accept", reason="same value; refresh confirmation")
        if candidate_source == "user_manual":
            return Resolution(action="supersede", reason="manual UI edit")
        if current_source == "user_manual":
            if candidate_source == "user_explicit_durable":
                return Resolution(action="supersede", reason="new explicit instruction overrides manual")
            return Resolution(action="reject", reason="manual profile fields are pinned")
        if source_rank(candidate_source) < source_rank(current_source):
            return Resolution(action="reject", reason="lower authority than the current profile value")
        if source_rank(candidate_source) == source_rank(current_source):
            return Resolution(action="conflict", reason="equal authority, different value")
        return Resolution(action="supersede", reason="higher authority")


class SemanticConflictStrategy(ConflictStrategy):
    kind = MemoryKind.SEMANTIC

    def resolve(self, scope, current, proposal):
        del scope
        if current is None:
            return Resolution(action="accept")
        candidate_source = str(proposal.payload.get("source") or "inferred")
        current_source = current.source
        candidate_content = _normalize(str(proposal.payload.get("content") or ""))
        current_content = _normalize(current.content)
        if candidate_content and candidate_content == current_content:
            return Resolution(action="accept", reason="same fact; refresh instead of duplicating")
        if source_rank(candidate_source) > source_rank(current_source):
            return Resolution(action="supersede", record=current, reason="higher authority")
        if source_rank(candidate_source) == source_rank(current_source):
            return Resolution(action="conflict", record=current, reason="equal authority, different content")
        return Resolution(action="reject", record=current, reason="lower authority than the active fact")


class AppendOnlyConflictStrategy(ConflictStrategy):
    kind = MemoryKind.EPISODIC

    def resolve(self, scope, current, proposal):
        del scope, current, proposal
        return Resolution(action="accept")


class VersionedConflictStrategy(ConflictStrategy):
    kind = MemoryKind.PROCEDURAL

    def resolve(self, scope, current, proposal):
        del scope
        if current is None:
            return Resolution(action="accept")
        candidate_version = int(proposal.payload.get("version") or 0)
        current_version = int(current.structured.get("version") or 0)
        if candidate_version > current_version:
            return Resolution(action="supersede", record=current, reason="new immutable version")
        return Resolution(action="reject", record=current, reason="procedures are versioned, not overwritten")


class MemoryResolver:
    def __init__(self, strategies: list[ConflictStrategy] | None = None) -> None:
        self._strategies: dict[str, ConflictStrategy] = {}
        for strategy in strategies or [
            ProfileConflictStrategy(),
            SemanticConflictStrategy(),
            AppendOnlyConflictStrategy(),
            VersionedConflictStrategy(),
        ]:
            self.register(strategy)

    def register(self, strategy: ConflictStrategy) -> None:
        self._strategies[strategy.kind] = strategy

    def strategy(self, kind: str) -> ConflictStrategy:
        try:
            return self._strategies[kind]
        except KeyError as exc:
            raise ValueError(f"no conflict strategy registered for kind {kind!r}") from exc

    def resolve(
        self,
        kind: str,
        scope: MemoryScope,
        current: MemoryRecord | None,
        proposal: MemoryProposal,
    ) -> Resolution:
        return self.strategy(kind).resolve(scope, current, proposal)


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())
