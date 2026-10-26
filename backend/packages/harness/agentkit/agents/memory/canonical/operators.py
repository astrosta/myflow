"""LangMem stateless operator adapter.

LangMem computes proposals only.  This module never imports the repository,
PostgreSQL, or Milvus; the host validates and persists every result.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

from langchain_core.language_models import BaseChatModel

from agentkit.agents.memory.canonical.proposals import (
    ExistingSemantic,
    DehydratedEpisode,
    EpisodeProposal,
    ProfileDocument,
    ProfileProposal,
    SemanticDocument,
    SemanticExtraction,
    SkillProposal,
    ToolSummary,
    TurnDigest,
)


class OperatorUnavailable(RuntimeError):
    """The configured model cannot drive the structured LangMem operators."""


class LangMemOperators:
    def __init__(self, model: BaseChatModel, *, timeout_seconds: float = 60.0) -> None:
        if not callable(getattr(model, "bind_tools", None)) and not callable(
            getattr(model, "with_structured_output", None)
        ):
            raise OperatorUnavailable(
                "memory operator model does not support structured output/tool calls"
            )
        self._model = model
        self._timeout = float(timeout_seconds)

    async def _run_sync(self, fn: Any, payload: Any) -> Any:
        return await asyncio.wait_for(asyncio.to_thread(fn, payload), timeout=self._timeout)

    async def extract_semantic(
        self,
        messages: Sequence[Any],
        existing: Sequence[ExistingSemantic],
    ) -> list[SemanticExtraction]:
        from langmem import create_memory_manager

        manager = create_memory_manager(
            self._model,
            schemas=[SemanticDocument],
            enable_inserts=True,
            enable_updates=True,
            enable_deletes=True,
        )
        payload = {
            "messages": list(messages),
            "existing": [(item.id, item.document) for item in existing],
        }
        result = await self._run_sync(manager.invoke, payload)
        return _semantic_extractions(result)

    async def extract_profile(
        self,
        current_message: str,
        profile: dict[str, Any],
    ) -> ProfileProposal:
        from langmem import create_memory_manager

        try:
            current = ProfileDocument.model_validate(profile)
        except Exception:
            current = ProfileDocument()
        manager = create_memory_manager(
            self._model,
            schemas=[ProfileDocument],
            enable_inserts=False,
            enable_updates=True,
            enable_deletes=False,
        )
        payload = {
            "messages": [{"role": "user", "content": current_message}],
            "existing": [("profile", current)],
        }
        result = await self._run_sync(manager.invoke, payload)
        if not result:
            return ProfileProposal()
        updated = result[0].content
        if not isinstance(updated, ProfileDocument):
            return ProfileProposal()
        fields: dict[str, Any] = {}
        old = current.model_dump(mode="json")
        new = updated.model_dump(mode="json")
        for section in ("identity", "communication", "work", "accessibility"):
            old_section = old.get(section) or {}
            new_section = new.get(section) or {}
            for key in set(old_section) | set(new_section):
                if old_section.get(key) != new_section.get(key):
                    path = f"{section}.{key}"
                    if key in new_section:
                        fields[path] = updated.__getattribute__(section)[key]
        if old.get("boundaries") != new.get("boundaries") and updated.boundaries is not None:
            fields["boundaries"] = updated.boundaries
        return ProfileProposal(fields=fields)

    async def dehydrate(
        self,
        turns: Sequence[TurnDigest],
        tool_summaries: Sequence[ToolSummary],
    ) -> EpisodeProposal:
        from langmem import create_memory_manager

        manager = create_memory_manager(
            self._model,
            schemas=[DehydratedEpisode],
            enable_inserts=True,
            enable_updates=False,
            enable_deletes=False,
        )
        messages = [
            {
                "role": "user",
                "content": f"intent={turn.intent}; outcome={turn.outcome}; summary={turn.summary}",
            }
            for turn in turns
        ]
        if tool_summaries:
            messages.append(
                {
                    "role": "user",
                    "content": "tools="
                    + "; ".join(
                        f"{tool.name}:{tool.status}:{tool.summary}" for tool in tool_summaries
                    ),
                }
            )
        result = await self._run_sync(manager.invoke, {"messages": messages})
        if not result:
            raise OperatorUnavailable("episode dehydrate returned no proposal")
        episode = result[0].content
        if not isinstance(episode, DehydratedEpisode):
            raise OperatorUnavailable("episode dehydrate returned an invalid schema")
        return EpisodeProposal(episode=episode)

    async def propose_skill(
        self,
        current_skill_md: str,
        episodes: Sequence[DehydratedEpisode],
    ) -> SkillProposal:
        from langmem import create_prompt_optimizer

        optimizer = create_prompt_optimizer(self._model, kind="gradient")
        trajectories = [
            {
                "messages": [
                    {"role": "user", "content": episode.task_intent},
                    {"role": "assistant", "content": episode.trajectory_digest},
                ],
                "feedback": episode.feedback_signal,
            }
            for episode in episodes
        ]
        output = await asyncio.wait_for(
            optimizer.ainvoke({"trajectories": trajectories, "prompt": current_skill_md}),
            timeout=self._timeout,
        )
        return SkillProposal(markdown=str(output or ""))


def _semantic_extractions(result: Sequence[Any]) -> list[SemanticExtraction]:
    extractions: list[SemanticExtraction] = []
    for memory in result or []:
        content = getattr(memory, "content", None)
        if type(content).__name__ == "RemoveDoc":
            extractions.append(SemanticExtraction(id=str(memory.id), remove=True))
        elif isinstance(content, SemanticDocument):
            extractions.append(SemanticExtraction(id=str(memory.id), document=content))
    return extractions
