"""Deterministic test doubles for the stateless operator boundary."""

from __future__ import annotations

from typing import Any, Sequence

from agentkit.agents.memory.canonical.proposals import (
    DehydratedEpisode,
    EpisodeProposal,
    ExistingSemantic,
    ProfileProposal,
    SemanticExtraction,
    SkillProposal,
    ToolSummary,
    TurnDigest,
)


class FakeOperators:
    """No model, no store, no side effects; records only method names."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.semantic_results: list[SemanticExtraction] = []
        self.profile_result = ProfileProposal()
        self.episode_result: EpisodeProposal | None = None
        self.skill_result = SkillProposal()

    async def extract_semantic(
        self, messages: Sequence[Any], existing: Sequence[ExistingSemantic]
    ) -> list[SemanticExtraction]:
        del messages, existing
        self.calls.append("extract_semantic")
        return list(self.semantic_results)

    async def extract_profile(self, current_message: str, profile: dict[str, Any]) -> ProfileProposal:
        del current_message, profile
        self.calls.append("extract_profile")
        return self.profile_result

    async def dehydrate(
        self, turns: Sequence[TurnDigest], tool_summaries: Sequence[ToolSummary]
    ) -> EpisodeProposal:
        del turns, tool_summaries
        self.calls.append("dehydrate")
        if self.episode_result is None:
            return EpisodeProposal(
                episode=DehydratedEpisode(
                    task_intent="test task",
                    final_outcome="SUCCESS",
                    evidence_refs=["message:test"],
                )
            )
        return self.episode_result

    async def propose_skill(
        self, current_skill_md: str, episodes: Sequence[DehydratedEpisode]
    ) -> SkillProposal:
        del current_skill_md, episodes
        self.calls.append("propose_skill")
        return self.skill_result


class FakeEmbedder:
    """Deterministic vectors for retrieval/projection tests."""

    def __init__(self, dimensions: int = 8) -> None:
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        seed = sum((index + 1) * ord(char) for index, char in enumerate(text))
        vector = [float((seed + index) % 17) for index in range(self.dimensions)]
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)
