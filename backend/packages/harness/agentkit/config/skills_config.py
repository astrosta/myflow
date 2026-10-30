"""技能目录、索引渲染参数与检索加载配置（M7 §3.1/§3.5）。

**没有模式开关**：索引（L0）是唯一形态，正文一律按需（§3.5）。旧设计的
deferred_discovery 被刻意删掉了——两套渲染等于两套提示词契约与两套测试。

`search` 是**按需加载的第二条路径**：技能多到一个都读不完时，模型用
`search_and_load_skills` 按任务意图检索，只把最相关的那几个技能的正文拉进来。
默认只有本地 BM25 通道（不依赖任何外部服务）；语义召回与精排要显式配置地址才开，
和知识库一样——代码里没有默认端点。
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentkit.config.reload_boundary import format_field_description
from agentkit.config.runtime_paths import resolve_path
from agentkit.knowledge.config import EmbeddingConfig, MilvusConfig, RerankConfig
from agentkit.skills.projection import ACTIVE_COLLECTION

DEFAULT_SKILLS_PATH = "skills"
DEFAULT_BUILTIN_SKILLS_PATH = "skills/builtin"
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


class SkillVectorStoreConfig(MilvusConfig):
    """skills active projection 的 Milvus 连接。

    投影是**当前激活状态**的物化视图，和知识库不是同一张表：collection 默认
    myflow_skills_active，**不继承** myflow_knowledge。uri 留空时由
    build_skill_runtime 借用 knowledge_base.vector_store 的连接参数（uri/token/
    db_name），collection 仍然强制为这张表——那是检索契约，不是部署参数。
    """

    collection: str = Field(
        default=ACTIVE_COLLECTION,
        description=(
            "active projection 的 collection 名（固定 myflow_skills_active）。"
            "投影只放当前激活的技能行，绝不能和知识库共用 collection。"
        ),
    )

    @field_validator("collection")
    @classmethod
    def _pin_the_active_collection(cls, value: str) -> str:
        if value != ACTIVE_COLLECTION:
            raise ValueError(
                f"skills.search.vector_store.collection must be {ACTIVE_COLLECTION!r}: "
                "the active projection owns its own collection and never reuses the "
                "knowledge base's."
            )
        return ACTIVE_COLLECTION


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
    vector_store: SkillVectorStoreConfig = Field(default_factory=SkillVectorStoreConfig)

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

    def active_vector_store(
        self, knowledge_vector_store: MilvusConfig
    ) -> SkillVectorStoreConfig:
        """投影实际使用的 Milvus 连接。

        技能自己配了 uri 就用它；没配就退回知识库的连接参数，让只部署了一套
        Milvus 的节点不必重复填地址。collection 例外：它永远是
        myflow_skills_active，绝不跟着知识库的 myflow_knowledge 走。
        """
        if self.vector_store.uri.strip():
            return self.vector_store
        # 只借连接参数：metric_type / index_type / consistency_level / flush /
        # timeout_seconds 是调优项，重新构造一份默认配置会把它们悄悄丢掉。
        return self.vector_store.model_copy(
            update={
                "uri": knowledge_vector_store.uri,
                "token": knowledge_vector_store.token,
                "db_name": knowledge_vector_store.db_name,
                "collection": ACTIVE_COLLECTION,
            }
        )

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


class SkillCreationConfig(BaseModel):
    """普通用户创作私有技能的配额与包边界。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="关闭后只有 admin 能创建技能。")
    max_drafts_per_user: int = Field(default=20, ge=1, description="每个用户最多保存多少个私有草稿。")
    max_pending_submissions_per_user: int = Field(
        default=5, ge=1, description="每个用户同时最多有多少个待审核提交。"
    )
    max_package_bytes: int = Field(
        default=5_242_880, ge=1, description="单个技能包（所有文件之和）的字节上限。"
    )
    max_file_bytes: int = Field(default=1_048_576, ge=1, description="单个技能文件的字节上限。")
    max_files: int = Field(default=200, ge=1, description="单个技能包允许的最大文件数。")


class SkillGitConfig(BaseModel):
    """服务端读写技能仓库的参数。普通用户永远拿不到这里的凭据。"""

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(default="", description="技能仓库 URL；代码里没有默认服务地址。")
    public_branch: str = Field(default="main", min_length=1, description="公共技能所在分支。")
    draft_ref_prefix: str = Field(
        default="refs/heads/myflow/drafts",
        min_length=1,
        description="每个用户一条草稿分支的 ref 前缀。",
    )
    cache_dir: str = Field(
        default=".myflow/skills",
        min_length=1,
        description="节点本地的 Git mirror 与 commit 内容缓存目录。",
    )
    ssh_key: str = Field(
        default="",
        repr=False,
        description="base64 编码的 SSH 私钥内容；空表示不使用密钥认证。",
    )
    command_timeout_seconds: int = Field(
        default=120, ge=1, description="单次 libgit2 网络操作的超时时间。"
    )

    @field_validator("ssh_key")
    @classmethod
    def _valid_ssh_key(cls, value: str) -> str:
        compact = "".join(value.split())
        if not compact:
            return ""
        try:
            decoded = base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("skills.git.ssh_key must be base64-encoded") from exc
        if b"PRIVATE KEY" not in decoded:
            raise ValueError("skills.git.ssh_key must decode to a private key")
        return compact


class SkillReviewConfig(BaseModel):
    """公共发布的人工审核规则。"""

    model_config = ConfigDict(extra="forbid")

    require_admin: bool = Field(default=True, description="公共发布必须经过 admin 审核。")
    allow_self_approval: bool = Field(default=True, description="是否允许 admin 批准自己的提交。")


class SkillIndexConfig(BaseModel):
    """Milvus active projection 的 outbox 与对账节奏。"""

    model_config = ConfigDict(extra="forbid")

    outbox_poll_seconds: float = Field(default=2.0, gt=0, description="outbox worker 轮询间隔。")
    catalog_poll_seconds: float = Field(
        default=5.0,
        ge=0,
        description="跨 worker catalog epoch 轮询间隔；0 = 关闭轮询（单 worker 部署）。",
    )
    reconciliation_interval_minutes: int = Field(
        default=10, ge=1, description="active projection 对账任务的运行间隔。"
    )


class SkillRetentionConfig(BaseModel):
    """可重建缓存的保留策略；元数据和公共历史永久保留。"""

    model_config = ConfigDict(extra="forbid")

    keep_versions_per_skill: int = Field(
        default=20, ge=1, description="每个技能保留多少个最近版本的 commit 内容缓存。"
    )
    cache_ttl_days: int = Field(default=30, ge=1, description="未被引用的缓存最长保留天数。")


class BuiltinSkillsConfig(BaseModel):
    """随项目发布的内置技能目录。只读，不写 Git，也不写数据库。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description=format_field_description(
            "skills.builtin.enabled",
            field_doc="关掉后不扫描、不索引、不挂载内置技能。",
        ),
    )
    path: str = Field(
        default=DEFAULT_BUILTIN_SKILLS_PATH,
        description=format_field_description(
            "skills.builtin.path",
            field_doc="内置技能根目录；相对路径按项目根解析。",
        ),
    )

    @field_validator("path")
    @classmethod
    def _reject_unsafe_builtin_root(cls, value: str) -> str:
        """内置根会成为只读挂载源，绝不能是项目根/文件系统根或含 .. 的逃逸路径。"""
        if not value or not value.strip():
            raise ValueError("skills.builtin.path must not be blank")
        path = Path(value)
        if any(part == ".." for part in path.parts):
            raise ValueError("skills.builtin.path must not contain '..'")
        if path.parent == path:
            # 包括 POSIX "/"、Windows 盘符根（C:\）和 UNC share 根。
            raise ValueError("skills.builtin.path must not be a filesystem root")
        return value

    def root_dir(self) -> Path:
        """宿主上的内置技能根目录（仅解析；安全校验见 skills.builtin.resolve_builtin_root）。"""
        return resolve_path(self.path)


class SkillsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["filesystem", "git"] = Field(
        default="filesystem",
        description="技能目录来源。filesystem 仅用于测试/本地调试；生产使用 git。",
    )
    builtin: BuiltinSkillsConfig = Field(default_factory=BuiltinSkillsConfig)
    creation: SkillCreationConfig = Field(default_factory=SkillCreationConfig)
    git: SkillGitConfig = Field(default_factory=SkillGitConfig)
    review: SkillReviewConfig = Field(default_factory=SkillReviewConfig)
    index: SkillIndexConfig = Field(default_factory=SkillIndexConfig)
    retention: SkillRetentionConfig = Field(default_factory=SkillRetentionConfig)
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
