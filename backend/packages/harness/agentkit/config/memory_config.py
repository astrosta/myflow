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

    injection_enabled: bool = True
    max_tokens: int = Field(default=800, ge=1, le=16_000)
    refresh: Literal["thread_start", "revision_delta"] = "thread_start"


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


class MemoryConfig(BaseModel):
    """memory: block in config.yaml."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    backend_class: str = Field(
        default="langmem",
        description=format_field_description("memory.backend_class"),
    )
    profile: MemoryProfileConfig = Field(default_factory=MemoryProfileConfig)
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
