"""Memory configuration (M10 section 2.11).

Only host-shared knobs live here. Backend-private settings stay in backend_config
as a free-form mapping so a new backend does not have to edit this module.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentkit.config.reload_boundary import format_field_description

logger = logging.getLogger(__name__)


class MemoryProfileConfig(BaseModel):
    """User profile injection policy."""

    model_config = ConfigDict(extra="forbid")

    inject: bool = True
    max_tokens: int = Field(default=800, ge=1, le=16_000)
    auto_write_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    refresh: Literal["thread_start", "revision_delta"] = "thread_start"

    @property
    def injection_enabled(self) -> bool:
        """Backward-compatible read alias; new configs use inject."""
        return self.inject

    @model_validator(mode="before")
    @classmethod
    def _map_legacy_injection_enabled(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "injection_enabled" not in data:
            return data
        migrated = dict(data)
        legacy = migrated.pop("injection_enabled")
        if "inject" in migrated and migrated["inject"] != legacy:
            raise ValueError("memory.profile.inject and injection_enabled disagree")
        migrated["inject"] = legacy
        return migrated


class MemoryIngestConfig(BaseModel):
    """Conversation extraction queue policy."""

    model_config = ConfigDict(extra="forbid")

    debounce_seconds: float = Field(default=30.0, ge=0.0, le=3_600.0)
    shutdown_flush_timeout_seconds: float = Field(default=30.0, ge=0.0, le=300.0)


class MemoryFoldingConfig(BaseModel):
    """Semantic folding policy (facts -> denser topic facts)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_facts: int = Field(default=50, ge=3)
    min_topic_fragments: int = Field(default=3, ge=2)
    min_cluster_size: int = Field(default=3, ge=2, le=20)
    max_cluster_size: int = Field(default=20, ge=3, le=50)
    similarity_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    max_summary_token_ratio: float = Field(default=0.80, gt=0.0, le=1.0)
    max_fold_depth: int = Field(default=2, ge=1, le=10)
    max_clusters_per_run: int = Field(default=10, ge=1, le=100)
    cooldown_days: float = Field(default=7.0, ge=0.0)


class MemoryMaintenanceConfig(BaseModel):
    """Periodic vitality eviction + semantic folding."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_hours: float = Field(default=24.0, gt=0.0)
    jitter_minutes: float = Field(default=60.0, ge=0.0)
    max_users: int = Field(default=500, ge=1)
    max_duration_seconds: float = Field(default=900.0, gt=0.0)
    shadow: bool = True
    high_watermark: int = Field(default=200, ge=1)
    target_count: int = Field(default=160, ge=0)
    min_vitality: float = Field(default=0.15, ge=0.0, le=1.0)
    min_age_days: float = Field(default=7.0, ge=0.0)
    explicit_min_age_days: float = Field(default=30.0, ge=0.0)
    explicit_consecutive_low_cycles: int = Field(default=2, ge=1)
    max_evictions_per_run: int = Field(default=50, ge=1)
    max_eviction_ratio: float = Field(default=0.10, gt=0.0, le=1.0)
    archive_retention_days: float = Field(default=30.0, ge=0.0)
    search_activity_window_days: float = Field(default=30.0, ge=0.0)
    min_search_calls_per_window: int = Field(default=20, ge=0)
    folding: MemoryFoldingConfig = Field(default_factory=MemoryFoldingConfig)

    @model_validator(mode="before")
    @classmethod
    def _drop_null_folding(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("folding") is None:
            return {key: value for key, value in data.items() if key != "folding"}
        return data

    @model_validator(mode="after")
    def _target_is_below_watermark(self) -> "MemoryMaintenanceConfig":
        if self.target_count > self.high_watermark:
            raise ValueError("memory.maintenance.target_count must be <= high_watermark")
        if self.folding.min_cluster_size > self.folding.max_cluster_size:
            raise ValueError(
                "memory.maintenance.folding.min_cluster_size must be <= max_cluster_size"
            )
        return self


class MemoryLangMemConfig(BaseModel):
    """Operator model and timeout policy; operators are stateless."""

    model_config = ConfigDict(extra="forbid")

    model: str = "agent_default"
    operator_timeout_seconds: float = Field(default=60.0, gt=0.0)
    schema_version: int = Field(default=1, ge=1)


class MemoryVectorStoreConfig(BaseModel):
    """Milvus projection settings. PostgreSQL remains authoritative."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["milvus"] = "milvus"
    uri: str = ""
    token: str = Field(default="", repr=False)
    collection: str = "myflow_memory_active"
    metric_type: Literal["COSINE", "IP", "L2"] = "COSINE"
    index_type: str = "AUTOINDEX"
    consistency_level: Literal["Strong", "Session", "Bounded", "Eventually"] = "Strong"


class MemoryEmbeddingConfig(BaseModel):
    """Embedding endpoint used by the memory projection/retrieval."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    api_key: str = Field(default="", repr=False)
    model: str = "Qwen/Qwen3-Embedding-8B"
    dimensions: int = Field(default=4096, ge=1, le=65536)
    batch_size: int = Field(default=8, ge=1, le=512)
    timeout_seconds: float = Field(default=120.0, gt=0.0)
    max_retries: int = Field(default=2, ge=0, le=8)


class MemorySemanticConfig(BaseModel):
    """Semantic fact visibility thresholds."""

    model_config = ConfigDict(extra="forbid")

    auto_activate_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    pending_review_ttl_days: int = Field(default=30, ge=1)
    include_pending_in_extraction: bool = True


class MemoryEpisodicConfig(BaseModel):
    """Episode capture/close policy."""

    model_config = ConfigDict(extra="forbid")

    session_idle_close_minutes: int = Field(default=30, ge=1)
    max_turns_per_session: int = Field(default=40, ge=1)
    gate_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    max_summary_tokens: int = Field(default=1200, ge=1)
    ttl_days: int = Field(default=180, ge=1)


class MemoryConsolidationConfig(BaseModel):
    """L2/L3 consolidation cadence; shadow is observability-only by default."""

    model_config = ConfigDict(extra="forbid")

    l2_unconsolidated_threshold: int = Field(default=10, ge=1)
    l3_cron: str = "0 3 * * *"
    shadow: bool = True


class MemorySkillCandidatesConfig(BaseModel):
    """Host-side skill candidate thresholds."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_source_episodes: int = Field(default=3, ge=1)
    min_distinct_threads: int = Field(default=2, ge=1)
    min_success_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    trigger_similarity_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    strong_trigger_similarity: float = Field(default=0.92, ge=0.0, le=1.0)
    min_common_steps: int = Field(default=2, ge=1)
    expire_days: int = Field(default=30, ge=1)
    require_user_confirmation: bool = True


class MemoryRetrievalConfig(BaseModel):
    """Hybrid retrieval quotas and lexical fallback."""

    model_config = ConfigDict(extra="forbid")

    candidate_k: int = Field(default=50, ge=1)
    lexical_fallback: bool = True
    per_kind_quota: dict[str, int] = Field(
        default_factory=lambda: {"semantic": 6, "episodic": 2}
    )


class MemoryConfigError(ValueError):
    """Memory configuration cannot be used to build a runtime."""


class MemoryConfig(BaseModel):
    """memory: block in config.yaml."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    canonical_store: Literal["postgres"] = "postgres"
    backend_class: str = Field(
        default="langmem",
        description=format_field_description("memory.backend_class"),
    )
    langmem: MemoryLangMemConfig = Field(default_factory=MemoryLangMemConfig)
    vector_store: MemoryVectorStoreConfig = Field(default_factory=MemoryVectorStoreConfig)
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    profile: MemoryProfileConfig = Field(default_factory=MemoryProfileConfig)
    semantic: MemorySemanticConfig = Field(default_factory=MemorySemanticConfig)
    episodic: MemoryEpisodicConfig = Field(default_factory=MemoryEpisodicConfig)
    consolidation: MemoryConsolidationConfig = Field(default_factory=MemoryConsolidationConfig)
    skill_candidates: MemorySkillCandidatesConfig = Field(
        default_factory=MemorySkillCandidatesConfig
    )
    retrieval: MemoryRetrievalConfig = Field(default_factory=MemoryRetrievalConfig)
    ingest: MemoryIngestConfig = Field(default_factory=MemoryIngestConfig)
    maintenance: MemoryMaintenanceConfig = Field(default_factory=MemoryMaintenanceConfig)
    backend_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_deprecated_manager_class(cls, data: Any) -> Any:
        """Old configs used manager_class; migrate it instead of silently dropping it."""
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        for section in ("profile", "ingest", "maintenance", "backend_config"):
            if section in migrated and migrated[section] is None:
                migrated.pop(section)
        if "manager_class" in migrated:
            if "backend_class" in migrated:
                raise ValueError(
                    "memory.manager_class is deprecated; use memory.backend_class "
                    "(both were provided, so the effective backend is ambiguous)"
                )
            migrated["backend_class"] = migrated.pop("manager_class")
            logger.warning("memory.manager_class is deprecated; use memory.backend_class")
        # Old DeerMem global mode is gone. Keep accepting the key so upgrades do not
        # fail at startup; the host behavior is fixed to profile + searchable facts.
        if "mode" in migrated:
            logger.warning("memory.mode is deprecated and ignored; memory is always layered")
            migrated.pop("mode", None)
        return migrated
