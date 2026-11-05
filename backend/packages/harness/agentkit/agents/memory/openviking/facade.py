"""Facade for the additive OpenViking memory slice."""

from __future__ import annotations

import logging

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
from agentkit.agents.memory.openviking.library import (
    MemoryContent,
    MemoryDetail,
    MemoryLibraryService,
    MemoryListPage,
    MemoryUnavailable,
)
from agentkit.agents.memory.openviking.profile import PersonalizationService, ProfileService
from agentkit.agents.memory.openviking.recall import RecallService
from agentkit.agents.memory.openviking.session import SessionManager
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.agents.memory.openviking.stream import CaptureStream, MemoryMessagePublisher
from agentkit.agents.memory.openviking.worker import TurnCaptureWorker

logger = logging.getLogger(__name__)


class MemoryFacade:
    def __init__(
        self,
        *,
        config: OpenVikingMemoryConfig,
        sync_store: MemorySyncStore,
        dedup: CaptureDedupStore,
        sessions: SessionManager,
        stream: CaptureStream,
        publisher: MemoryMessagePublisher,
        worker: TurnCaptureWorker,
        profile: ProfileService,
        personalization: PersonalizationService,
        recall: RecallService | None = None,
        library: MemoryLibraryService | None = None,
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
        self._library = library

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

    async def publish_turn_end(
        self,
        session: SessionRef,
        turn_id: str,
        decision: TurnDecision,
        **metadata: object,
    ) -> CapturedMessage:
        return await self._publisher.publish_turn_end(
            session,
            turn_id,
            signals=decision.signals,
            **metadata,
        )

    async def commit_session(self, session: SessionRef) -> None:
        """Commit one thread session so OpenViking extracts the appended messages."""

        await self._sessions.commit(session)

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

    async def list_memory_items(
        self, user_id: str, memory_type: str, *, limit: int = 50
    ) -> MemoryListPage:
        return await self._library_service().list_items(
            self._sessions.ov_user(user_id), memory_type, limit=limit
        )

    async def memory_detail(self, user_id: str, uri: str) -> MemoryDetail:
        return await self._library_service().detail(self._sessions.ov_user(user_id), uri)

    async def read_memory_content(self, user_id: str, uri: str) -> MemoryContent:
        return await self._library_service().read_content(
            self._sessions.ov_user(user_id), uri
        )

    async def update_memory_content(
        self,
        user_id: str,
        uri: str,
        content: str,
        *,
        expected_sha256: str | None,
    ) -> MemoryContent:
        return await self._library_service().update_content(
            self._sessions.ov_user(user_id),
            uri,
            content,
            expected_sha256=expected_sha256,
        )

    async def delete_memory_item(self, user_id: str, uri: str) -> None:
        await self._library_service().delete_item(
            self._sessions.ov_user(user_id), uri
        )

    def _library_service(self) -> MemoryLibraryService:
        if self._library is None:
            raise MemoryUnavailable("OpenViking memory library is not configured")
        return self._library

    async def count_uncertain(self, *, limit: int = 100) -> int:
        """Return only rows that startup may need to reconcile.

        Normal `appended` rows are terminal and are intentionally excluded;
        this is the bounded startup scan from the design, not a full-table
        reconciliation.
        """

        return len(await self._dedup.uncertain(limit=limit))

    async def open_session_count(self, *, user_id: str | None = None) -> int:
        return len(await self._sync_store.list_active(user_id=user_id, limit=500))

    async def flush(self, *, user_id: str | None = None) -> int:
        """Commit active sessions; failures stay retryable and do not stop the sweep."""

        rows = await self._sync_store.list_active(user_id=user_id)
        flushed = 0
        for row in rows:
            ref = SessionRef(
                user_id=row.user_id,
                thread_id=row.thread_id,
                ov_user=row.ov_user,
                ov_session_id=row.ov_session_id,
                stream_shard=row.stream_shard,
                session_epoch=row.session_epoch,
                session_incarnation=row.session_incarnation,
            )
            try:
                await self._sessions.ensure_session(ref)
                await self._sessions.commit(ref)
            except Exception:
                logger.warning(
                    "OpenViking commit flush failed for %s/%s",
                    row.user_id,
                    row.thread_id,
                    exc_info=True,
                )
                continue
            flushed += 1
        return flushed

    async def status(self) -> MemoryStatus:
        stream = self._stream
        pending = getattr(stream, "pending", [])
        return MemoryStatus(
            healthy=True,
            degraded=False,
            pending_stream_messages=len(pending) if isinstance(pending, list) else 0,
            pending_missing_turn_ends=await self.count_uncertain(),
        )


__all__ = ["MemoryFacade"]
