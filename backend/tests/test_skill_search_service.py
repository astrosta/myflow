"""One-query retrieval over the caller's active private skill projection.

Every request is one query embedding plus one Milvus search.  PostgreSQL
confirms the hits against the caller's active pointers, the commit cache
supplies bodies, and no other user's skill can survive confirmation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.config.skills_config import (
    SkillEmbeddingChannelConfig,
    SkillKeywordChannelConfig,
    SkillRerankStageConfig,
    SkillSearchConfig,
)
from agentkit.persistence.schema import Base
from agentkit.skills.domain import PackageManifest, StagedFile
from agentkit.skills.package import build_package
from agentkit.skills.projection import MilvusSkillProjection, ProjectionHit
from agentkit.skills.search import (
    CONTENT_UNAVAILABLE,
    LEXICAL_FALLBACK,
    RERANK_UNAVAILABLE,
    VECTOR_UNAVAILABLE,
    SkillSearchService,
    SkillSearchUnavailable,
)
from agentkit.skills.store.sql import SqlSkillStore

LIMITS = {"max_files": 20, "max_file_bytes": 4096, "max_package_bytes": 16384}


def skill_md(name: str, description: str = "Does things", body: str = "# Body\n") -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}".encode()


def manifest(
    name: str, *, description: str = "Does things", body: str = "# Body\n"
) -> PackageManifest:
    files = {"SKILL.md": StagedFile("SKILL.md", skill_md(name, description, body), "text")}
    return build_package(files, **LIMITS)


class FakeProjection:
    """Records the filter contract without a Milvus server."""

    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []
        self.hits: list[ProjectionHit] = []
        self.fail = False

    async def search(
        self,
        *,
        user_id: str,
        vector: list[float] | tuple[float, ...],
        limit: int,
    ) -> list[ProjectionHit]:
        self.searches.append(
            {
                "user_id": user_id,
                "vector": list(vector),
                "limit": limit,
                "filter": MilvusSkillProjection._filter(user_id),
            }
        )
        if self.fail:
            raise RuntimeError("Milvus is unavailable")
        return list(self.hits)


class FakeContent:
    """Commit cache backed by a dict; a missing row fails like a missing commit."""

    def __init__(self) -> None:
        self.texts: dict[tuple[str, str], str] = {}
        self.reads: list[tuple[str, str]] = []

    async def read_text(self, commit: str, path: str) -> str:
        self.reads.append((commit, path))
        try:
            return self.texts[(commit, path)]
        except KeyError:
            raise FileNotFoundError(f"{commit}:{path}") from None


class FakeEmbedder:
    def __init__(self) -> None:
        self.query_calls: list[str] = []
        self.fail = False
        self.vector = [1.0, 0.0, 0.0]

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        if self.fail:
            raise RuntimeError("embedding service is unavailable")
        return list(self.vector)

    async def embed_documents(self, texts: list[str] | tuple[str, ...]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]


class FakeReranker:
    """Records what it was asked to order; returns a caller-supplied order."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.queries: list[str] = []
        self.order: list[int] | None = None
        self.fail = False

    async def rerank(self, *, query: str, documents: list[str] | tuple[str, ...]):
        self.queries.append(query)
        self.calls.append(list(documents))
        if self.fail:
            raise RuntimeError("rerank service is unavailable")
        order = self.order if self.order is not None else list(range(len(documents)))
        return [SimpleNamespace(index=index) for index in order]


class FakeUsage:
    """只记调用：使用次数的 Redis/DB 细节由 test_skill_usage.py 负责。"""

    def __init__(self) -> None:
        self.loads: list[tuple[str, str]] = []

    async def record_load(self, user_id: str, skill_id: str) -> None:
        self.loads.append((user_id, skill_id))


@dataclass
class Harness:
    store: SqlSkillStore
    session_factory: async_sessionmaker[AsyncSession]
    projection: FakeProjection = field(default_factory=FakeProjection)
    content: FakeContent = field(default_factory=FakeContent)
    embedder: FakeEmbedder = field(default_factory=FakeEmbedder)
    reranker: FakeReranker | None = None
    usage: FakeUsage | None = None

    def service(self, config: SkillSearchConfig | None = None) -> SkillSearchService:
        return SkillSearchService(
            self.store,
            self.projection,  # type: ignore[arg-type]
            self.content,  # type: ignore[arg-type]
            self.embedder,
            config or search_config(),
            self.reranker,  # type: ignore[arg-type]
            usage=self.usage,  # type: ignore[arg-type]
        )


def search_config(**overrides: Any) -> SkillSearchConfig:
    embedding = SkillEmbeddingChannelConfig(
        enabled=True, base_url="http://embed.invalid", dimensions=3
    )
    values = {"embedding": embedding, **overrides}
    return SkillSearchConfig(**values)


@pytest.fixture(name="harness")
def harness_fixture(tmp_path: Path):
    async def _build() -> tuple[Harness, object]:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "skills.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return Harness(SqlSkillStore(factory), factory), engine

    built, engine = asyncio.run(_build())
    yield built
    asyncio.run(engine.dispose())


async def save_private(
    harness: Harness,
    *,
    owner: str,
    name: str,
    body: str = "# Body\n",
    description: str = "Does things",
    seq: int = 1,
) -> tuple[Any, Any]:
    skill = await harness.store.create_skill(owner_user_id=owner, private_name=name)
    commit = chr(ord("a") + seq) * 40
    path = f"users/{owner}/{skill.id}/SKILL.md"
    version = await harness.store.record_saved_version(
        skill_id=skill.id,
        owner_user_id=owner,
        manifest=manifest(name, description=description, body=body),
        draft_ref=f"refs/heads/myflow/drafts/{owner}",
        draft_commit=commit,
        skill_path=path,
    )
    harness.content.texts[(commit, path)] = skill_md(name, description, body).decode()
    scan = await harness.store.record_security_scan(
        skill_id=skill.id,
        owner_user_id=owner,
        version_id=version.id,
        status="completed",
        is_safe=True,
        max_severity="SAFE",
        findings=[],
        scanner="cisco-ai-skill-scanner",
        scanner_version="2.3.2",
        duration_ms=1,
    )
    await harness.store.activate_version(
        skill_id=skill.id,
        owner_user_id=owner,
        version_id=version.id,
        scan_id=scan.id,
        actor=owner,
    )
    return skill, version


def hit(
    *,
    owner: str,
    skill_id: str,
    version_id: str,
    score: float = 0.5,
) -> ProjectionHit:
    return ProjectionHit(
        projection_key=f"private:{owner}:{skill_id}",
        scope="private",
        owner_user_id=owner,
        skill_id=skill_id,
        version_id=version_id,
        score=score,
    )


def call(harness: Harness, **overrides: Any):
    config = overrides.pop("config", None)
    values = {
        "user_id": "u1",
        "task_query": "pgvector",
        "domain_keywords": [],
        "available_skills": None,
        **overrides,
    }
    return harness.service(config).search(**values)


@pytest.mark.asyncio
async def test_one_search_with_owner_filter(harness: Harness) -> None:
    skill, version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# Private body\npgvector 多租户排查\n"
    )
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    outcome = await call(harness)

    assert len(harness.projection.searches) == 1
    assert harness.projection.searches[0]["filter"] == (
        'scope == "private" and enabled == true and owner_user_id == "u1"'
    )
    assert "skill_id not in" not in harness.projection.searches[0]["filter"]
    assert [item.scope for item in outcome.hits] == ["private"]
    assert outcome.hits[0].skill.skill_id == skill.id
    assert outcome.hits[0].version_id == version.id
    assert "pgvector" in outcome.hits[0].body


@pytest.mark.asyncio
async def test_other_users_private_hits_are_rejected(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u2", name="other")
    harness.projection.hits = [
        hit(owner="u2", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    outcome = await call(harness)
    assert outcome.hits == ()


@pytest.mark.asyncio
async def test_a_stale_active_version_is_dropped(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id="stale", score=0.9)
    ]

    assert (await call(harness)).hits == ()
    assert version.id != "stale"


@pytest.mark.asyncio
async def test_rerank_reorders_confirmed_candidates_using_bodies(harness: Harness) -> None:
    alpha, alpha_version = await save_private(
        harness, owner="u1", name="alpha", body="# alpha\npgvector\n", seq=1
    )
    beta, beta_version = await save_private(
        harness, owner="u1", name="beta", body="# beta\npgvector\n", seq=2
    )
    harness.reranker = FakeReranker()
    harness.reranker.order = [1, 0]
    harness.projection.hits = [
        hit(owner="u1", skill_id=alpha.id, version_id=alpha_version.id, score=0.9),
        hit(owner="u1", skill_id=beta.id, version_id=beta_version.id, score=0.5),
    ]

    outcome = await call(harness)

    assert [item.skill.skill_id for item in outcome.hits] == [beta.id, alpha.id]
    assert outcome.reranked is True


@pytest.mark.asyncio
async def test_rerank_failure_falls_back_to_the_fused_order(harness: Harness) -> None:
    alpha, alpha_version = await save_private(
        harness, owner="u1", name="alpha", seq=1
    )
    beta, beta_version = await save_private(harness, owner="u1", name="beta", seq=2)
    harness.reranker = FakeReranker()
    harness.reranker.fail = True
    harness.projection.hits = [
        hit(owner="u1", skill_id=alpha.id, version_id=alpha_version.id, score=0.9),
        hit(owner="u1", skill_id=beta.id, version_id=beta_version.id, score=0.5),
    ]

    outcome = await call(harness)

    assert RERANK_UNAVAILABLE in outcome.warnings
    assert len(outcome.hits) == 2


@pytest.mark.asyncio
async def test_bm25_recall_adds_candidates_the_vector_search_missed(harness: Harness) -> None:
    dense, dense_version = await save_private(
        harness, owner="u1", name="dense", description="unrelated", seq=1
    )
    lexical, lexical_version = await save_private(
        harness,
        owner="u1",
        name="lexical",
        description="pgvector specialist",
        seq=2,
    )
    harness.projection.hits = [
        hit(owner="u1", skill_id=dense.id, version_id=dense_version.id, score=0.9)
    ]

    outcome = await call(harness, config=search_config(top_k=5, candidate_k=5))

    assert {item.skill.skill_id for item in outcome.hits} == {dense.id, lexical.id}
    assert set(outcome.channels) == {"embedding", "keyword"}
    assert lexical_version.id


@pytest.mark.asyncio
async def test_min_score_applies_to_the_normalised_fused_score(harness: Harness) -> None:
    skill, version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# Body\n"
    )
    workdir = search_config(min_score=1.0, top_k=5, candidate_k=5)
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    outcome = await call(harness, config=workdir)
    assert outcome.hits[0].score == 1.0


@pytest.mark.asyncio
async def test_no_channel_available_returns_no_hits_with_reasons(harness: Harness) -> None:
    await save_private(harness, owner="u1", name="pdf-tools")
    harness.embedder.fail = True
    config = search_config(
        keyword=SkillKeywordChannelConfig(enabled=False),
        top_k=5,
        candidate_k=5,
    )

    outcome = await call(harness, config=config)

    assert outcome.hits == ()
    assert VECTOR_UNAVAILABLE in outcome.warnings


@pytest.mark.asyncio
async def test_each_body_is_clipped_to_max_body_chars(harness: Harness) -> None:
    skill, version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# Body\n" + "x" * 500 + "\n"
    )
    config = search_config(max_body_chars=200, top_k=5, candidate_k=5)
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    outcome = await call(harness, config=config)
    assert len(outcome.hits[0].body) <= 200


@pytest.mark.asyncio
async def test_the_total_body_budget_stops_loading_more_hits(harness: Harness) -> None:
    first, first_version = await save_private(
        harness, owner="u1", name="first", body="# Body\n" + "a" * 200 + "\n", seq=1
    )
    second, second_version = await save_private(
        harness, owner="u1", name="second", body="# Body\n" + "b" * 200 + "\n", seq=2
    )
    config = search_config(
        max_body_chars=200, max_context_chars=210, top_k=5, candidate_k=5
    )
    harness.projection.hits = [
        hit(owner="u1", skill_id=first.id, version_id=first_version.id, score=0.9),
        hit(owner="u1", skill_id=second.id, version_id=second_version.id, score=0.8),
    ]

    outcome = await call(harness, config=config)
    assert len(outcome.hits) == 1


@pytest.mark.asyncio
async def test_a_missing_body_warns_and_is_dropped(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.content.texts.clear()
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    outcome = await call(harness)
    assert outcome.hits == ()
    assert CONTENT_UNAVAILABLE in outcome.warnings


@pytest.mark.asyncio
async def test_milvus_failure_falls_back_to_local_lexical_scoring(harness: Harness) -> None:
    await save_private(
        harness, owner="u1", name="pdf-tools", body="# Body\npgvector 排查\n"
    )
    harness.projection.fail = True

    outcome = await call(harness)

    assert VECTOR_UNAVAILABLE in outcome.warnings
    assert LEXICAL_FALLBACK in outcome.warnings
    assert [item.skill.name for item in outcome.hits] == ["pdf-tools"]


@pytest.mark.asyncio
async def test_embedding_failure_skips_the_vector_search(harness: Harness) -> None:
    await save_private(harness, owner="u1", name="pdf-tools", body="# Body\npgvector\n")
    harness.embedder.fail = True

    outcome = await call(harness)

    assert harness.projection.searches == []
    assert LEXICAL_FALLBACK in outcome.warnings
    assert outcome.hits


@pytest.mark.asyncio
async def test_disabled_embedding_channel_uses_lexical_scoring(harness: Harness) -> None:
    await save_private(harness, owner="u1", name="pdf-tools", body="# Body\npgvector\n")
    config = search_config(embedding=SkillEmbeddingChannelConfig(enabled=False))

    outcome = await call(harness, config=config)

    assert harness.projection.searches == []
    assert outcome.channels == ("keyword",)


@pytest.mark.asyncio
async def test_available_skills_and_disabled_switch_filter_hits(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    assert (await call(harness, available_skills=frozenset({"other"}))).hits == ()
    assert len((await call(harness)).hits) == 1

    await harness.store.set_skill_enabled(
        skill_id=skill.id,
        owner_user_id="u1",
        enabled=False,
        index_enabled=False,
    )
    assert (await call(harness)).hits == ()


@pytest.mark.asyncio
async def test_a_missing_catalog_fails_closed_without_unconfirmed_hits(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    async def broken_confirm(*args, **kwargs):
        raise RuntimeError("catalog is down")

    harness.store.confirm_hits = broken_confirm  # type: ignore[method-assign]
    with pytest.raises(SkillSearchUnavailable):
        await call(harness)


@pytest.mark.asyncio
async def test_an_empty_query_returns_no_hits(harness: Harness) -> None:
    outcome = await call(harness, task_query="   ")
    assert outcome.hits == ()
    assert harness.embedder.query_calls == []

@pytest.mark.asyncio
async def test_load_skill_returns_the_body_and_records_one_use(harness: Harness) -> None:
    skill, _version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# Private body\n先查 pg_stat_activity\n"
    )
    harness.usage = FakeUsage()

    loaded = await harness.service().load_skill(user_id="u1", skill_id=skill.id)

    assert loaded is not None
    assert loaded.skill.skill_id == skill.id
    assert "先查 pg_stat_activity" in loaded.body
    assert "---" not in loaded.body  # frontmatter 不进正文
    assert harness.usage.loads == [("u1", skill.id)]
    assert harness.projection.searches == []  # 按 id 加载不走召回


@pytest.mark.asyncio
async def test_search_never_counts_as_a_load(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.usage = FakeUsage()
    harness.projection.hits = [
        hit(owner="u1", skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    outcome = await call(harness)

    assert len(outcome.hits) == 1
    assert harness.usage.loads == []


@pytest.mark.asyncio
async def test_load_skill_is_scoped_to_the_owner(harness: Harness) -> None:
    skill, _version = await save_private(harness, owner="u2", name="pdf-tools")
    harness.usage = FakeUsage()

    assert await harness.service().load_skill(user_id="u1", skill_id=skill.id) is None
    assert harness.usage.loads == []


@pytest.mark.asyncio
async def test_load_skill_refuses_a_disabled_skill(harness: Harness) -> None:
    skill, _version = await save_private(harness, owner="u1", name="pdf-tools")
    await harness.store.set_skill_enabled(
        skill_id=skill.id, owner_user_id="u1", enabled=False, index_enabled=False
    )
    harness.usage = FakeUsage()

    assert await harness.service().load_skill(user_id="u1", skill_id=skill.id) is None
    assert harness.usage.loads == []


@pytest.mark.asyncio
async def test_load_skill_fails_when_the_body_cannot_be_read(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    path = f"users/u1/{skill.id}/SKILL.md"
    harness.content.texts.pop((version.draft_commit, path))  # type: ignore[misc]
    harness.usage = FakeUsage()

    assert await harness.service().load_skill(user_id="u1", skill_id=skill.id) is None
    assert harness.usage.loads == []
