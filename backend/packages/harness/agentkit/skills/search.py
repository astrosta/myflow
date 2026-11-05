"""One-query skill retrieval over the active projection (Task 12).

The catalog path is deliberately small and linear:

    query -> one embedding -> one Milvus search -> one PostgreSQL confirmation
          -> read bodies from the commit cache -> sort, budget

Milvus only narrows the candidate set.  Every hit that becomes visible has been
confirmed against the caller's active private pointer, so a stale projection row
can never leak content.  PostgreSQL is the authority: when the catalog cannot be
read the request fails closed instead of returning unconfirmed hits.

There is no public catalog in v2.  A user's retrieval can only return skills
whose owner_user_id equals the caller and whose version is currently active.
A vector outage is not a retrieval outage: when the query embedding or the
Milvus search fails, the same visibility rules are applied to the active catalog
and the existing BM25 scorer ranks those skills locally (lexical_fallback).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from agentkit.config.skills_config import SkillSearchConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import SkillScope, VisibleSkill
from agentkit.skills.loader import MIN_USEFUL_BODY_CHARS, clip_body
from agentkit.skills.parser import FRONTMATTER_RE
from agentkit.skills.projection import ProjectionHit, SkillProjectionStore
from agentkit.skills.router import (
    Embedder,
    Reranker,
    SkillDocument,
    lexical_scores,
    tokenize,
)
from agentkit.skills.store.sql import SqlSkillStore
from agentkit.skills.types import Skill
from agentkit.skills.usage import SkillUsageService

logger = logging.getLogger(__name__)

#: Warnings the tool may hand back to the model.
VECTOR_UNAVAILABLE = "vector_unavailable"
LEXICAL_FALLBACK = "lexical_fallback"
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


CHANNEL_EMBEDDING = 'embedding'
CHANNEL_KEYWORD = 'keyword'


def _fuse_ranked(
    dense: Sequence[tuple[VisibleSkill, float]],
    sparse: Sequence[tuple[VisibleSkill, float]],
    *,
    rrf_k: int,
) -> list[tuple[VisibleSkill, float, tuple[str, ...]]]:
    """RRF 融合两路名次；同一 skill_id 只留一条。

    两路的名次各自给 1/(k + rank) 的分，然后**归一化到最高分为 1.0**，这样
    min_score 仍然是配置里那个 0..1 语义（老 router 也是这么做的）。

    去重发生在这里而不是两路内部：同一技能可能只被向量或只被 BM25 召回，
    在合并处收敛后才不会出现两条命中。
    """

    picked: dict[str, VisibleSkill] = {}
    order: list[str] = []
    channels: dict[str, list[str]] = {}
    fused: dict[str, float] = {}
    k = max(1, int(rrf_k))

    for name, ranked in ((CHANNEL_EMBEDDING, dense), (CHANNEL_KEYWORD, sparse)):
        for rank, (source, _score) in enumerate(ranked):
            skill_id = source.version.skill_id
            if skill_id not in picked:
                picked[skill_id] = source
                order.append(skill_id)
                channels[skill_id] = []
                fused[skill_id] = 0.0
            fused[skill_id] += 1.0 / (k + rank + 1)
            if name not in channels[skill_id]:
                channels[skill_id].append(name)

    if not order:
        return []
    best = max(fused.values())
    scored = [
        (picked[skill_id], fused[skill_id] / best, tuple(channels[skill_id]))
        for skill_id in order
    ]
    scored.sort(key=lambda item: (-item[1], item[0].skill.name, item[0].version.skill_id))
    return scored


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


def _strip_frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return (text[match.end() :] if match is not None else text).strip()


def _document(skill: Skill, body: str) -> SkillDocument:
    """The router's document shape, built from an already-read catalog body."""
    head = frozenset(tokenize(f"{skill.name} {skill.description}"))
    counts = Counter(tokenize(f"{skill.name} {skill.description}\n{body}"))
    return SkillDocument(
        skill=skill,
        body=body,
        counts=counts,
        length=sum(counts.values()),
        head_tokens=head,
    )


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
        """Projection, embedder and reranker are all optional.

        The shipped configuration is lexical-only (search.enabled=true,
        embedding.enabled=false), so a missing projection/embedder must mean
        "local BM25", never "no retrieval service at all".  The reranker is the
        third stage: it can only reorder candidates that already survived
        recall, confirmation and private dedupe, and it never decides
        visibility.
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
        dense = await self._dense_ranked(
            user_id=user_id,
            query=query,
            available_skills=available_skills,
            warnings=warnings,
        )
        sparse = await self._sparse_ranked(
            user_id=user_id,
            query=query,
            domain_keywords=domain_keywords,
            available_skills=available_skills,
            warnings=warnings,
        )
        if not dense and not sparse:
            return SkillSearchOutcome(
                hits=(), candidates=0, warnings=tuple(dict.fromkeys(warnings))
            )
        if not dense:
            # 只剩词法通道：老名字保留，工具靠它解释“为什么没有向量”。
            warnings.append(LEXICAL_FALLBACK)

        fused = _fuse_ranked(dense, sparse, rrf_k=self._config.rrf_k)
        if self._config.min_score is not None:
            fused = [item for item in fused if item[1] >= self._config.min_score]
        if not fused:
            return SkillSearchOutcome(
                hits=(), candidates=0, warnings=tuple(dict.fromkeys(warnings))
            )

        channels = (
            CHANNEL_EMBEDDING if dense else None,
            CHANNEL_KEYWORD if sparse else None,
        )
        # candidates 是“召回规模”：个人偏好/白名单在装载前才生效，它们不该让
        # 调用方以为只召回了一条。
        candidates = len(fused)
        head = fused[: max(1, self._config.candidate_k)]
        ordered = [
            (source, score)
            for source, score, _names in head
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
        return replace(
            outcome,
            channels=tuple(name for name in channels if name is not None),
        )

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------

    async def _query_vector(self, query: str) -> list[float] | None:
        """Embed the task intent exactly once; a failure degrades to lexical."""
        embedding = self._config.embedding
        if self._embedder is None or not embedding.enabled or not embedding.base_url.strip():
            return None
        try:
            vector = list(await self._embedder.embed_query(query))
        except Exception as exc:  # noqa: BLE001 - BM25 still answers
            logger.warning(
                "Skill query embedding failed (%s); using lexical scoring",
                type(exc).__name__,
            )
            return None
        return vector or None

    async def _dense_ranked(
        self,
        *,
        user_id: str,
        query: str,
        available_skills: frozenset[str] | None,
        warnings: list[str],
    ) -> list[tuple[VisibleSkill, float]]:
        """稠密通道：一次 query embedding + 一次 Milvus search + 一次 PG 确认。

        没有向量能力（未配置、embedding 失败、没有投影）或 Milvus 报错时返回空
        列表并记 vector_unavailable——空列表不是“没有结果”，词法通道还会给候选。
        """
        vector = await self._query_vector(query)
        if vector is None or self._projection is None:
            warnings.append(VECTOR_UNAVAILABLE)
            return []
        try:
            hits = await self._projection.search(
                user_id=user_id,
                vector=vector,
                limit=self._config.candidate_k,
            )
        except Exception as exc:  # noqa: BLE001 - the lexical channel still answers
            logger.warning(
                "Skill projection search failed (%s); using lexical scoring",
                type(exc).__name__,
            )
            warnings.append(VECTOR_UNAVAILABLE)
            return []
        try:
            confirmed = await self._store.confirm_hits(user_id, hits)
        except Exception as exc:  # noqa: BLE001 - never fall back to unconfirmed rows
            raise SkillSearchUnavailable(
                "skills catalog confirmation is unavailable"
            ) from exc

        scores: dict[tuple[str, str, str], float] = {}
        for hit in hits:
            key = (_scope_value(hit.scope), hit.skill_id, hit.version_id)
            scores[key] = max(scores.get(key, 0.0), float(hit.score))

        ranked = [
            (
                source,
                scores.get(
                    (
                        _scope_value(source.scope),
                        source.version.skill_id,
                        source.version.id,
                    ),
                    0.0,
                ),
            )
            for source in confirmed
        ]
        ranked.sort(
            key=lambda item: (-item[1], item[0].skill.name, item[0].version.skill_id)
        )
        return ranked

    async def _sparse_ranked(
        self,
        *,
        user_id: str,
        query: str,
        domain_keywords: Sequence[str],
        available_skills: frozenset[str] | None,
        warnings: list[str],
    ) -> list[tuple[VisibleSkill, float]]:
        """稀疏通道：对 PG 可见集合做本地 BM25（读正文，不依赖任何外部服务）。

        正文是“报错原文 / 库名 / 命令”唯一能被字面命中的地方，所以这条通道不是
        兜底，而是和向量并行的一路；读不出正文只丢该候选，不影响整体。
        """
        if not self._config.keyword.enabled:
            return []
        try:
            visible = await self._store.visible_for_user(user_id)
        except Exception as exc:  # noqa: BLE001 - fail closed, not fail open
            raise SkillSearchUnavailable("skills catalog is unavailable") from exc

        chosen = list(visible)
        documents: list[SkillDocument] = []
        for source in chosen:
            body = await self._read_body(source)
            if body is None:
                warnings.append(CONTENT_UNAVAILABLE)
                continue
            documents.append(_document(source.skill, body))

        scores = lexical_scores(
            documents,
            task_query=query,
            domain_keywords=domain_keywords,
            config=self._config.keyword,
        )
        by_id = {source.version.skill_id: source for source in chosen}
        ranked = sorted(
            ((document, scores.get(document.key, 0.0)) for document in documents),
            key=lambda item: (
                -item[1],
                item[0].skill.name,
                item[0].skill.skill_id or "",
            ),
        )
        return [
            (by_id[document.skill.skill_id], score)
            for document, score in ranked
            if score > 0 and document.skill.skill_id is not None
        ]

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
        return _strip_frontmatter(text)

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
    "LEXICAL_FALLBACK",
    "VECTOR_UNAVAILABLE",
    "SkillSearchHit",
    "SkillSearchOutcome",
    "SkillSearchService",
    "SkillSearchUnavailable",
    "configure_skill_runtime",
    "get_skill_runtime",
    "reset_skill_runtime",
]
