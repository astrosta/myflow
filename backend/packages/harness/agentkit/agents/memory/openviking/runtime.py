"""Composition root for the OpenViking memory adapter.

This module is deliberately independent of the FastAPI app. The gateway builds
one runtime during lifespan startup and installs it as a process singleton when
memory.openviking.enabled is true.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from agentkit.agents.memory.openviking.adapter import MemoryMessageAdapter
from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from agentkit.agents.memory.openviking.facade import MemoryFacade
from agentkit.agents.memory.openviking.library import MemoryLibraryService
from agentkit.agents.memory.openviking.onboarding import OnboardingService
from agentkit.agents.memory.openviking.profile import PersonalizationService, ProfileService
from agentkit.agents.memory.openviking.recall import RecallService
from agentkit.agents.memory.openviking.session import SessionManager
from agentkit.agents.memory.openviking.store import CaptureDedupStore, MemorySyncStore
from agentkit.agents.memory.openviking.suggestions import (
    ProfileSuggester,
    ProfileSuggestionStore,
)
from agentkit.agents.memory.openviking.stream import (
    InMemoryCaptureStream,
    MemoryMessagePublisher,
    RedisCaptureStream,
)
from agentkit.agents.memory.openviking.tools import make_openviking_memory_tools
from agentkit.agents.memory.openviking.worker import TurnCaptureWorker, WorkerSupervisor
from agentkit.skills.session_import import SessionSkillImporter

logger = logging.getLogger(__name__)


class _EmptyFacade:
    async def search(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class ProfileServices:
    """Profile/onboarding surface that does not require the OpenViking runtime."""

    def __init__(self, profile: ProfileService, onboarding: OnboardingService, suggestions: ProfileSuggester):
        self.profile = profile
        self.onboarding = onboarding
        self.suggestions = suggestions


def build_profile_services(
    config: OpenVikingMemoryConfig,
    session_factory: Any,
    *,
    seed_path: str | None = None,
) -> ProfileServices:
    profile = ProfileService(
        session_factory,
        seed_path or config.personalization.profile_seed_file,
    )
    return ProfileServices(
        profile=profile,
        onboarding=OnboardingService(session_factory, profile),
        suggestions=ProfileSuggester(
            ProfileSuggestionStore(session_factory),
            profile,
            _EmptyFacade(),
            suggestion_days=config.retention.suggestion_days,
        ),
    )


class OpenVikingMemoryRuntime:
    def __init__(
        self,
        *,
        config: OpenVikingMemoryConfig,
        facade: MemoryFacade,
        profile: ProfileService,
        worker: TurnCaptureWorker,
        supervisor: WorkerSupervisor,
        stream: Any,
        client_factory: Any,
        onboarding: OnboardingService,
        suggestions: ProfileSuggester,
        redis: Any = None,
        library: MemoryLibraryService | None = None,
    ) -> None:
        self.config = config
        self.facade = facade
        self.profile = profile
        self.onboarding = onboarding
        self.suggestions = suggestions
        self.worker = worker
        self.supervisor = supervisor
        self.stream = stream
        self.client_factory = client_factory
        self.redis = redis
        self.library = library
        self.tools = make_openviking_memory_tools(facade)

    @classmethod
    def build(
        cls,
        config: OpenVikingMemoryConfig,
        session_factory: Any,
        client_factory: Any,
        *,
        redis: Any = None,
        seed_path: str | None = None,
        skill_author: Any = None,
    ) -> "OpenVikingMemoryRuntime":
        if not config.enabled:
            raise RuntimeError("memory.openviking.enabled must be true to build the runtime")
        if session_factory is None:
            raise RuntimeError("OpenViking memory requires a database session factory")
        if len(config.user_id_salt) < 32:
            raise RuntimeError(
                "memory.openviking.user_id_salt must be at least 32 characters; "
                "it is the only stable user-id pseudonymization key"
            )
        sync_store = MemorySyncStore(session_factory)
        dedup = CaptureDedupStore(session_factory)
        sessions = SessionManager(sync_store, config=config, resolver=client_factory)
        if redis is not None:
            stream: Any = RedisCaptureStream(
                redis,
                key_prefix=config.capture_stream.stream_key_prefix,
                group=config.capture_stream.consumer_group_prefix,
                consumer=config.capture_stream.consumer_name_prefix,
                maxlen=config.capture_stream.maxlen,
                claim_idle_ms=config.capture_stream.claim_idle_ms,
            )
        else:
            stream = InMemoryCaptureStream()
        publisher = MemoryMessagePublisher(stream)
        skill_importer = (
            SessionSkillImporter(
                skill_author,
                sessions,
                max_skills_per_task=config.session_skills.max_skills_per_task,
            )
            if skill_author is not None and config.session_skills.enabled
            else None
        )
        worker = TurnCaptureWorker(
            MemoryMessageAdapter(),
            stream,
            dedup,
            sessions,
            sync_store,
            lease_seconds=config.capture.dedup_lease_seconds,
            missing_turn_end_timeout_seconds=(
                config.capture.missing_turn_end_timeout_seconds
            ),
            session_skills=config.session_skills,
            session_skill_importer=skill_importer,
        )
        supervisor = WorkerSupervisor(
            stream,
            worker,
            shards=config.capture_stream.shard_count,
            redis=redis,
            singleton_key=config.capture_stream.worker_singleton_key,
            owner=f"{config.capture_stream.consumer_name_prefix}-{uuid.uuid4().hex}",
            lease_seconds=config.capture_stream.singleton_lease_seconds,
        )
        profile = ProfileService(session_factory, seed_path or config.personalization.profile_seed_file)
        personalization = PersonalizationService(
            profile,
            identity_path=config.personalization.identity_file,
            soul_path=config.personalization.soul_file,
        )
        recall = RecallService(resolver=client_factory)
        library = MemoryLibraryService(client_factory)
        facade = MemoryFacade(
            config=config,
            sync_store=sync_store,
            dedup=dedup,
            sessions=sessions,
            stream=stream,
            publisher=publisher,
            worker=worker,
            profile=profile,
            personalization=personalization,
            recall=recall,
            library=library,
        )
        onboarding = OnboardingService(session_factory, profile)
        suggestions = ProfileSuggester(
            ProfileSuggestionStore(session_factory),
            profile,
            facade,
            suggestion_days=config.retention.suggestion_days,
        )
        return cls(
            config=config,
            facade=facade,
            profile=profile,
            worker=worker,
            supervisor=supervisor,
            stream=stream,
            client_factory=client_factory,
            onboarding=onboarding,
            suggestions=suggestions,
            redis=redis,
            library=library,
        )

    async def start(self) -> None:
        # In-memory streams are for tests/embedded use; there is no external
        # producer to consume, so there is no supervisor to run.
        if self.redis is None:
            return
        uncertain = await self.facade.count_uncertain()
        if uncertain:
            logger.warning(
                "%s OpenViking capture rows need reconciliation; only pending/appending "
                "and appended rows without appended_at were scanned",
                uncertain,
            )
        await self.supervisor.start()

    async def stop(self) -> None:
        await self.supervisor.stop()
        close = getattr(self.client_factory, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


_runtime: OpenVikingMemoryRuntime | None = None


def configure_openviking_memory(runtime: OpenVikingMemoryRuntime | None) -> None:
    global _runtime
    _runtime = runtime


def get_openviking_memory_runtime() -> OpenVikingMemoryRuntime | None:
    return _runtime


__all__ = [
    "OpenVikingMemoryRuntime",
    "configure_openviking_memory",
    "get_openviking_memory_runtime",
]
