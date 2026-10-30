"""Facade for the additive OpenViking memory slice."""

from __future__ import annotations

from langchain_core.messages import BaseMessage

from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from agentkit.agents.memory.openviking.contracts import (
    CapturedMessage,
    MemoryHit,
    MemoryStatus,
    PersonalizationView,
    ProfileView,
    SessionRef,
    TurnDecision,
)
from agentkit.agents.memory.openviking.policy import classify_turn
from agentkit.agents.memory.openviking.profile import PersonalizationService, ProfileService
from agentkit.agents.memory.openviking.recall import RecallService
from agentkit.agents.memory.openviking.session import SessionManager
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.agents.memory.openviking.stream import InMemoryCaptureStream, MemoryMessagePublisher
from agentkit.agents.memory.openviking.worker import TurnCaptureWorker


class MemoryFacade:
    def __init__(
        self,
        *,
        config: OpenVikingMemoryConfig,
        sync_store: MemorySyncStore,
        dedup: CaptureDedupStore,
        sessions: SessionManager,
        stream: InMemoryCaptureStream,
        publisher: MemoryMessagePublisher,
        worker: TurnCaptureWorker,
        profile: ProfileService,
        personalization: PersonalizationService,
        recall: RecallService | None = None,
    ) -> None:
        self.config = config
        self._sync_store = sync_store
        self._dedup = dedup
        self._sessions = sessions
        self._stream = stream
        self._publisher = publisher
        self._worker = worker
        self._profile = profile
        self._personalization = personalization
        self._recall = recall

    def decide_capture(self, text: str, *, has_attachments: bool = False) -> TurnDecision:
        return classify_turn(
            text,
            has_attachments=has_attachments,
            extra_skip_tokens=self.config.capture.extra_skip_tokens,
            skip_greetings=self.config.capture.skip_greetings,
            skip_emoji_only=self.config.capture.skip_emoji_only,
        )

    async def resolve_capture_context(self, user_id: str, thread_id: str) -> SessionRef:
        return await self._sessions.resolve_capture_context(user_id, thread_id)

    async def publish_human(
        self,
        session: SessionRef,
        turn_id: str,
        message: BaseMessage,
    ) -> CapturedMessage:
        return await self._publisher.publish_human(session, turn_id, message)

    async def publish_ai(
        self,
        session: SessionRef,
        turn_id: str,
        message: BaseMessage,
    ) -> CapturedMessage:
        return await self._publisher.publish_ai(session, turn_id, message)

    async def publish_tool(
        self,
        session: SessionRef,
        turn_id: str,
        message: BaseMessage,
    ) -> CapturedMessage:
        return await self._publisher.publish_tool(session, turn_id, message)

    async def process(self, captured: CapturedMessage) -> str:
        return await self._worker.process(captured)

    async def get_profile(self, user_id: str) -> ProfileView:
        return await self._profile.get_snapshot(user_id)

    async def get_personalization(self, user_id: str) -> PersonalizationView:
        return await self._personalization.render(user_id)

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        memory_types: list[str],
        limit: int = 8,
        since: str | None = None,
        until: str | None = None,
        time_field: str | None = None,
    ) -> list[MemoryHit]:
        if self._recall is None:
            raise RuntimeError("OpenViking recall is not configured")
        return await self._recall.search(
            ov_user=self._sessions.ov_user(user_id),
            query=query,
            memory_types=memory_types,
            limit=limit,
            since=since,
            until=until,
            time_field=time_field,
        )

    async def read(self, user_id: str, uri: str, *, max_chars: int = 8_000) -> str:
        if self._recall is None:
            raise RuntimeError("OpenViking recall is not configured")
        return await self._recall.read(self._sessions.ov_user(user_id), uri, max_chars=max_chars)

    async def overview(self, user_id: str, uri: str) -> str:
        if self._recall is None:
            raise RuntimeError("OpenViking recall is not configured")
        return await self._recall.overview(self._sessions.ov_user(user_id), uri)

    async def status(self) -> MemoryStatus:
        pending = len(getattr(self._stream, "pending", []))
        return MemoryStatus(
            healthy=True,
            degraded=False,
            pending_stream_messages=pending,
        )


__all__ = ["MemoryFacade"]
