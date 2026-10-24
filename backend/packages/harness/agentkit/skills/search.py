"""One-query skill retrieval over the active projection (Task 12).

The catalog path is deliberately small and linear:

    query -> one embedding -> one Milvus search -> one PostgreSQL confirmation
          -> read bodies from the commit cache -> sort, prefer private, budget

Milvus only narrows the candidate set.  Every hit that becomes visible has been
confirmed against the authoritative public/private active pointer, so a stale
projection row can never leak content.  PostgreSQL is the authority: when the
catalog cannot be read the request fails closed instead of returning
unconfirmed hits.

A private row always wins over a public row for the same skill id.  The two are
not compared by score: "my copy" is a different object with different content,
and ranking must not decide which one the owner sees.  If the private body is
unreadable the skill is dropped with a warning; falling back to the public body
would silently load content the owner never approved.

A vector outage is not a retrieval outage: when the query embedding or the
Milvus search fails, the same visibility rules are applied to the catalog and
the existing BM25 scorer ranks those skills locally (lexical_fallback).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agentkit.config.skills_config import SkillSearchConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import SkillScope, VisibleSkill
from agentkit.skills.loader import MIN_USEFUL_BODY_CHARS, clip_body
from agentkit.skills.parser import FRONTMATTER_RE
from agentkit.skills.projection import ProjectionHit, SkillProjectionStore
from agentkit.skills.router import Embedder, SkillDocument, lexical_scores, tokenize
from agentkit.skills.store.sql import SqlSkillStore
from agentkit.skills.types import Skill

logger = logging.getLogger(__name__)

#: Warnings the tool may hand back to the model.
VECTOR_UNAVAILABLE = "vector_unavailable"
LEXICAL_FALLBACK = "lexical_fallback"
CONTENT_UNAVAILABLE = "content_unavailable"


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
    release_id: str | None


@dataclass(frozen=True, slots=True)
class SkillSearchOutcome:
    hits: tuple[SkillSearchHit, ...]
    candidates: int
    warnings: tuple[str, ...]


def _scope_value(scope: SkillScope | str) -> str:
    return scope.value if isinstance(scope, SkillScope) else str(scope)


def _preference_allows(skill: Skill, prefs: Mapping[str, bool] | None) -> bool:
    """Personal prefs are keyed by skill id (catalog) or name (legacy file)."""
    if prefs is None:
        return True
    if skill.skill_id is not None and skill.skill_id in prefs:
        return bool(prefs[skill.skill_id])
    return bool(prefs.get(skill.name, True))


def _allows(
    skill: Skill,
    available_skills: frozenset[str] | None,
    prefs: Mapping[str, bool] | None,
) -> bool:
    if not _preference_allows(skill, prefs):
        return False
    return available_skills is None or skill.name in available_skills


def _prefer_private(sources: Sequence[VisibleSkill]) -> list[VisibleSkill]:
    """Collapse public/private rows per skill id; private wins unconditionally."""
    chosen: dict[str, VisibleSkill] = {}
    for source in sources:
        skill_id = source.version.skill_id
        current = chosen.get(skill_id)
        if current is None or (
            source.scope is SkillScope.PRIVATE
            and current.scope is not SkillScope.PRIVATE
        ):
            chosen[skill_id] = source
    return list(chosen.values())


def _content_location(source: VisibleSkill) -> tuple[str | None, str]:
    """Public bodies come from the release commit; private from the draft commit."""
    if source.scope is SkillScope.PUBLIC:
        release = source.release
        if release is None:
            return None, ""
        return release.git_commit, release.skill_path
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
    ) -> None:
        """Projection and embedder are optional: without them only BM25 runs.

        The shipped configuration is lexical-only (search.enabled=true,
        embedding.enabled=false), so a missing projection/embedder must mean
        "local BM25", never "no retrieval service at all".
        """
        self._store = store
        self._projection = projection
        self._content = content
        self._embedder = embedder
        self._config = config

    async def search(
        self,
        *,
        user_id: str,
        task_query: str,
        domain_keywords: Sequence[str],
        available_skills: frozenset[str] | None,
        prefs: Mapping[str, bool] | None,
    ) -> SkillSearchOutcome:
        query = (task_query or "").strip()
        if not query:
            return SkillSearchOutcome(hits=(), candidates=0, warnings=())

        private_skill_ids = await self._private_skill_ids(user_id)
        warnings: list[str] = []
        vector = await self._query_vector(query)
        if vector is None:
            warnings.append(VECTOR_UNAVAILABLE)
            return await self._lexical(
                user_id=user_id,
                query=query,
                domain_keywords=domain_keywords,
                available_skills=available_skills,
                prefs=prefs,
                warnings=warnings,
            )

        if self._projection is None:  # 没有投影 = 只跑词法通道，绝不猜
            warnings.append(VECTOR_UNAVAILABLE)
            return await self._lexical(
                user_id=user_id,
                query=query,
                domain_keywords=domain_keywords,
                available_skills=available_skills,
                prefs=prefs,
                warnings=warnings,
            )

        try:
            hits = await self._projection.search(
                user_id=user_id,
                private_skill_ids=private_skill_ids,
                vector=vector,
                limit=self._config.candidate_k,
            )
        except Exception as exc:  # noqa: BLE001 - the lexical channel is the fallback
            logger.warning(
                "Skill projection search failed (%s); using lexical scoring",
                type(exc).__name__,
            )
            warnings.append(VECTOR_UNAVAILABLE)
            return await self._lexical(
                user_id=user_id,
                query=query,
                domain_keywords=domain_keywords,
                available_skills=available_skills,
                prefs=prefs,
                warnings=warnings,
            )

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

        chosen = _prefer_private(confirmed)
        candidates = len(chosen)
        ordered = [
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
            for source in chosen
            if _allows(source.skill, available_skills, prefs)
        ]
        ordered.sort(
            key=lambda item: (
                -item[1],
                item[0].skill.name,
                item[0].version.skill_id,
            )
        )
        return await self._load(ordered, candidates=candidates, warnings=warnings)

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------

    async def _private_skill_ids(self, user_id: str) -> tuple[str, ...]:
        try:
            return await self._store.private_skill_ids(user_id)
        except Exception as exc:  # noqa: BLE001 - the catalog is the authority
            raise SkillSearchUnavailable("skills catalog is unavailable") from exc

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

    async def _lexical(
        self,
        *,
        user_id: str,
        query: str,
        domain_keywords: Sequence[str],
        available_skills: frozenset[str] | None,
        prefs: Mapping[str, bool] | None,
        warnings: list[str],
    ) -> SkillSearchOutcome:
        warnings.append(LEXICAL_FALLBACK)
        try:
            visible = await self._store.visible_for_user(user_id)
        except Exception as exc:  # noqa: BLE001 - fail closed, not fail open
            raise SkillSearchUnavailable("skills catalog is unavailable") from exc

        chosen = _prefer_private(visible)
        candidates = len(chosen)
        bodies: dict[str, str] = {}
        documents: list[SkillDocument] = []
        for source in chosen:
            if not _allows(source.skill, available_skills, prefs):
                continue
            body = await self._read_body(source)
            if body is None:
                warnings.append(CONTENT_UNAVAILABLE)
                continue
            bodies[source.version.skill_id] = body
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
        ordered = [
            (by_id[document.skill.skill_id], score)
            for document, score in ranked
            if score > 0 and document.skill.skill_id is not None
        ]
        return await self._load(
            ordered, candidates=candidates, warnings=warnings, bodies=bodies
        )

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
                    release_id=source.release.id if source.release is not None else None,
                )
            )
        return SkillSearchOutcome(
            hits=tuple(hits),
            candidates=candidates,
            warnings=tuple(dict.fromkeys(warnings)),
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
