"""ContextForge tool-management configuration.

The contextforge block is disabled by default. It is the only place that
knows how to reach ContextForge; the rest of the runtime consumes this
config through AppConfig.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentkit.knowledge.config import EmbeddingConfig, MilvusConfig, RerankConfig

TOOLS_COLLECTION = "myflow_tools_catalog"


class ContextForgeSyncConfig(BaseModel):
    """How the process refreshes tool metadata and when writes return."""

    model_config = ConfigDict(extra="forbid")

    admin_refresh_async: bool = True
    startup_sync: bool = True
    sync_timeout_seconds: float = Field(default=60.0, gt=0)
    cache_ttl_seconds: int = Field(default=60, ge=5)
    retry_on_tool_error: bool = True
    tool_error_refresh_timeout_seconds: float = Field(default=10.0, gt=0)
    gc_on_manual_reindex: bool = True


class AdminEventsConfig(BaseModel):
    """GET /admin/events subscription settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    url: str = ""
    token: str = Field(default="", repr=False)
    reconnect_min_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_seconds: float = Field(default=60.0, gt=0)
    read_timeout_seconds: float = Field(default=90.0, gt=0)
    debounce_ms: int = Field(default=500, ge=0)
    refresh_min_interval_seconds: float = Field(default=5.0, ge=1)


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
    query_instruction: str = (
        "Given a task, retrieve the tool that can perform it"
    )
    document_instruction: str = ""


class ToolRerankConfig(RerankConfig):
    """Optional cross-encoder stage; failure keeps the Milvus order."""

    enabled: bool = False


class ToolVectorStoreConfig(MilvusConfig):
    """One collection in the existing Milvus instance."""

    collection: str = Field(
        default=TOOLS_COLLECTION,
        description="Tool catalog collection; never the knowledge or skills collection.",
    )

    @field_validator("collection")
    @classmethod
    def _pin_tools_collection(cls, value: str) -> str:
        if value != TOOLS_COLLECTION:
            raise ValueError(
                "contextforge.search.vector_store.collection must be "
                f"{TOOLS_COLLECTION!r}: tool documents never share the knowledge "
                "or skills collection."
            )
        return TOOLS_COLLECTION


class ToolSearchConfig(BaseModel):
    """search_tools retrieval policy."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    candidate_k: int = Field(default=20, ge=1, le=200)
    top_k: int = Field(default=5, ge=1, le=20)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_document_chars: int = Field(default=4000, ge=200, le=100000)
    max_description_chars: int = Field(default=500, ge=20, le=10000)
    max_result_chars: int = Field(default=6000, ge=200, le=100000)
    keyword: ToolKeywordConfig = Field(default_factory=ToolKeywordConfig)
    embedding: ToolEmbeddingConfig = Field(default_factory=ToolEmbeddingConfig)
    rerank: ToolRerankConfig = Field(default_factory=ToolRerankConfig)
    vector_store: ToolVectorStoreConfig = Field(default_factory=ToolVectorStoreConfig)

    @model_validator(mode="after")
    def _stages_are_coherent(self) -> Self:
        if self.candidate_k < self.top_k:
            raise ValueError("contextforge.search.candidate_k must be >= top_k")
        if not self.keyword.enabled and not self.embedding.enabled:
            raise ValueError(
                "contextforge.search needs at least one recall channel: "
                "enable keyword or embedding"
            )
        return self


class ContextForgeConfig(BaseModel):
    """The contextforge: block. Disabled means no network calls at all."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = ""
    admin_token: str = Field(default="", repr=False)
    agent_token: str = Field(default="", repr=False)
    mcp_url: str = ""
    virtual_server_id: str = ""
    verify_tls: bool = True
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    tool_name_prefix: bool = False
    tool_loading: Literal["auto", "eager", "deferred"] = "auto"
    deferred_threshold: int = Field(default=24, ge=0)
    sync: ContextForgeSyncConfig = Field(default_factory=ContextForgeSyncConfig)
    admin_events: AdminEventsConfig = Field(default_factory=AdminEventsConfig)
    search: ToolSearchConfig = Field(default_factory=ToolSearchConfig)

    @field_validator("base_url", "mcp_url")
    @classmethod
    def _url_shape(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        if not text.startswith(("http://", "https://")):
            raise ValueError("ContextForge URLs must start with http:// or https://")
        return text.rstrip("/")

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.enabled and not self.base_url:
            raise ValueError("contextforge.enabled=true requires base_url")
        if self.tool_loading != "eager" and not self.search.enabled:
            raise ValueError(
                "contextforge.tool_loading="
                f"{self.tool_loading!r} requires contextforge.search.enabled=true"
            )
        if self.search.enabled and self.enabled:
            if not self.search.vector_store.uri.strip():
                raise ValueError(
                    "contextforge.search.enabled=true requires vector_store.uri "
                    "(the existing Milvus instance)"
                )
            if not self.search.embedding.base_url.strip():
                raise ValueError(
                    "contextforge.search.enabled=true requires "
                    "embedding.base_url"
                )
            if not self.search.embedding.model.strip():
                raise ValueError(
                    "contextforge.search.enabled=true requires embedding.model"
                )
            if self.search.rerank.enabled:
                if not self.search.rerank.base_url.strip():
                    raise ValueError(
                        "contextforge.search.rerank.enabled=true requires "
                        "rerank.base_url"
                    )
                if not self.search.rerank.model.strip():
                    raise ValueError(
                        "contextforge.search.rerank.enabled=true requires "
                        "rerank.model"
                    )
        if self.sync.cache_ttl_seconds < 5:
            raise ValueError("contextforge.sync.cache_ttl_seconds must be >= 5")
        if self.admin_events.reconnect_max_seconds < self.admin_events.reconnect_min_seconds:
            raise ValueError(
                "contextforge.admin_events.reconnect_max_seconds must be >= "
                "reconnect_min_seconds"
            )
        return self

    def resolved_mcp_url(self) -> str:
        """Return the MCP endpoint, deriving it from base_url when omitted."""
        if self.mcp_url:
            return self.mcp_url
        if not self.base_url:
            return ""
        if self.virtual_server_id:
            return f"{self.base_url}/servers/{self.virtual_server_id}/mcp"
        return f"{self.base_url}/mcp"

    def resolved_agent_token(self) -> str:
        return self.agent_token or self.admin_token

    def resolved_admin_events_token(self) -> str:
        return self.admin_events.token or self.admin_token

    def resolved_admin_events_url(self) -> str:
        return self.admin_events.url or f"{self.base_url.rstrip('/')}/admin/events"


__all__ = [
    "AdminEventsConfig",
    "ContextForgeConfig",
    "ContextForgeSyncConfig",
    "TOOLS_COLLECTION",
    "ToolEmbeddingConfig",
    "ToolKeywordConfig",
    "ToolRerankConfig",
    "ToolSearchConfig",
    "ToolVectorStoreConfig",
]
