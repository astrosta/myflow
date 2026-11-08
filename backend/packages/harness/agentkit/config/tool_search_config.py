"""`tool_search:` block: retrieval budgets and the pinned Milvus collection.

```yaml
tool_search:
  enabled: true
  candidate_k: 20
  top_k: 5
  max_schema_chars: 3000
  vector_store: {collection: myflow_mcp_tools}
```

`mcp_tools` is the authoritative tool catalog; the Milvus collection is a
derived retrieval index rebuilt from those rows.  The collection name is pinned
here so tool documents can never be written into the knowledge or skills
collection: a wrong collection name does not fail loudly at query time, it
quietly returns someone else's passages.

This module is the surviving home of the tool_search policy.  The ContextForge
block still carries an identical copy of it (same budgets, different collection)
for the retiring gateway path; when that block is deleted this file keeps the
field set unchanged and only the pinned collection differs.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentkit.knowledge.config import EmbeddingConfig, MilvusConfig, RerankConfig

#: The one collection holding the derived tool documents.
MCP_TOOL_COLLECTION = "myflow_mcp_tools"


class ToolKeywordConfig(BaseModel):
    """BM25 sparse channel stored in Milvus."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    analyzer: Literal["chinese", "english", "standard"] = "chinese"
    bm25_k1: float = Field(default=1.2, gt=0, le=10)
    bm25_b: float = Field(default=0.75, ge=0, le=1)


class ToolEmbeddingConfig(EmbeddingConfig):
    """Dense channel layered over the shared embedding service."""

    enabled: bool = True
    query_instruction: str = "Given a task, retrieve the tool that can perform it"
    document_instruction: str = ""


class ToolRerankConfig(RerankConfig):
    """Optional cross-encoder stage; a failure keeps the Milvus order."""

    enabled: bool = False


class ToolVectorStoreConfig(MilvusConfig):
    """The one collection in the existing Milvus instance that holds tools."""

    collection: str = Field(
        default=MCP_TOOL_COLLECTION,
        description="Tool catalog collection; never the knowledge or skills collection.",
    )

    @field_validator("collection")
    @classmethod
    def _pin_tools_collection(cls, value: str) -> str:
        if value != MCP_TOOL_COLLECTION:
            raise ValueError(
                f"tool_search.vector_store.collection must be {MCP_TOOL_COLLECTION!r}: "
                "tool documents never share the knowledge or skills collection."
            )
        return MCP_TOOL_COLLECTION


class ToolSearchConfig(BaseModel):
    """tool_search retrieval and output-budget policy."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    candidate_k: int = Field(default=20, ge=1, le=200)
    top_k: int = Field(default=5, ge=1, le=20)
    rrf_k: int = Field(
        default=60,
        ge=1,
        le=1000,
        description="Milvus RRFRanker 的 k；融合在服务端完成。",
    )
    min_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Milvus 返回分数的下限（混合检索是 RRFRanker 原始分，约 1/(k+rank)）。",
    )
    max_document_chars: int = Field(default=4000, ge=200, le=100000)
    max_description_chars: int = Field(default=500, ge=20, le=10000)
    #: One tool card's schema budget.  5 x 3000 stays inside max_result_chars,
    #: so a full five-card answer never has to be cut for a single fat schema.
    max_schema_chars: int = Field(default=3000, ge=200, le=100000)
    #: The whole tool_search ToolMessage: five cards plus their metadata.
    max_result_chars: int = Field(default=16000, ge=200, le=100000)
    #: tool_execute refuses a call_name this run did not retrieve.  The design
    #: default is on; turning it off is a deliberate loosening of the gate.
    require_retrieved: bool = True
    #: How many call names one run may hold callable.  The design caps it at five
    #: so a retrieved set stays a shortlist, never a second tool table.
    max_retrieved_tools: int = Field(default=5, ge=1, le=5)
    keyword: ToolKeywordConfig = Field(default_factory=ToolKeywordConfig)
    embedding: ToolEmbeddingConfig = Field(default_factory=ToolEmbeddingConfig)
    rerank: ToolRerankConfig = Field(default_factory=ToolRerankConfig)
    vector_store: ToolVectorStoreConfig = Field(default_factory=ToolVectorStoreConfig)

    @model_validator(mode="after")
    def _stages_are_coherent(self) -> Self:
        if self.candidate_k < self.top_k:
            raise ValueError("tool_search.candidate_k must be >= top_k")
        if not self.keyword.enabled and not self.embedding.enabled:
            raise ValueError(
                "tool_search needs at least one recall channel: enable keyword or embedding"
            )
        return self


__all__ = [
    "MCP_TOOL_COLLECTION",
    "ToolEmbeddingConfig",
    "ToolKeywordConfig",
    "ToolRerankConfig",
    "ToolSearchConfig",
    "ToolVectorStoreConfig",
]
