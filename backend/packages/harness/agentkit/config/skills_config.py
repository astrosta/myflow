"""技能目录、索引渲染参数与检索加载配置（M7 §3.1/§3.5）。

**没有模式开关**：索引（L0）是唯一形态，正文一律按需（§3.5）。旧设计的
deferred_discovery 被刻意删掉了——两套渲染等于两套提示词契约与两套测试。

`search` 是**按需加载的第二条路径**：技能多到一个都读不完时，模型用
`search_and_load_skills` 按任务意图检索，只把最相关的那几个技能的正文拉进来。
默认只有本地 BM25 通道（不依赖任何外部服务）；语义召回与精排要显式配置地址才开，
和知识库一样——代码里没有默认端点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentkit.config.reload_boundary import format_field_description
from agentkit.config.runtime_paths import resolve_path
from agentkit.knowledge.config import EmbeddingConfig, RerankConfig

DEFAULT_SKILLS_PATH = "skills"
DEFAULT_SKILLS_CONTAINER_PATH = "/mnt/skills"
DEFAULT_INDEX_DESCRIPTION_MAX_CHARS = 120
DEFAULT_SEARCH_CANDIDATE_K = 20
DEFAULT_SEARCH_TOP_K = 3


class SkillKeywordChannelConfig(BaseModel):
    """本地稀疏通道：BM25，分词在进程内做（中文按二元组）。**不需要任何外部服务。**"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    bm25_k1: float = Field(default=1.2, gt=0, le=10, description="BM25 词频饱和参数。")
    bm25_b: float = Field(default=0.75, ge=0, le=1, description="BM25 长度归一参数。")
    keyword_weight: float = Field(
        default=2.0,
        gt=0,
        le=10,
        description="domain_keywords 里每个词的分数倍数。它是模型给的强信号，比正文里偶然出现一次的词重。",
    )
    name_weight: float = Field(
        default=3.0,
        gt=0,
        le=10,
        description="技能名/描述里命中词的权重倍数。元数据是作者手写的摘要，比正文噪声低。",
    )


class SkillEmbeddingChannelConfig(EmbeddingConfig):
    """可选的语义召回（bi-encoder）。**默认关**：没配地址就不发请求，直接走 BM25。"""

    enabled: bool = False
    query_instruction: str = (
        "Given a task description, retrieve the skill whose operating procedure applies"
    )


class SkillRerankStageConfig(RerankConfig):
    """可选的精排（cross-encoder）。失败一律回退融合顺序，不影响"能不能加载"。"""


class SkillSearchConfig(BaseModel):
    """`search_and_load_skills` 的两段式检索：召回 candidate_k → 精排 top_k。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="关掉后不注册 search_and_load_skills：技能只能靠索引 + read_file 逐个读。",
    )
    candidate_k: int = Field(
        default=DEFAULT_SEARCH_CANDIDATE_K,
        ge=1,
        le=200,
        description="召回阶段交给精排的候选数。",
    )
    top_k: int = Field(
        default=DEFAULT_SEARCH_TOP_K,
        ge=1,
        le=20,
        description="最终加载几个技能的正文。每多一个，就是一段常驻上下文。",
    )
    rrf_k: int = Field(default=60, ge=1, le=1000, description="两路召回融合的 RRF k。")
    min_score: float | None = Field(
        default=None,
        description="融合分数的下限；给定时低于它的命中直接丢弃（宁可空手也不塞不相关的正文）。",
    )
    max_body_chars: int = Field(
        default=6000, ge=200, le=100000, description="单个技能正文的字符预算，超了从尾部截断。"
    )
    max_context_chars: int = Field(
        default=12000, ge=200, le=200000, description="一次加载返回的正文总预算。"
    )
    keyword: SkillKeywordChannelConfig = Field(default_factory=SkillKeywordChannelConfig)
    embedding: SkillEmbeddingChannelConfig = Field(default_factory=SkillEmbeddingChannelConfig)
    rerank: SkillRerankStageConfig = Field(default_factory=SkillRerankStageConfig)

    @model_validator(mode="after")
    def _stages_are_coherent(self) -> Self:
        if self.candidate_k < self.top_k:
            raise ValueError("skills.search.candidate_k must be >= skills.search.top_k")
        if self.embedding.enabled and not self.embedding.base_url.strip():
            raise ValueError(
                "skills.search.embedding.enabled=true requires skills.search.embedding.base_url"
            )
        if self.rerank.enabled:
            if not self.rerank.base_url.strip():
                raise ValueError(
                    "skills.search.rerank.enabled=true requires skills.search.rerank.base_url"
                )
            if not self.rerank.model.strip():
                raise ValueError(
                    "skills.search.rerank.enabled=true requires skills.search.rerank.model"
                )
        if not self.keyword.enabled and not self.embedding.enabled:
            # 两路都关就只剩空召回：那是配置错误，不是"省一次请求"
            raise ValueError(
                "skills.search needs at least one recall channel: "
                "enable skills.search.keyword or skills.search.embedding"
            )
        return self

    def recall_channels(self) -> tuple[str, ...]:
        """实际会跑的召回通道，用于工具返回里告诉模型"这次是怎么找到的"。"""
        channels: list[str] = []
        if self.keyword.enabled:
            channels.append("keyword")
        if self.embedding.enabled and self.embedding.base_url.strip():
            channels.append("embedding")
        return tuple(channels)


class SkillActiveContextConfig(BaseModel):
    """`<active_skills>` 块：已激活技能的正文要不要跟着进持久上下文（M7 §3.10）。

    正文**不进 state**（checkpoint 会膨胀），这里是**每次投影时从技能树现读**：
    `skill_context` 台账只记 name/path/description，正文按这里的预算现取现用。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="关掉后 <active_skills> 只留名字、描述和目录；正文仍靠消息里的工具结果。",
    )
    max_body_chars: int = Field(
        default=4000,
        ge=200,
        le=100000,
        description="单个技能正文在 <active_skills> 里的字符预算。",
    )
    max_context_chars: int = Field(
        default=8000,
        ge=200,
        le=200000,
        description="<active_skills> 整块的正文总预算。",
    )
    skip_bodies_still_in_context: bool = Field(
        default=True,
        description=(
            "正文还在本轮消息里（工具结果的 JSON / read_file 的结果）就不再重复写一遍。"
            "关掉就每轮都整段重放：上下文被压缩之后仍在，但平时是双份 token。"
        ),
    )


class SkillsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        default=DEFAULT_SKILLS_PATH,
        description="技能树根目录。相对路径按项目根解析；其下是 public/ 与 custom/。",
    )
    container_path: str = Field(
        default=DEFAULT_SKILLS_CONTAINER_PATH,
        description=format_field_description("skills.container_path"),
    )
    index_description_max_chars: int = Field(
        default=DEFAULT_INDEX_DESCRIPTION_MAX_CHARS,
        ge=1,
        description="L0 索引里每行描述截断到多少字符。索引必须有界，否则每加一个技能就重写一遍系统提示词。",
    )
    search: SkillSearchConfig = Field(default_factory=SkillSearchConfig)
    active_context: SkillActiveContextConfig = Field(default_factory=SkillActiveContextConfig)

    def root_dir(self) -> Path:
        """宿主上的技能树根目录（用于扫描）。"""
        return resolve_path(self.path)
