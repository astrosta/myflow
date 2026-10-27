"""Episode session capture and deterministic close boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from agentkit.agents.memory.canonical.capture import (
    TurnRef,
    build_turn_refs,
    is_explicit_remember,
)
from agentkit.agents.memory.canonical.domain import MemorySessionView
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.config.memory_config import MemoryConfig


@dataclass(frozen=True, slots=True)
class CaptureResult:
    session_id: str | None
    appended: int
    closed: bool
    job_id: str | None
    episode_id: str | None = None
    semantic_extracted: bool = False


@dataclass(frozen=True, slots=True)
class CloseResult:
    session_id: str
    job_id: str
    closed: bool = True
    already_closed: bool = False


class SessionService:
    def __init__(self, repo: MemoryRepository, config: MemoryConfig) -> None:
        self._repo = repo
        self._config = config

    async def capture_turn(
        self,
        user_id: str,
        thread_id: str,
        run_id: str | None,
        messages: Sequence[Any],
        *,
        terminal: bool,
        explicit_remember: bool = False,
    ) -> CaptureResult:
        refs = build_turn_refs(messages, run_id=run_id)
        if not refs:
            return CaptureResult(None, 0, False, None)

        session = await self._repo.get_open_session(user_id, thread_id)
        if session is None:
            session = await self._repo.open_session(
                user_id,
                thread_id,
                run_id=run_id or "",
                turn_id=refs[0].message_id,
                now=_now(),
            )
        elif _user_turns(session.turn_refs) >= self._config.episodic.max_turns_per_session:
            await self._close(session, "max_turns")
            session = await self._repo.open_session(
                user_id,
                thread_id,
                run_id=run_id or "",
                turn_id=refs[0].message_id,
                now=_now(),
            )

        before = len(session.turn_refs)
        updated = await self._repo.append_session_refs(
            session.id,
            [_ref_dict(ref) for ref in refs],
            now=_now(),
        )
        appended = max(0, len(updated.turn_refs) - before)
        if terminal:
            closed = await self._close(updated, "terminal")
            return CaptureResult(updated.id, appended, True, closed.job_id)
        if explicit_remember:
            closed = await self._close(updated, "explicit_remember")
            return CaptureResult(updated.id, appended, True, closed.job_id)
        return CaptureResult(updated.id, appended, False, None)

    async def close_session(
        self, user_id: str, thread_id: str, reason: str
    ) -> CloseResult | None:
        session = await self._repo.get_open_session(user_id, thread_id)
        if session is not None:
            return await self._close(session, reason)
        latest = await self._repo.get_latest_session(user_id, thread_id)
        if latest is None:
            return None
        if latest.episode_key:
            job = await self._repo.get_ingest_job(latest.episode_key)
            if job is not None:
                return CloseResult(latest.id, job.id, already_closed=True)
        return await self._close(latest, reason)

    async def close_all_open_sessions(self, reason: str = "shutdown") -> int:
        sessions = await self._repo.list_open_sessions()
        for session in sessions:
            await self._close(session, reason)
        return len(sessions)

    async def close_idle_sessions(self, now: datetime) -> int:
        cutoff = now - timedelta(
            minutes=self._config.episodic.session_idle_close_minutes
        )
        sessions = await self._repo.list_open_sessions(before=cutoff)
        for session in sessions:
            await self._close(session, "idle")
        return len(sessions)

    async def close_max_turn_sessions(self) -> int:
        sessions = await self._repo.list_open_sessions()
        closed = 0
        for session in sessions:
            if _user_turns(session.turn_refs) >= self._config.episodic.max_turns_per_session:
                await self._close(session, "max_turns")
                closed += 1
        return closed

    async def count_open(self, user_id: str, thread_id: str) -> int:
        return await self._repo.count_open_sessions(user_id, thread_id)

    async def _close(self, session: MemorySessionView, reason: str) -> CloseResult:
        if session.status != "open" and session.episode_key:
            job = await self._repo.get_ingest_job(session.episode_key)
            if job is not None:
                return CloseResult(session.id, job.id, already_closed=True)
        last_turn = session.turn_refs[-1].get("message_id", "unknown") if session.turn_refs else "empty"
        episode_key = f"{session.id}:{last_turn}:{reason}"
        closed = await self._repo.close_session(
            session.id, reason=reason, episode_key=episode_key, now=_now()
        )
        job = await self._repo.enqueue_ingest_job(
            session.id,
            episode_key=episode_key,
            payload={
                "thread_id": session.thread_id,
                "episode_key": episode_key,
                "turn_refs": closed.turn_refs,
            },
        )
        return CloseResult(session.id, job.id)


def _ref_dict(ref: TurnRef) -> dict[str, Any]:
    return {
        "message_id": ref.message_id,
        "role": ref.role,
        "run_id": ref.run_id,
        "tool_name": ref.tool_name,
        "tool_status": ref.tool_status,
        "summary": ref.summary,
        "text_sha256": ref.text_sha256,
    }


def _user_turns(refs: Sequence[dict[str, Any]]) -> int:
    return sum(1 for ref in refs if ref.get("role") == "user")


def _now() -> datetime:
    from agentkit.agents.memory.types import utc_now

    return utc_now()
