"""M7 §3.5：技能检索的两段式 —— BM25 召回 → 融合 → 可选精排。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agentkit.config.skills_config import SkillKeywordChannelConfig, SkillSearchConfig
from agentkit.skills.router import (
    build_documents,
    lexical_scores,
    merge_rankings,
    search_skills,
    tokenize,
)
from agentkit.skills.storage import load_skills
from fakes.skill_tree import skills_config, write_skill

MULTITENANT = "pg-multitenant"
HNSW = "pgvector-tuning"
CSV = "csv-tools"


def _tree(tmp_path) -> list:
    write_skill(
        tmp_path,
        "public",
        MULTITENANT,
        description="PostgreSQL 连接池与租户隔离排障",
        body="# 串库排查\n多租户场景下 search_path 被连接池复用，先查 pg_stat_activity。\n",
    )
    write_skill(
        tmp_path,
        "public",
        HNSW,
        description="pgvector HNSW 向量索引调优",
        body="# 调优\nef_search 与 m 参数一起改，先看 recall 再看延迟。\n",
    )
    write_skill(
        tmp_path,
        "public",
        CSV,
        description="CSV 导出与编码转换",
        body="# 导出\nExcel 打开 CSV 乱码时转 GBK。\n",
    )
    return load_skills(skills_config(tmp_path))


def _search(tmp_path, *, task_query: str, keywords=(), embedder=None, reranker=None, **overrides):
    skills = _tree(tmp_path)
    config = SkillSearchConfig(**{"max_body_chars": 4000, **overrides})
    return asyncio.run(
        search_skills(
            skills,
            task_query=task_query,
            domain_keywords=list(keywords),
            config=config,
            embedder=embedder,
            reranker=reranker,
        )
    )


def test_tokenizer_keeps_ascii_words_and_cuts_cjk_into_bigrams():
    assert tokenize("排查 PostgreSQL search_path 多租户") == [
        "postgresql",
        "search_path",
        "排查",
        "多租",
        "租户",
    ]


def test_bm25_puts_the_body_match_first(tmp_path):
    outcome = _search(tmp_path, task_query="多租户 search_path 连接池串库")

    assert outcome.hits
    assert outcome.hits[0].skill.name == MULTITENANT
    assert outcome.channels == ("keyword",)
    assert "keyword" in outcome.hits[0].channels


def test_domain_keywords_bring_in_and_boost_skills(tmp_path):
    """关键词是**额外**信号：查询词没提到它，也能把候选拉进来，并加权原有命中。"""
    documents = build_documents(_tree(tmp_path))
    config = SkillKeywordChannelConfig()
    keys = {document.skill.name: document.key for document in documents}

    plain = lexical_scores(
        documents, task_query="多租户 search_path", domain_keywords=[], config=config
    )
    boosted = lexical_scores(
        documents, task_query="多租户 search_path", domain_keywords=["ef_search"], config=config
    )

    assert keys[HNSW] not in plain  # 查询词一个字都没出现在这个技能里
    assert boosted[keys[HNSW]] > 0  # 关键词把它拉进来了

    reinforced = lexical_scores(
        documents, task_query="多租户 search_path", domain_keywords=["pg_stat_activity"], config=config
    )
    assert reinforced[keys[MULTITENANT]] > plain[keys[MULTITENANT]]  # 原有命中也吃到关键词的权重


def test_a_name_hit_beats_an_incidental_body_mention(tmp_path):
    outcome = _search(tmp_path, task_query="pgvector")

    assert outcome.hits[0].skill.name == HNSW


def test_top_k_cuts_the_loaded_set(tmp_path):
    outcome = _search(tmp_path, task_query="PostgreSQL CSV pgvector 索引", top_k=1)

    assert len(outcome.hits) == 1
    assert outcome.candidates >= 1


def test_min_score_is_a_normalized_cut(tmp_path):
    """只有一个通道时，排第一的归一化分数是 1.0，其余都小于它。"""

    best = _search(tmp_path, task_query="多租户 search_path 连接池", min_score=1.0)
    none = _search(tmp_path, task_query="多租户 search_path 连接池", min_score=0.99)

    assert [hit.skill.name for hit in best.hits] == [MULTITENANT]
    # 0.99 只是把归一化前的第二名也放进来，不会凭空造出更多命中
    assert len(none.hits) >= len(best.hits)


def test_merge_rankings_normalizes_the_perfect_hit_to_one():
    fused, channels = merge_rankings([["a", "b"], ["a"]], rrf_k=60)

    assert fused["a"] == 1.0
    assert fused["b"] < 1.0
    assert channels["a"] == ("keyword", "embedding")


def test_embedding_channel_alone_can_drive_the_order(tmp_path):
    skills = _tree(tmp_path)
    config = SkillSearchConfig(
        keyword=SkillKeywordChannelConfig(enabled=False),
        embedding={"enabled": True, "base_url": "http://embedding.test"},
    )

    outcome = asyncio.run(
        search_skills(
            skills,
            task_query="随便一句话",
            config=config,
            embedder=_FakeEmbedder(favour="HNSW"),
        )
    )

    assert outcome.channels == ("embedding",)
    assert outcome.hits[0].skill.name == HNSW


def test_embedding_failure_falls_back_to_bm25(tmp_path):
    skills = _tree(tmp_path)
    config = SkillSearchConfig(embedding={"enabled": True, "base_url": "http://embedding.test"})

    outcome = asyncio.run(
        search_skills(
            skills,
            task_query="多租户 search_path",
            config=config,
            embedder=_BrokenEmbedder(),
        )
    )

    assert outcome.warnings == ("embedding_unavailable",)
    assert outcome.hits[0].skill.name == MULTITENANT


def test_rerank_reorders_the_candidates(tmp_path):
    skills = _tree(tmp_path)
    config = SkillSearchConfig(candidate_k=3, top_k=3)

    baseline = asyncio.run(
        search_skills(skills, task_query="多租户 search_path 连接池", config=config)
    )
    reranked = asyncio.run(
        search_skills(
            skills,
            task_query="多租户 search_path 连接池",
            config=config,
            reranker=_ReversingReranker(),
        )
    )

    assert reranked.reranked is True
    assert [hit.skill.name for hit in reranked.hits] == list(
        reversed([hit.skill.name for hit in baseline.hits])
    )


def test_rerank_failure_keeps_the_fused_order(tmp_path):
    skills = _tree(tmp_path)
    config = SkillSearchConfig()

    baseline = asyncio.run(
        search_skills(skills, task_query="多租户 search_path 连接池", config=config)
    )
    degraded = asyncio.run(
        search_skills(
            skills,
            task_query="多租户 search_path 连接池",
            config=config,
            reranker=_BrokenReranker(),
        )
    )

    assert degraded.warnings == ("rerank_failed",)
    assert [hit.skill.name for hit in degraded.hits] == [
        hit.skill.name for hit in baseline.hits
    ]


def test_a_skill_whose_file_vanished_is_skipped(tmp_path):
    skills = _tree(tmp_path)
    victim = next(skill for skill in skills if skill.name == CSV)
    victim.skill_file.unlink()

    documents = build_documents(skills)

    assert {document.skill.name for document in documents} == {MULTITENANT, HNSW}


def test_lexical_scores_are_empty_without_query_terms(tmp_path):
    skills = _tree(tmp_path)

    assert (
        lexical_scores(
            build_documents(skills),
            task_query="",
            domain_keywords=[],
            config=SkillKeywordChannelConfig(),
        )
        == {}
    )


class _FakeEmbedder:
    """正文里带 `favour` 这个词的技能拿到 [1, 0]，其余 [0, 1]；查询也投 [1, 0]。"""

    def __init__(self, *, favour: str) -> None:
        self._favour = favour

    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]

    async def embed_documents(self, texts) -> list[list[float]]:
        return [[1.0, 0.0] if self._favour in text else [0.0, 1.0] for text in texts]


class _BrokenEmbedder:
    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("embedding service is down")

    async def embed_documents(self, texts) -> list[list[float]]:
        raise RuntimeError("embedding service is down")


class _ReversingReranker:
    async def rerank(self, *, query: str, documents):
        del query
        return [SimpleNamespace(index=index) for index in reversed(range(len(list(documents))))]


class _BrokenReranker:
    async def rerank(self, *, query: str, documents):
        raise RuntimeError("rerank service is down")
