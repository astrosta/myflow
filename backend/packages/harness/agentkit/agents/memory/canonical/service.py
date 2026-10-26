"""Canonical memory composition root and profile rendering."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MutationContext,
    ProfileSnapshotView,
    SkillCandidateView,
    SkillConfirmResult,
)
from agentkit.agents.memory.canonical.episodic import EpisodicService, L1Gate
from agentkit.agents.memory.canonical.operators import LangMemOperators, OperatorUnavailable
from agentkit.agents.memory.canonical.operator_model import resolve_operator_model
from agentkit.agents.memory.canonical.profile import ProfileService
from agentkit.agents.memory.canonical.projection import MemoryMilvusProjection, MemoryProjector
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.canonical.retrieval import HybridRetriever
from agentkit.agents.memory.canonical.semantic import SemanticService
from agentkit.agents.memory.canonical.sessions import CaptureResult, SessionService
from agentkit.agents.memory.canonical.skill_candidates import SkillCandidateService
from agentkit.agents.memory.context import PROFILE_CLOSE, PROFILE_OPEN
from agentkit.agents.memory.tokens import count_tokens
from agentkit.agents.memory.types import PROFILE_FIELD_PRIORITY
from agentkit.config.memory_config import MemoryConfig
from agentkit.knowledge.config import EmbeddingConfig
from agentkit.knowledge.embedding import OpenAICompatibleEmbedder

logger = logging.getLogger(__name__)

_FIELD_LABELS = {
    "identity.preferred_name": "称呼",
    "identity.pronouns": "代词",
    "identity.locale": "地区",
    "identity.timezone": "时区",
    "communication.language": "语言",
    "communication.tone": "语气",
    "communication.verbosity": "详细程度",
    "communication.format": "格式",
    "work.role": "角色",
    "work.domains": "领域",
    "work.tools": "工具",
    "work.expertise": "专长",
    "accessibility.needs": "无障碍需求",
    "boundaries": "边界",
}
_PROFILE_SOURCES = {"user_manual", "user_explicit_durable", "user_confirmed", "conversation_extracted"}


@dataclass(frozen=True, slots=True)
class RenderedProfile:
    text: str
    token_count: int
    truncated: bool


class MemoryService:
    """Async facade used by middleware, tools, and the HTTP API."""

    canonical = True

    def __init__(
        self,
        repo: MemoryRepository,
        sessions: SessionService,
        profile: ProfileService,
        semantic: SemanticService,
        episodic: EpisodicService,
        skills: SkillCandidateService | None,
        retriever: HybridRetriever,
        projector: MemoryProjector | None,
        config: MemoryConfig,
    ) -> None:
        self.repo = repo
        self.sessions = sessions
        self.profile = profile
        self.semantic = semantic
        self.episodic = episodic
        self.skills = skills
        self.retriever = retriever
        self.projector = projector
        self.config = config

    async def get_profile(self, user_id: str) -> ProfileSnapshotView:
        return await self.profile.get(user_id)

    async def patch_profile(
        self, user_id: str, fields: dict[str, Any], *, expected_revision: int
    ) -> ProfileSnapshotView:
        return await self.profile.patch_manual(
            user_id, fields, expected_revision=expected_revision
        )

    async def clear_profile_field(
        self, user_id: str, field_path: str, *, expected_revision: int | None = None
    ) -> ProfileSnapshotView:
        return await self.profile.clear_field(
            user_id, field_path, expected_revision=expected_revision
        )

    async def render_profile(self, user_id: str) -> RenderedProfile:
        snapshot = await self.get_profile(user_id)
        return render_profile_view(snapshot, self.config.profile.max_tokens)

    async def capture_turn(
        self,
        user_id: str,
        thread_id: str,
        run_id: str | None,
        messages: list[Any],
        *,
        terminal: bool,
    ) -> CaptureResult:
        return await self.sessions.capture_turn(
            user_id, thread_id, run_id, messages, terminal=terminal
        )

    async def close_session(
        self, user_id: str, thread_id: str, reason: str
    ) -> Any:
        return await self.sessions.close_session(user_id, thread_id, reason)

    async def search(
        self, user_id: str, *, text: str, kinds: list[str] | None = None, limit: int = 8, cursor: str | None = None
    ) -> Any:
        return await self.retriever.search(
            user_id,
            text=text,
            kinds=kinds or ["semantic"],
            limit=limit,
            cursor=cursor,
        )

    async def list_pending(self, user_id: str, *, limit: int = 50, cursor: str | None = None) -> Any:
        return await self.semantic.list_pending(user_id, limit=limit, cursor=cursor)

    async def promote(self, user_id: str, record_id: str, *, expected_revision: int) -> Any:
        return await self.semantic.promote(
            user_id, record_id, expected_revision=expected_revision
        )

    async def delete_item(self, user_id: str, kind: str, record_id: str) -> None:
        if kind not in {"semantic", "episodic"}:
            raise ValueError("only semantic/episodic items can be deleted")
        await self.repo.soft_delete(
            user_id,
            record_id,
            reason="user_forget",
            mutation=MutationContext("USER", user_id, "DELETE", "user_forget", ()),
        )

    async def get_episodes(self, user_id: str, *, limit: int = 50) -> list[Any]:
        return await self.episodic.list_episodes(user_id, limit=limit)

    async def correct_episode(
        self,
        user_id: str,
        episode_id: str,
        *,
        content: str,
        outcome: str,
        evidence_refs: list[str],
        expected_revision: int,
    ) -> Any:
        current = await self.repo.get_record(user_id, episode_id)
        if current is None:
            raise ValueError("episode not found")
        return await self.episodic.correct(
            episode_id,
            content=content,
            outcome=outcome,
            evidence_refs=evidence_refs,
            expected_revision=expected_revision,
        )

    async def list_skill_candidates(
        self, user_id: str, *, status: str | None = None
    ) -> list[SkillCandidateView]:
        if self.skills is None:
            return []
        return await self.skills.list(user_id, status=status)

    async def confirm_skill_candidate(
        self,
        user_id: str,
        candidate_id: str,
        *,
        expected_revision: int,
        package_sha256: str,
        idempotency_key: str,
    ) -> SkillConfirmResult:
        if self.skills is None:
            raise ValueError("skill candidates are unavailable")
        return await self.skills.confirm(
            user_id,
            candidate_id,
            expected_revision=expected_revision,
            package_sha256=package_sha256,
            idempotency_key=idempotency_key,
        )

    async def reject_skill_candidate(
        self, user_id: str, candidate_id: str, *, expected_revision: int, reason: str
    ) -> SkillCandidateView:
        if self.skills is None:
            raise ValueError("skill candidates are unavailable")
        return await self.skills.reject(
            user_id, candidate_id, expected_revision=expected_revision, reason=reason
        )

    async def reindex(self, user_id: str | None = None) -> int:
        if self.projector is None:
            return 0
        return await self.projector.reindex(user_id)

    async def close(self) -> None:
        if self.projector is not None:
            await self.projector.stop()
        if hasattr(self, "retriever") and getattr(self.retriever, "_projection", None) is not None:
            close = getattr(self.retriever._projection, "close", None)
            if callable(close):
                await close()


def render_profile_view(snapshot: ProfileSnapshotView, max_tokens: int = 800) -> RenderedProfile:
    lines: list[tuple[str, str]] = []
    for path in PROFILE_FIELD_PRIORITY:
        field = snapshot.fields.get(path)
        if field is None or field.source not in _PROFILE_SOURCES:
            continue
        rendered = _render_value(field.value)
        if rendered:
            lines.append((path, f"{_FIELD_LABELS[path]}: {rendered}"))
    kept: list[str] = []
    truncated = False
    for path, line in lines:
        candidate = chr(10).join([PROFILE_OPEN, *kept, line, PROFILE_CLOSE])
        if count_tokens(candidate, mode="char") > max_tokens:
            truncated = True
            continue
        kept.append(line)
    text = chr(10).join([PROFILE_OPEN, *kept, PROFILE_CLOSE]) if kept else ""
    return RenderedProfile(
        text=text,
        token_count=count_tokens(text, mode="char") if text else 0,
        truncated=truncated,
    )


def _render_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or None
    return " ".join(str(value).split()) or None


@dataclass(frozen=True, slots=True)
class MemoryRuntime:
    service: MemoryService | None
    projector: MemoryProjector | None


def build_memory_runtime(
    config: MemoryConfig,
    session_factory: Any,
    *,
    app_config: Any = None,
    skill_author: Any = None,
) -> MemoryRuntime:
    if not config.enabled or session_factory is None:
        return MemoryRuntime(None, None)
    repo = MemoryRepository(session_factory)
    sessions = SessionService(repo, config)
    operators: Any = None
    try:
        if app_config is not None:
            operators = LangMemOperators(
                resolve_operator_model(app_config),
                timeout_seconds=config.langmem.operator_timeout_seconds,
            )
    except OperatorUnavailable:
        logger.warning("memory operators are disabled: model cannot use structured tools")
    profile = ProfileService(
        repo,
        operators,
        auto_write_threshold=config.profile.auto_write_threshold,
    )
    semantic = SemanticService(repo, operators, config)
    episodic = EpisodicService(
        repo,
        operators,
        L1Gate(config.episodic.gate_threshold),
        config.episodic.max_summary_tokens,
    )
    embedder = _make_embedder(config)
    projection = MemoryMilvusProjection(
        config.vector_store, dimensions=config.embedding.dimensions
    )
    projector = MemoryProjector(repo, projection, embedder, config)
    retriever = HybridRetriever(repo, projection, embedder, config)
    skills = (
        SkillCandidateService(repo, operators, embedder, skill_author, config)
        if skill_author is not None
        else None
    )
    service = MemoryService(
        repo, sessions, profile, semantic, episodic, skills, retriever, projector, config
    )
    return MemoryRuntime(service, projector)


class _UnavailableEmbedder:
    """Deterministic failure surface: retrieval must fall back to PG lexical."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = int(dimensions)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("memory embedding endpoint is not configured")

    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("memory embedding endpoint is not configured")


def _make_embedder(config: MemoryConfig) -> Any:
    if not config.embedding.base_url:
        return _UnavailableEmbedder(config.embedding.dimensions)
    return OpenAICompatibleEmbedder(
        EmbeddingConfig(
            base_url=config.embedding.base_url,
            api_key=config.embedding.api_key,
            model=config.embedding.model,
            dimensions=config.embedding.dimensions,
            batch_size=config.embedding.batch_size,
            timeout_seconds=config.embedding.timeout_seconds,
            max_retries=config.embedding.max_retries,
        )
    )


_service: MemoryService | None = None


def configure_memory_service(service: MemoryService | None) -> None:
    global _service
    _service = service


def reset_memory_service() -> None:
    global _service
    _service = None


def get_memory_service() -> MemoryService | None:
    return _service
