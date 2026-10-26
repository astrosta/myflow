"""L1 gate, episode dehydration, and correction.

C is invoked only after a session closes and the host gate passes.  The
service stores a dehydrated summary plus evidence references; it never writes
the session transcript to the memory tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from agentkit.agents.memory.canonical.domain import (
    MemoryNotFoundError,
    MemoryRecordView,
    MutationContext,
    RecordDraft,
)
from agentkit.agents.memory.canonical.operators import OperatorUnavailable
from agentkit.agents.memory.canonical.proposals import (
    DehydratedEpisode,
    EpisodeProposal,
    ToolSummary,
    TurnDigest,
)
from agentkit.agents.memory.canonical.repository import MemoryRepository


@dataclass(frozen=True, slots=True)
class GateSignals:
    tool_error: bool = False
    retry: bool = False
    user_correction: bool = False
    multi_tool: bool = False
    long_task: bool = False
    terminal_success: bool = False
    terminal_failure: bool = False
    greeting: bool = False


@dataclass(frozen=True, slots=True)
class GateDecision:
    score: float
    passed: bool
    reasons: tuple[str, ...] = ()


class L1Gate:
    def __init__(self, threshold: float = 0.55) -> None:
        self._threshold = float(threshold)

    def evaluate(self, signals: GateSignals) -> GateDecision:
        score = 0.0
        reasons: list[str] = []
        for name, weight in (
            ("tool_error", 0.30),
            ("retry", 0.30),
            ("user_correction", 0.30),
            ("multi_tool", 0.20),
            ("long_task", 0.10),
            ("terminal_success", 0.10),
            ("terminal_failure", 0.10),
        ):
            if getattr(signals, name):
                score += weight
                reasons.append(name)
        if signals.greeting and not reasons:
            score = 0.0
        score = min(1.0, score)
        return GateDecision(score=score, passed=score >= self._threshold, reasons=tuple(reasons))


class EpisodicService:
    def __init__(
        self,
        repo: MemoryRepository,
        operators: Any,
        gate: L1Gate,
        max_summary_tokens: int = 1200,
    ) -> None:
        self._repo = repo
        self._operators = operators
        self._gate = gate
        self._max_summary_tokens = int(max_summary_tokens)

    async def process_ingest_job(
        self, job_id: str, *, signals: GateSignals | None = None
    ) -> MemoryRecordView | None:
        job = await self._repo.get_ingest_job_by_id(job_id)
        if job is None:
            return None
        existing_id = (job.payload or {}).get("episode_id")
        if isinstance(existing_id, str):
            existing = await self._repo.get_record(job.user_id, existing_id)
            if existing is not None:
                return existing

        decision = self._gate.evaluate(signals or GateSignals())
        if not decision.passed:
            await self._repo.finish_ingest_job(
                job.id,
                status="done",
                payload={"skipped": "l1", "gate_score": decision.score},
            )
            return None

        refs = job.payload.get("turn_refs") or []
        turns = [
            TurnDigest(
                intent=str(ref.get("summary") or ""),
                outcome=str(ref.get("tool_status") or "observed"),
                tool_calls=(str(ref["tool_name"]),) if ref.get("tool_name") else (),
                summary=str(ref.get("summary") or ""),
                evidence_refs=(str(ref.get("message_id") or ""),),
            )
            for ref in refs
            if isinstance(ref, dict)
        ]
        tools = [
            ToolSummary(
                name=str(ref.get("tool_name") or "tool"),
                status=str(ref.get("tool_status") or "ok"),
                summary=str(ref.get("summary") or ""),
            )
            for ref in refs
            if isinstance(ref, dict) and ref.get("role") == "tool"
        ]
        try:
            proposal = await self._operators.dehydrate(turns, tools)
        except OperatorUnavailable as exc:
            await self._repo.finish_ingest_job(
                job.id, status="error", error=type(exc).__name__
            )
            return None
        except Exception as exc:  # noqa: BLE001 - provider failures are retryable
            await self._repo.finish_ingest_job(
                job.id, status="error", error=type(exc).__name__
            )
            return None

        episode = proposal.episode if isinstance(proposal, EpisodeProposal) else None
        if episode is None or not episode.evidence_refs:
            await self._repo.finish_ingest_job(
                job.id, status="error", error="invalid_episode_proposal"
            )
            return None

        content = _episode_content(episode)
        if len(content) > self._max_summary_tokens * 4:
            content = content[: self._max_summary_tokens * 4]
        now = _now()
        record = await self._repo.create_record(
            RecordDraft(
                user_id=job.user_id,
                id=_record_id(),
                kind="episodic",
                subtype="episode",
                status="active",
                content=content,
                payload=episode.model_dump(mode="json"),
                topic_key=episode.task_class,
                scope_condition="global",
                source="conversation",
                evidence_tier="BEHAVIORAL_OBSERVED",
                confidence=0.70,
                salience=episode.salience,
                explicit_remember=False,
                parent_memory_id=None,
                correction_of=None,
                valid_from=None,
                valid_to=None,
                occurred_at=now,
                thread_id=job.thread_id,
                agent_name=None,
                provenance={
                    "episode_key": job.episode_key,
                    "session_id": job.episode_session_id,
                    "evidence_refs": list(episode.evidence_refs),
                },
            ),
            mutation=MutationContext("SYSTEM", "episodic-worker", "ADD", None, ()),
        )
        await self._repo.finish_ingest_job(
            job.id, status="done", payload={"episode_id": record.id}
        )
        return record

    async def correct(
        self,
        episode_id: str,
        *,
        content: str,
        outcome: str,
        evidence_refs: Sequence[str],
        expected_revision: int,
    ) -> MemoryRecordView:
        old = await self._repo.get_record_unscoped(episode_id)
        if old is None:
            raise MemoryNotFoundError(f"episode {episode_id} not found")
        now = _now()
        new = await self._repo.create_record(
            RecordDraft(
                user_id=old.user_id,
                id=_record_id(),
                kind="episodic",
                subtype="episode",
                status="active",
                content=content,
                payload={"content": content, "outcome": outcome, "evidence_refs": list(evidence_refs)},
                topic_key=old.topic_key,
                scope_condition=old.scope_condition,
                source="conversation",
                evidence_tier="EXPLICIT_STATEMENT",
                confidence=0.85,
                salience=old.salience,
                explicit_remember=False,
                parent_memory_id=None,
                correction_of=old.id,
                valid_from=None,
                valid_to=None,
                occurred_at=now,
                thread_id=old.thread_id,
                agent_name=old.agent_name,
                provenance={"evidence_refs": list(evidence_refs), "correction_of": old.id},
            ),
            mutation=MutationContext("USER", old.user_id, "ADD", "correction", ()),
        )
        await self._repo.update_record(
            old.user_id,
            old.id,
            expected_revision=expected_revision,
            changes={"status": "corrected"},
            mutation=MutationContext("USER", old.user_id, "UPDATE", "correction", ()),
        )
        return new

    async def list_episodes(
        self, user_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> list[MemoryRecordView]:
        del cursor
        return await self._repo.list_records(user_id, kind="episodic", limit=limit)


def _episode_content(episode: DehydratedEpisode) -> str:
    return "\n".join(
        part
        for part in (
            episode.task_intent.strip(),
            episode.trajectory_digest.strip(),
            episode.key_takeaway.strip(),
        )
        if part
    )


def _record_id() -> str:
    from uuid import uuid4

    return str(uuid4())


def _now() -> datetime:
    from agentkit.agents.memory.types import utc_now

    return utc_now()
