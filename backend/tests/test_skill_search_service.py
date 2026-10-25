"""Task 12: one-query retrieval over the active skills projection.

Every request is one query embedding plus one Milvus search.  PostgreSQL
confirms the hits, the commit cache supplies the bodies, and a private row
always wins over a public row for the same skill id.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from types import SimpleNamespace

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
    SkillSearchHit,
    SkillSearchOutcome,
    SkillSearchService,
    SkillSearchUnavailable,
)
from agentkit.skills.store.sql import SqlSkillStore

LIMITS = {"max_files": 20, "max_file_bytes": 4096, "max_package_bytes": 16384}
PUBLIC_COMMIT = "1" * 40


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
        private_skill_ids: list[str] | tuple[str, ...],
        vector: list[float] | tuple[float, ...],
        limit: int,
    ) -> list[ProjectionHit]:
        self.searches.append(
            {
                "user_id": user_id,
                "private_skill_ids": list(private_skill_ids),
                "vector": list(vector),
                "limit": limit,
                "filter": MilvusSkillProjection._filter(user_id, private_skill_ids),
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


@dataclass
class Harness:
    store: SqlSkillStore
    session_factory: async_sessionmaker[AsyncSession]
    projection: FakeProjection = field(default_factory=FakeProjection)
    content: FakeContent = field(default_factory=FakeContent)
    embedder: FakeEmbedder = field(default_factory=FakeEmbedder)
    reranker: FakeReranker | None = None

    def service(self, config: SkillSearchConfig | None = None) -> SkillSearchService:
        return SkillSearchService(
            self.store,
            self.projection,  # type: ignore[arg-type]
            self.content,  # type: ignore[arg-type]
            self.embedder,
            config or search_config(),
            self.reranker,  # type: ignore[arg-type]
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
    return skill, version


async def publish(
    harness: Harness,
    *,
    owner: str,
    skill: Any,
    version: Any,
    public_name: str | None = None,
    body: str = "# Public body\n",
) -> Any:
    submission = await harness.store.create_submission(
        skill_id=skill.id,
        version=version,
        owner_user_id=owner,
        submitted_commit=version.draft_commit,
    )
    release = await harness.store.record_public_release(
        submission=submission,
        public_name=public_name or skill.private_name,
        git_repository="https://example.invalid/skills.git",
        git_commit=PUBLIC_COMMIT,
        skill_path=f"public/{public_name or skill.private_name}/SKILL.md",
        source_commit=version.draft_commit,
        actor="admin",
    )
    harness.content.texts[(PUBLIC_COMMIT, release.skill_path)] = skill_md(
        public_name or skill.private_name, body=body
    ).decode()
    return release


def hit(
    *,
    scope: str,
    owner: str | None,
    skill_id: str,
    version_id: str,
    score: float = 0.5,
) -> ProjectionHit:
    key = f"public:{skill_id}" if scope == "public" else f"private:{owner}:{skill_id}"
    return ProjectionHit(
        projection_key=key,
        scope=scope,
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
        "prefs": None,
        **overrides,
    }
    return harness.service(config).search(**values)


@pytest.mark.asyncio
async def test_one_search_and_private_precedence(harness: Harness) -> None:
    skill, version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# Private body\npgvector 多租户排查\n"
    )
    await publish(harness, owner="u1", skill=skill, version=version)
    harness.projection.hits = [
        hit(scope="public", owner=None, skill_id=skill.id, version_id=version.id, score=0.9),
        hit(scope="private", owner="u1", skill_id=skill.id, version_id=version.id, score=0.5),
    ]

    outcome = await call(harness)

    assert len(harness.projection.searches) == 1
    search = harness.projection.searches[0]
    assert "skill_id not in" in search["filter"]
    assert "owner_user_id" in search["filter"]
    assert search["private_skill_ids"] == [skill.id]
    assert search["limit"] == 20
    assert search["vector"] == [1.0, 0.0, 0.0]
    assert harness.embedder.query_calls == ["pgvector"]
    assert [item.scope for item in outcome.hits] == ["private"]
    [loaded] = outcome.hits
    assert isinstance(loaded, SkillSearchHit)
    assert loaded.version_id == version.id
    assert loaded.release_id is None
    assert loaded.skill.name == "pdf-tools"
    assert "pgvector 多租户排查" in loaded.body
    assert "description:" not in loaded.body
    assert outcome.candidates == 1
    assert outcome.warnings == ()


# ---------------------------------------------------------------------------
# Task 21: the third stage reorders confirmed, deduped candidates by body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_reorders_confirmed_candidates_using_bodies(harness: Harness) -> None:
    alpha, alpha_version = await save_private(
        harness, owner='u1', name='alpha', body='# Alpha pgvector 建索引', seq=1
    )
    beta, beta_version = await save_private(
        harness, owner='u1', name='beta', body='# Beta pgvector 慢查询排查', seq=2
    )
    harness.projection.hits = [
        hit(scope='private', owner='u1', skill_id=alpha.id, version_id=alpha_version.id, score=0.9),
        hit(scope='private', owner='u1', skill_id=beta.id, version_id=beta_version.id, score=0.5),
    ]
    harness.reranker = FakeReranker()
    # 向量把 alpha 排在前面，精排认为 beta 更相关。
    harness.reranker.order = [1, 0]

    outcome = await call(harness)

    assert [item.skill.name for item in outcome.hits][0] == 'beta'
    assert outcome.reranked is True
    assert 'rerank_unavailable' not in outcome.warnings
    # 精排读到的是正文，不是元数据。
    documents = harness.reranker.calls[0]
    assert len(documents) == 2
    assert any('慢查询排查' in document for document in documents)
    assert harness.reranker.queries == ['pgvector']


@pytest.mark.asyncio
async def test_rerank_never_reorders_a_private_copy_below_the_public_one(
    harness: Harness,
) -> None:
    """去重发生在精排之前：公共副本根本不该出现在精排的文档里。"""

    skill, version = await save_private(
        harness, owner='u1', name='pdf-tools', body='PRIVATE-BODY-21', seq=1
    )
    await publish(harness, owner='u1', skill=skill, version=version)
    # 第二个技能保证去重后仍有 2 个候选，精排才会真的被调用。
    other, other_version = await save_private(
        harness, owner='u1', name='other', body='OTHER-BODY-21', seq=2
    )
    harness.projection.hits = [
        hit(scope='public', owner=None, skill_id=skill.id, version_id=version.id, score=0.9),
        hit(scope='private', owner='u1', skill_id=skill.id, version_id=version.id, score=0.5),
        hit(scope='private', owner='u1', skill_id=other.id, version_id=other_version.id, score=0.4),
    ]
    harness.reranker = FakeReranker()
    # 精排更偏好另一个技能：这不该让作者的公共副本冒出来。
    harness.reranker.order = [1, 0]

    outcome = await call(harness)

    assert {item.scope for item in outcome.hits} == {'private'}
    documents = harness.reranker.calls[0]
    assert len(documents) == 2
    assert any('PRIVATE-BODY-21' in document for document in documents)
    # 公共副本的正文没有被送进精排，也就不可能被它排到前面。
    assert not any('Public body' in document for document in documents)


@pytest.mark.asyncio
async def test_rerank_failure_falls_back_to_the_fused_order(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    alpha, alpha_version = await save_private(
        harness, owner='u1', name='alpha', seq=1
    )
    beta, beta_version = await save_private(
        harness, owner='u1', name='beta', seq=2
    )
    harness.projection.hits = [
        hit(scope='private', owner='u1', skill_id=alpha.id, version_id=alpha_version.id, score=0.9),
        hit(scope='private', owner='u1', skill_id=beta.id, version_id=beta_version.id, score=0.5),
    ]
    harness.reranker = FakeReranker()
    harness.reranker.fail = True

    outcome = await call(harness)

    # 顺序回到融合/向量顺序；精排失败绝不能变成“没有结果”。
    assert [item.skill.name for item in outcome.hits] == ['alpha', 'beta']
    assert outcome.reranked is False
    assert 'rerank_unavailable' in outcome.warnings


@pytest.mark.asyncio
async def test_rerank_documents_are_clipped_to_max_document_chars(
    harness: Harness,
) -> None:
    long_body = '# 步骤 ' + '很长的正文 ' * 120
    skill, version = await save_private(
        harness,
        owner='u1',
        name='alpha',
        description='换流站巡检',
        body=long_body,
        seq=1,
    )
    other, other_version = await save_private(harness, owner='u1', name='beta', seq=2)
    harness.projection.hits = [
        hit(scope='private', owner='u1', skill_id=skill.id, version_id=version.id, score=0.9),
        hit(scope='private', owner='u1', skill_id=other.id, version_id=other_version.id, score=0.2),
    ]
    harness.reranker = FakeReranker()
    # 200 是这个字段允许的最小值：不要再往小调，那是配置契约。
    config = search_config(rerank=SkillRerankStageConfig(max_document_chars=200))

    await call(harness, config=config)

    [documents] = harness.reranker.calls
    assert all(len(document) <= 200 for document in documents)
    # 元数据在头部，必须留下：正文再长也不能把“这是什么技能”挤掉。
    assert documents[0].startswith('alpha')
    assert '换流站巡检' in documents[0]


@pytest.mark.asyncio
async def test_other_users_private_hits_are_rejected(harness: Harness) -> None:
    skill, _version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.projection.hits = [
        hit(scope="private", owner="u2", skill_id=skill.id, version_id="v2", score=0.9)
    ]

    outcome = await call(harness)

    # 向量命中被 PG 确认阶段丢弃；词法通道只读调用方自己的可见正文，
    # 所以这里一次 u2 的正文都没读，也没产生命中。
    assert outcome.hits == ()
    assert outcome.candidates == 0
    assert not [read for read in harness.content.reads if "u2" in read[1]]


@pytest.mark.asyncio
async def test_a_stale_active_version_is_dropped(harness: Harness) -> None:
    skill, _version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.projection.hits = [
        hit(scope="private", owner="u1", skill_id=skill.id, version_id="stale", score=0.9)
    ]

    outcome = await call(harness)

    assert outcome.hits == ()
    # 只读当前 active 版本的正文（BM25 通道），不会去读被替换掉的那个 commit。
    stale_reads = [read for read in harness.content.reads if read[0] == 'stale']
    assert stale_reads == []


@pytest.mark.asyncio
async def test_public_hits_are_visible_to_another_user(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    release = await publish(
        harness, owner="u1", skill=skill, version=version, public_name="pdf-public"
    )
    harness.projection.hits = [
        hit(scope="public", owner=None, skill_id=skill.id, version_id=version.id, score=0.9)
    ]

    outcome = await call(harness, user_id="u2")

    assert harness.projection.searches[0]["private_skill_ids"] == []
    assert "skill_id not in" not in harness.projection.searches[0]["filter"]
    [loaded] = outcome.hits
    assert loaded.scope == "public"
    assert loaded.release_id == release.id
    assert loaded.skill.name == "pdf-public"
    assert loaded.body.strip() == "# Public body"


# ---------------------------------------------------------------------------
# Task 22: BM25 is a parallel recall channel, fused with the vector channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bm25_recall_adds_candidates_the_vector_search_missed(
    harness: Harness,
) -> None:
    """报错原文只出现在正文里：向量没召回，BM25 也必须把它带回来。"""

    lexical_only, lexical_version = await save_private(
        harness,
        owner='u1',
        name='lexical-only',
        description='与查询无语义相似度',
        body='排查 pgvector 的 HNSW 参数',
        seq=1,
    )
    dense_only, dense_version = await save_private(
        harness, owner='u1', name='dense-only', description='向量召回', seq=2
    )
    harness.projection.hits = [
        hit(scope='private', owner='u1', skill_id=dense_only.id, version_id=dense_version.id, score=0.9),
    ]

    outcome = await call(harness)

    names = [item.skill.name for item in outcome.hits]
    assert names[0] == 'dense-only' or 'lexical-only' in names
    assert 'lexical-only' in names
    assert outcome.channels == ('embedding', 'keyword')


@pytest.mark.asyncio
async def test_min_score_applies_to_the_normalised_fused_score(
    harness: Harness,
) -> None:
    alpha, alpha_version = await save_private(
        harness, owner='u1', name='alpha', body='pgvector 建索引', seq=1
    )
    # beta 只在向量通道出现（正文不含查询词），融合分数因此明显低于 alpha。
    beta, beta_version = await save_private(
        harness, owner='u1', name='beta', body='完全无关的正文', seq=2
    )
    harness.projection.hits = [
        hit(scope='private', owner='u1', skill_id=alpha.id, version_id=alpha_version.id, score=0.9),
        hit(scope='private', owner='u1', skill_id=beta.id, version_id=beta_version.id, score=0.4),
    ]
    config = search_config(min_score=0.9)

    outcome = await call(harness, config=config)

    # 融合分数归一化到最高分 1.0，min_score 仍然按 0..1 的直觉生效。
    assert [item.skill.name for item in outcome.hits] == ['alpha']
    assert outcome.hits[0].score == 1.0


@pytest.mark.asyncio
async def test_no_channel_available_returns_no_hits_with_reasons(
    harness: Harness,
) -> None:
    await save_private(harness, owner='u1', name='alpha', seq=1)
    harness.embedder.fail = True
    config = search_config(keyword=SkillKeywordChannelConfig(enabled=False))

    outcome = await call(harness, config=config)

    assert outcome.hits == ()
    assert outcome.candidates == 0
    assert 'vector_unavailable' in outcome.warnings
    assert outcome.channels == ()


@pytest.mark.asyncio
async def test_each_body_is_clipped_to_max_body_chars(harness: Harness) -> None:
    skill, version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# 长正文\n" + "步骤 " * 400
    )
    harness.projection.hits = [
        hit(scope="private", owner="u1", skill_id=skill.id, version_id=version.id)
    ]

    outcome = await call(harness, config=search_config(max_body_chars=200))

    [loaded] = outcome.hits
    assert len(loaded.body) <= 200
    assert loaded.body.endswith("完整内容请 read_file 读 path）")


@pytest.mark.asyncio
async def test_the_total_body_budget_stops_loading_more_hits(harness: Harness) -> None:
    first, first_version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# A\n" + "步骤 " * 400, seq=1
    )
    second, second_version = await save_private(
        harness, owner="u1", name="csv-tools", body="# B\n" + "步骤 " * 400, seq=2
    )
    harness.projection.hits = [
        hit(scope="private", owner="u1", skill_id=first.id, version_id=first_version.id, score=0.9),
        hit(scope="private", owner="u1", skill_id=second.id, version_id=second_version.id, score=0.8),
    ]

    outcome = await call(
        harness, config=search_config(max_body_chars=400, max_context_chars=250)
    )

    assert len(outcome.hits) == 1
    assert len(outcome.hits[0].body) <= 250
    assert outcome.candidates == 2


@pytest.mark.asyncio
async def test_a_missing_private_body_warns_and_never_falls_back_to_public(
    harness: Harness,
) -> None:
    skill, version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# Private body\n"
    )
    release = await publish(harness, owner="u1", skill=skill, version=version)
    harness.content.texts.pop((version.draft_commit, version.skill_path))
    harness.projection.hits = [
        hit(scope="private", owner="u1", skill_id=skill.id, version_id=version.id, score=0.9),
        hit(scope="public", owner=None, skill_id=skill.id, version_id=version.id, score=0.8),
    ]

    outcome = await call(harness)

    assert outcome.hits == ()
    assert outcome.candidates == 1
    assert outcome.warnings == ("content_unavailable",)
    assert (PUBLIC_COMMIT, release.skill_path) not in harness.content.reads


@pytest.mark.asyncio
async def test_milvus_failure_falls_back_to_local_lexical_scoring(harness: Harness) -> None:
    skill, _version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# 排查\npgvector 多租户 search_path 串库\n"
    )
    harness.projection.fail = True

    outcome = await call(harness, domain_keywords=["pgvector"])

    assert len(harness.projection.searches) == 1
    assert harness.embedder.query_calls == ["pgvector"]
    assert outcome.warnings == ("vector_unavailable", "lexical_fallback")
    [loaded] = outcome.hits
    assert loaded.scope == "private"
    assert loaded.skill.skill_id == skill.id
    assert "search_path" in loaded.body
    assert loaded.score > 0


@pytest.mark.asyncio
async def test_embedding_failure_skips_the_vector_search(harness: Harness) -> None:
    _skill, _version = await save_private(
        harness, owner="u1", name="pdf-tools", body="# 排查\npgvector 多租户\n"
    )
    harness.embedder.fail = True

    outcome = await call(harness)

    assert harness.projection.searches == []
    assert harness.embedder.query_calls == ["pgvector"]
    assert outcome.warnings == ("vector_unavailable", "lexical_fallback")
    assert [item.scope for item in outcome.hits] == ["private"]


@pytest.mark.asyncio
async def test_disabled_embedding_channel_uses_lexical_scoring(harness: Harness) -> None:
    await save_private(harness, owner="u1", name="pdf-tools", body="# 排查\npgvector\n")
    keyword_only = SkillSearchConfig()

    outcome = await call(harness, config=keyword_only)

    assert harness.projection.searches == []
    assert outcome.warnings == ("vector_unavailable", "lexical_fallback")
    assert [item.skill.name for item in outcome.hits] == ["pdf-tools"]


@pytest.mark.asyncio
async def test_preferences_and_available_skills_filter_hits(harness: Harness) -> None:
    skill, version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.projection.hits = [
        hit(scope="private", owner="u1", skill_id=skill.id, version_id=version.id)
    ]

    disabled = await call(harness, prefs={skill.id: False})
    assert disabled.hits == ()
    assert disabled.candidates == 1

    denied = await call(harness, available_skills=frozenset({"other"}))
    assert denied.hits == ()

    allowed = await call(harness, available_skills=frozenset({"pdf-tools"}))
    assert [item.skill.skill_id for item in allowed.hits] == [skill.id]


@pytest.mark.asyncio
async def test_a_missing_catalog_fails_closed_without_unconfirmed_hits(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skill, _version = await save_private(harness, owner="u1", name="pdf-tools")
    harness.projection.hits = [
        hit(scope="private", owner="u1", skill_id="whatever", version_id="v1", score=0.9)
    ]

    async def unavailable(user_id: str, hits: list[ProjectionHit]) -> list[Any]:
        raise RuntimeError("PostgreSQL is unavailable")

    monkeypatch.setattr(harness.store, "confirm_hits", unavailable)

    with pytest.raises(SkillSearchUnavailable):
        await call(harness)
    assert harness.content.reads == []


@pytest.mark.asyncio
async def test_an_empty_query_returns_no_hits(harness: Harness) -> None:
    await save_private(harness, owner="u1", name="pdf-tools")

    outcome = await call(harness, task_query="   ")

    assert outcome == SkillSearchOutcome(hits=(), candidates=0, warnings=())
    assert harness.projection.searches == []
    assert harness.embedder.query_calls == []
