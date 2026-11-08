"""One-query skill retrieval over the active Milvus projection (Task 12).

The catalog path is deliberately small and linear:

    query -> embedding (optional) + BM25 text
          -> one Milvus dense/BM25/hybrid search (RRFRanker server-side)
          -> one PostgreSQL confirmation
          -> read bodies from the commit cache -> sort, budget

Milvus performs both recall channels and the RRF fusion.  The application never
tokenizes for BM25, computes BM25 scores, computes cosine similarity, or merges
rankings.  Every hit that becomes visible has been confirmed against the
caller's active private pointer, so a stale projection row can never leak
content.  PostgreSQL is the authority: when the catalog cannot be read the
request fails closed instead of returning unconfirmed hits.

There is no public catalog in v2.  A user's retrieval can only return skills
whose owner_user_id equals the caller and whose version is currently active.
A retrieval outage returns no unconfirmed/local fallback hits; the tool reports
the warning instead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from agentkit.config.skills_config import SkillSearchConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import SkillScope, VisibleSkill
from agentkit.skills.loader import MIN_USEFUL_BODY_CHARS, clip_body
from agentkit.skills.parser import strip_frontmatter
from agentkit.skills.projection import ProjectionHit, SkillProjectionStore
from agentkit.skills.router import Embedder, Reranker
from agentkit.skills.store.sql import SqlSkillStore
from agentkit.skills.types import Skill
from agentkit.skills.usage import SkillUsageService

logger = logging.getLogger(__name__)

#: Warnings the tool may hand back to the model.
VECTOR_UNAVAILABLE = "vector_unavailable"
KEYWORD_UNAVAILABLE = "keyword_unavailable"
RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
CONTENT_UNAVAILABLE = "content_unavailable"
RERANK_UNAVAILABLE = "rerank_unavailable"


class SkillSearchUnavailable(RuntimeError):
    """The authoritative catalog could not confirm the projection hits."""


@dataclass(frozen=True, slots=True)
class SkillSearchHit:
    """One loaded skill: confirmed metadata plus the body already under budget."""

    skill: Skill
    body: str
    score: float
    scope: str
    version_id: str


@dataclass(frozen=True, slots=True)
class SkillSearchOutcome:
    hits: tuple[SkillSearchHit, ...]
    candidates: int
    warnings: tuple[str, ...]
    #: 终排是否真的由精排决定：工具把它回给模型，用来解释“为什么是它”。
    reranked: bool = False
    #: 真正贡献了候选的召回通道（embedding / keyword），按固定顺序去重。
    channels: tuple[str, ...] = ()


CHANNEL_EMBEDDING = "embedding"
CHANNEL_KEYWORD = "keyword"


def _rerank_text(skill: Skill, body: str, max_chars: int) -> str:
    """送进精排的文档：元数据在头、正文在后，整体不超过 max_document_chars。

    正文才是精排要读的东西，但不能无界：cross-encoder 的成本随长度线性增长。
    """
    limit = max(1, int(max_chars))
    head = f"{skill.name}\n{skill.description}"
    if not body:
        return head[:limit]
    room = limit - len(head) - 2
    if room <= 0:
        return head[:limit]
    return f"{head}\n\n{body[:room]}"


def _scope_value(scope: SkillScope | str) -> str:
    return scope.value if isinstance(scope, SkillScope) else str(scope)


def _allows(
    skill: Skill,
    available_skills: frozenset[str] | None,
) -> bool:
    return available_skills is None or skill.name in available_skills


def _content_location(source: VisibleSkill) -> tuple[str | None, str]:
    """Active private bodies come from the immutable draft commit."""
    version = source.version
    return version.draft_commit, version.skill_path


class SkillSearchService:
    """Retrieval over the active projection, confirmed by the SQL catalog."""

    def __init__(
        self,
        store: SqlSkillStore,
        projection: SkillProjectionStore | None,
        content: SkillContentStore,
        embedder: Embedder | None,
        config: SkillSearchConfig,
        reranker: Reranker | None = None,
        usage: SkillUsageService | None = None,
    ) -> None:
        """Projection and embedder are optional capabilities, not algorithms.

        Query embedding is skipped when the dense channel is disabled, but the
        Milvus projection is still the only retrieval index: keyword recall is
        server-side BM25.  The reranker is the third stage: it can only reorder
        candidates that already survived recall and confirmation, and it never
        decides visibility.
        """
        self._store = store
        self._projection = projection
        self._content = content
        self._embedder = embedder
        self._config = config
        self._reranker = reranker
        self._usage = usage

    async def load_skill(
        self, *, user_id: str, skill_id: str
    ) -> SkillSearchHit | None:
        """Load one active, enabled private skill by its internal id."""
        try:
            source = await self._store.get_active_visible(
                scope=SkillScope.PRIVATE,
                skill_id=skill_id,
                owner_user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001 - tool maps this to unavailable
            raise SkillSearchUnavailable(
                "skills catalog confirmation is unavailable"
            ) from exc
        if source is None or not source.skill.enabled:
            return None
        body = await self._read_body(source)
        if body is None:
            return None
        if self._usage is not None:
            await self._usage.record_load(user_id, skill_id)
        return SkillSearchHit(
            skill=source.skill,
            body=body,
            score=1.0,
            scope=_scope_value(source.scope),
            version_id=source.version.id,
        )

    async def search(
        self,
        *,
        user_id: str,
        task_query: str,
        domain_keywords: Sequence[str],
        available_skills: frozenset[str] | None,
    ) -> SkillSearchOutcome:
        query = (task_query or "").strip()
        if not query:
            return SkillSearchOutcome(hits=(), candidates=0, warnings=())

        warnings: list[str] = []
        ranked, channels = await self._milvus_ranked(
            user_id=user_id,
            query=query,
            domain_keywords=domain_keywords,
            warnings=warnings,
        )
        if not ranked:
            return SkillSearchOutcome(
                hits=(), candidates=0, warnings=tuple(dict.fromkeys(warnings))
            )

        # candidates 是“召回规模”：个人偏好/白名单在装载前才生效，它们不该让
        # 调用方以为只召回了一条。
        candidates = len(ranked)
        head = ranked[: max(1, self._config.candidate_k)]
        ordered = [
            (source, score)
            for source, score in head
            if _allows(source.skill, available_skills)
        ]
        bodies: dict[str, str] = {}
        ordered, reranked = await self._rerank_candidates(
            query=query, ordered=ordered, bodies=bodies, warnings=warnings
        )
        outcome = await self._load(
            ordered,
            candidates=candidates,
            warnings=warnings,
            bodies=bodies or None,
            reranked=reranked,
        )
        return replace(outcome, channels=channels)

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------

    async def _query_vector(self, query: str) -> list[float] | None:
        """Embed the task intent exactly once; a failure degrades to BM25."""
        embedding = self._config.embedding
        if self._embedder is None or not embedding.enabled or not embedding.base_url.strip():
            return None
        try:
            vector = list(await self._embedder.embed_query(query))
        except Exception as exc:  # noqa: BLE001 - BM25 still answers
            logger.warning(
                "Skill query embedding failed (%s); using Milvus BM25",
                type(exc).__name__,
            )
            return None
        return vector or None

    def _keyword_query(self, query: str, domain_keywords: Sequence[str]) -> str:
        """Build the BM25 query string; Milvus does all tokenization/scoring."""
        if not self._config.keyword.enabled:
            return ""
        parts = [query, *(item.strip() for item in domain_keywords)]
        return " ".join(dict.fromkeys(item for item in parts if item))

    async def _milvus_ranked(
        self,
        *,
        user_id: str,
        query: str,
        domain_keywords: Sequence[str],
        warnings: list[str],
    ) -> tuple[list[tuple[VisibleSkill, float]], tuple[str, ...]]:
        """One Milvus recall, one PG confirmation, no client-side fusion.

        Dense + BM25 uses hybrid_search with Milvus RRFRanker. When only one
        channel is configured (or the embedding call failed), the remaining
        channel is still a Milvus search; it is never scored in this process.
        """
        if self._projection is None:
            warnings.append(RETRIEVAL_UNAVAILABLE)
            return [], ()

        embedding = self._config.embedding
        vector = await self._query_vector(query)
        keyword = self._keyword_query(query, domain_keywords)
        if embedding.enabled and vector is None and keyword:
            warnings.append(VECTOR_UNAVAILABLE)
        if vector is None and not keyword:
            warnings.append(
                VECTOR_UNAVAILABLE if embedding.enabled else KEYWORD_UNAVAILABLE
            )
            return [], ()

        channels: tuple[str, ...]
        if vector is not None and keyword:
            channels = (CHANNEL_EMBEDDING, CHANNEL_KEYWORD)
        elif vector is not None:
            channels = (CHANNEL_EMBEDDING,)
        else:
            channels = (CHANNEL_KEYWORD,)

        try:
            if vector is not None and keyword:
                hits = await self._projection.hybrid_search(
                    user_id=user_id,
                    vector=vector,
                    keyword_query=keyword,
                    limit=self._config.candidate_k,
                    rrf_k=self._config.rrf_k,
                )
            elif vector is not None:
                hits = await self._projection.search(
                    user_id=user_id,
                    vector=vector,
                    limit=self._config.candidate_k,
                )
            else:
                hits = await self._projection.keyword_search(
                    user_id=user_id,
                    query=keyword,
                    limit=self._config.candidate_k,
                )
        except Exception as exc:  # noqa: BLE001 - no local scoring fallback
            logger.warning(
                "Skill projection search failed (%s); returning no unconfirmed hits",
                type(exc).__name__,
            )
            warnings.append(
                KEYWORD_UNAVAILABLE if channels == (CHANNEL_KEYWORD,) else VECTOR_UNAVAILABLE
            )
            return [], ()

        try:
            confirmed = await self._store.confirm_hits(user_id, hits)
        except Exception as exc:  # noqa: BLE001 - never return unconfirmed rows
            raise SkillSearchUnavailable(
                "skills catalog confirmation is unavailable"
            ) from exc

        # PostgreSQL returns a set; Milvus' order is the ranking. Rebuild the
        # order from the hit list so the server-side RRF order survives confirmation.
        by_key = {
            (_scope_value(source.scope), source.version.skill_id, source.version.id): source
            for source in confirmed
        }
        ranked: list[tuple[VisibleSkill, float]] = []
        seen: set[tuple[str, str, str]] = set()
        for hit in hits:
            key = (_scope_value(hit.scope), hit.skill_id, hit.version_id)
            source = by_key.get(key)
            if source is None or key in seen:
                continue
            score = float(hit.score)
            if self._config.min_score is not None and score < self._config.min_score:
                continue
            ranked.append((source, score))
            seen.add(key)
        return ranked, channels

    async def _rerank_candidates(
        self,
        *,
        query: str,
        ordered: list[tuple[VisibleSkill, float]],
        bodies: dict[str, str],
        warnings: list[str],
    ) -> tuple[list[tuple[VisibleSkill, float]], bool]:
        """用正文精排已确认、已去重的候选（设计 §7.4 步骤 7）。

        输入顺序是融合顺序；精排只重排前 candidate_k 个，尾部原样接在后面。
        reranker 缺失、候选不足两个、调用失败或返回空顺序，都退回原顺序并记
        rerank_unavailable —— 精排永远不能成为“没有结果”的原因。
        """
        reranker = self._reranker
        if reranker is None or len(ordered) < 2:
            return ordered, False

        head = ordered[: max(1, self._config.candidate_k)]
        kept: list[tuple[VisibleSkill, float]] = []
        documents: list[str] = []
        for source, score in head:
            skill_id = source.version.skill_id
            body = bodies.get(skill_id)
            if body is None:
                body = await self._read_body(source) or ""
                bodies[skill_id] = body
            documents.append(
                _rerank_text(
                    source.skill, body, self._config.rerank.max_document_chars
                )
            )
            kept.append((source, score))

        try:
            results = await reranker.rerank(query=query, documents=documents)
        except Exception as exc:  # noqa: BLE001 - the fused order still answers
            logger.warning(
                "Skill rerank failed (%s); keeping the fused order",
                type(exc).__name__,
            )
            warnings.append(RERANK_UNAVAILABLE)
            return ordered, False

        order: list[int] = []
        for item in results:
            index = getattr(item, "index", None)
            if isinstance(index, int) and 0 <= index < len(kept) and index not in order:
                order.append(index)
        if not order:
            warnings.append(RERANK_UNAVAILABLE)
            return ordered, False
        # 服务漏掉的候选按原顺序补在后面：不能因为精排少返回一条就丢掉技能。
        order += [index for index in range(len(kept)) if index not in order]
        return [kept[index] for index in order] + ordered[len(kept) :], True

    async def _read_body(self, source: VisibleSkill) -> str | None:
        commit, path = _content_location(source)
        if not commit:
            logger.warning(
                "Active skill %s has no content commit", source.version.skill_id
            )
            return None
        try:
            text = await self._content.read_text(commit, path)
        except Exception as exc:  # noqa: BLE001 - one unreadable skill is not fatal
            logger.warning(
                "Could not read active skill %s content (%s)",
                source.version.skill_id,
                type(exc).__name__,
            )
            return None
        return strip_frontmatter(text)

    async def _load(
        self,
        ordered: Sequence[tuple[VisibleSkill, float]],
        *,
        candidates: int,
        warnings: list[str],
        bodies: Mapping[str, str] | None = None,
        reranked: bool = False,
    ) -> SkillSearchOutcome:
        """Read, clip, and cap the loaded bodies under the context budget."""
        remaining = self._config.max_context_chars
        hits: list[SkillSearchHit] = []
        for source, score in ordered:
            if len(hits) >= self._config.top_k:
                break
            skill_id = source.version.skill_id
            body = bodies.get(skill_id) if bodies is not None else None
            if body is None:
                body = await self._read_body(source)
            if body is None:
                warnings.append(CONTENT_UNAVAILABLE)
                continue
            budget = min(self._config.max_body_chars, remaining)
            if budget < MIN_USEFUL_BODY_CHARS and budget < len(body):
                break
            clipped, _truncated = clip_body(body, budget)
            remaining -= len(clipped)
            hits.append(
                SkillSearchHit(
                    skill=source.skill,
                    body=clipped,
                    score=round(float(score), 4),
                    scope=_scope_value(source.scope),
                    version_id=source.version.id,
                )
            )

        return SkillSearchOutcome(
            hits=tuple(hits),
            candidates=candidates,
            warnings=tuple(dict.fromkeys(warnings)),
            reranked=reranked,
        )


# ----------------------------------------------------------------------
# process-level runtime
# ----------------------------------------------------------------------

_runtime: SkillSearchService | None = None


def configure_skill_runtime(service: SkillSearchService | None) -> None:
    """Install the process-wide search service (Task 15 wires the real one)."""
    global _runtime
    _runtime = service


def get_skill_runtime() -> SkillSearchService | None:
    """Return the configured service; tools must never build a live one themselves."""
    return _runtime


def reset_skill_runtime() -> None:
    global _runtime
    _runtime = None


__all__ = [
    "CONTENT_UNAVAILABLE",
    "KEYWORD_UNAVAILABLE",
    "RETRIEVAL_UNAVAILABLE",
    "VECTOR_UNAVAILABLE",
    "SkillSearchHit",
    "SkillSearchOutcome",
    "SkillSearchService",
    "SkillSearchUnavailable",
    "configure_skill_runtime",
    "get_skill_runtime",
    "reset_skill_runtime",
]
