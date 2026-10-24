"""技能检索：search_and_load_skills 的召回 → 融合 → 精排（M7 §3.5 的第二条按需路径）。

两段式：

1. **召回**：本地 BM25（进程内分词，中文按二元组）+ 可选的 bi-encoder 向量通道，
   两份排名用 RRF 融合成 Top-`candidate_k`。**默认只有 BM25**：不依赖任何外部服务，
   同一份技能树上的排名是确定的，可以写成测试。
2. **精排**：可选 cross-encoder，对候选的"名字 + 描述 + 正文"重排成 Top-`top_k`。
   失败一律回退融合顺序并写进 `warnings`——和知识库的 rerank 同一条纪律：
   精排是提精度的，不是"能不能用"的前提。

排序之外只有三件事：正文在这里**不截断**（预算是 loader 的事）；技能读不出来就跳过
（一个坏技能不该让整次检索失败）；`domain_keywords` 里的词拿到更高权重
（那是模型明确点名的技术名词，比正文里偶然出现一次的词重得多）。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from agentkit.config.skills_config import SkillKeywordChannelConfig, SkillSearchConfig
from agentkit.skills.loader import read_skill_body
from agentkit.skills.types import Skill

logger = logging.getLogger(__name__)

#: ASCII 词（含库名/命令里的 `.` `-` `/` `_`）与中文连续段
_ASCII_TOKEN_RE = re.compile("[a-z0-9_]+(?:[.\\-/+][a-z0-9_]+)*")
_CJK_RUN_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]+")

#: 向量缓存上限。技能正文只有几 KB、技能数只有几十个，512 够用一整个进程了。
_VECTOR_CACHE_MAX = 512
_vector_cache: dict[tuple[str, int, str, int], list[float]] = {}


def _fingerprint(text: str) -> str:
    """向量缓存的键用 sha256 而不是 `hash()`：后者带进程随机盐，且碰撞了会把别的技能的向量给出去。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class Embedder(Protocol):
    """bi-encoder 只需要这两个方法（`knowledge.embedding` 的客户端就长这样）。"""

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    async def rerank(self, *, query: str, documents: Sequence[str]) -> Sequence[object]: ...


def tokenize(text: str) -> list[str]:
    """BM25 用的分词：ASCII 词原样，中文按二元组（不引入分词器依赖）。

    中文没有空格，按字切会丢掉"多租户"这种词义，按二元组切是成本最低的折中：
    "多租户" → ["多租","租户"]，查"租户"能命中，查"多租户"也能命中。
    """
    lowered = text.lower()
    tokens = _ASCII_TOKEN_RE.findall(lowered)
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """一个可检索的技能：元数据 + 正文 + 预先算好的词频。"""

    skill: Skill
    body: str
    counts: Mapping[str, int]
    length: int
    head_tokens: frozenset[str]

    @property
    def key(self) -> str:
        return self.skill.container_file


@dataclass(frozen=True, slots=True)
class SkillHit:
    skill: Skill
    body: str
    score: float
    channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    hits: tuple[SkillHit, ...]
    channels: tuple[str, ...]
    candidates: int
    reranked: bool
    warnings: tuple[str, ...]


def build_documents(skills: Sequence[Skill]) -> list[SkillDocument]:
    """读正文建索引。读不出来的技能**跳过并 warning**，不让整次检索失败。"""
    documents: list[SkillDocument] = []
    for skill in skills:
        body = _read_body(skill)
        if body is None:
            continue
        # 索引是"名字 + 描述 + 正文"：元数据也是检索对象，只是命中时额外加权（head_tokens）
        head = frozenset(tokenize(f"{skill.name} {skill.description}"))
        counts = Counter(tokenize(f"{skill.name} {skill.description}\n{body}"))
        documents.append(
            SkillDocument(
                skill=skill,
                body=body,
                counts=counts,
                length=sum(counts.values()),
                head_tokens=head,
            )
        )
    return documents


def _read_body(skill: Skill) -> str | None:
    return read_skill_body(skill)


def lexical_scores(
    documents: Sequence[SkillDocument],
    *,
    task_query: str,
    domain_keywords: Sequence[str],
    config: SkillKeywordChannelConfig,
) -> dict[str, float]:
    """BM25。查询词权重：`domain_keywords` × keyword_weight，命中元数据 × name_weight。"""
    query_terms: Counter[str] = Counter(tokenize(task_query))
    for keyword in domain_keywords:
        for token in tokenize(keyword):
            query_terms[token] += config.keyword_weight
    if not query_terms or not documents:
        return {}

    document_frequency: Counter[str] = Counter()
    for document in documents:
        for term in query_terms:
            if term in document.counts:
                document_frequency[term] += 1

    total = len(documents)
    average_length = (sum(document.length for document in documents) / total) or 1.0
    scores: dict[str, float] = {}
    for document in documents:
        score = 0.0
        for term, weight in query_terms.items():
            frequency = document.counts.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            length_ratio = document.length / average_length
            saturation = frequency * (config.bm25_k1 + 1) / (
                frequency + config.bm25_k1 * (1 - config.bm25_b + config.bm25_b * length_ratio)
            )
            boost = config.name_weight if term in document.head_tokens else 1.0
            score += weight * idf * saturation * boost
        if score > 0:
            scores[document.key] = score
    return scores


async def embedding_scores(
    documents: Sequence[SkillDocument],
    *,
    task_query: str,
    embedder: Embedder,
    config: SkillSearchConfig,
) -> dict[str, float]:
    """余弦相似度。向量按 (地址, 维度, 正文指纹) 缓存在进程内：技能不变就不重复算。"""
    if not documents:
        return {}
    query_vector = await embedder.embed_query(task_query)
    embedding = config.embedding
    texts: list[str] = []
    keys: list[tuple[str, int, str, int]] = []
    for document in documents:
        text = _document_text(document)
        keys.append((embedding.base_url, embedding.dimensions, _fingerprint(text), len(text)))
        texts.append(text)
    missing = [index for index, key in enumerate(keys) if key not in _vector_cache]
    if missing:
        vectors = await embedder.embed_documents([texts[index] for index in missing])
        for index, vector in zip(missing, vectors, strict=True):
            if len(_vector_cache) >= _VECTOR_CACHE_MAX:
                _vector_cache.clear()  # 简单胜过聪明：缓存满了就整块丢
            _vector_cache[keys[index]] = list(vector)

    scores: dict[str, float] = {}
    for document, key in zip(documents, keys, strict=True):
        similarity = _cosine(query_vector, _vector_cache[key])
        if similarity > 0:
            scores[document.key] = similarity
    return scores


def _document_text(document: SkillDocument) -> str:
    """送进 embedding/rerank 的文本：名字 + 描述 + 正文（正文是最有信息量的部分）。"""
    head = f"{document.skill.name}\n{document.skill.description}"
    return f"{head}\n\n{document.body}" if document.body else head


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def merge_rankings(
    rankings: Sequence[Sequence[str]], *, rrf_k: int
) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
    """RRF 融合。返回 (归一化到 (0,1] 的分数, 每个文档命中的通道)。

    归一化让满分 = 每个通道都排第一，`min_score` 才有可比的语义。
    """
    if not rankings:
        return {}, {}
    fused: dict[str, float] = {}
    channels: dict[str, list[str]] = {}
    names = ["keyword", "embedding"]
    for position, ordered in enumerate(rankings):
        name = names[position] if position < len(names) else f"channel{position}"
        for rank, key in enumerate(ordered, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (rrf_k + rank)
            channels.setdefault(key, []).append(name)
    ceiling = len(rankings) / (rrf_k + 1)
    return (
        {key: min(1.0, score / ceiling) for key, score in fused.items()},
        {key: tuple(names_hit) for key, names_hit in channels.items()},
    )


def _ranked(scores: Mapping[str, float]) -> list[str]:
    return [key for key, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


async def search_skills(
    skills: Sequence[Skill],
    *,
    task_query: str,
    domain_keywords: Sequence[str] = (),
    config: SkillSearchConfig,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> SearchOutcome:
    """召回 → 融合 → 精排。空召回返回空 hits（措辞交给调用方）。"""
    documents = build_documents(skills)
    channels = config.recall_channels()
    if not documents:
        return SearchOutcome(hits=(), channels=channels, candidates=0, reranked=False, warnings=())
    by_key = {document.key: document for document in documents}

    warnings: list[str] = []
    rankings: list[list[str]] = []
    lexical = lexical_scores(
        documents,
        task_query=task_query,
        domain_keywords=domain_keywords,
        config=config.keyword,
    )
    if lexical:
        rankings.append(_ranked(lexical))
    if embedder is not None:
        try:
            vectors = await embedding_scores(
                documents, task_query=task_query, embedder=embedder, config=config
            )
        except Exception as exc:  # 语义通道是加分项：挂了也要能靠 BM25 出结果
            logger.warning("Skill embedding channel failed; falling back to BM25: %s", exc)
            warnings.append("embedding_unavailable")
        else:
            if vectors:
                rankings.append(_ranked(vectors))
    if not rankings:
        return SearchOutcome(
            hits=(), channels=channels, candidates=0, reranked=False, warnings=tuple(warnings)
        )

    fused, channel_map = merge_rankings(rankings, rrf_k=config.rrf_k)
    ordered = _ranked(fused)
    if config.min_score is not None:
        ordered = [key for key in ordered if fused[key] >= config.min_score]
    candidates = ordered[: config.candidate_k]

    reranked = False
    final = candidates
    if reranker is not None and candidates:
        try:
            order = await _rerank(reranker, task_query, [by_key[key] for key in candidates])
        except Exception as exc:
            logger.warning("Skill rerank failed; keeping the fused order: %s", exc)
            warnings.append("rerank_failed")
        else:
            reranked = True
            final = [candidates[index] for index in order]

    hits = tuple(
        SkillHit(
            skill=by_key[key].skill,
            body=by_key[key].body,
            score=round(fused[key], 4),
            channels=channel_map.get(key, ()),
        )
        for key in final[: config.top_k]
    )
    return SearchOutcome(
        hits=hits,
        channels=channels,
        candidates=len(candidates),
        reranked=reranked,
        warnings=tuple(warnings),
    )


async def _rerank(
    reranker: Reranker, task_query: str, documents: Sequence[SkillDocument]
) -> list[int]:
    """把精排结果翻成下标顺序；服务漏掉的候选按原顺序补在后面（不能凭空丢技能）。"""
    results = await reranker.rerank(
        query=task_query, documents=[_document_text(document) for document in documents]
    )
    order: list[int] = []
    for item in results:
        index = getattr(item, "index", None)
        if isinstance(index, int) and 0 <= index < len(documents) and index not in order:
            order.append(index)
    return order + [index for index in range(len(documents)) if index not in order]
