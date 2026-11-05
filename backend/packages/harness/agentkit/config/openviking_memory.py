"""Configuration for the OpenViking memory adapter.

The config is intentionally separate from the legacy MemoryConfig so the new
module can be introduced without changing the old runtime. The session policy
here is the single source of truth for create_session calls.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The application-level memory type catalog used by retrieval, model tools and
#: the management API. Session skills are deliberately not a memory type here:
#: OpenViking extracts them into the user's skill directory and MyFlow imports
#: them into the Git-backed skill catalog instead.
DEFAULT_MEMORY_TYPES: tuple[str, ...] = (
    "preferences",
    "entities",
    "events",
    "cases",
    "trajectories",
    "experiences",
    "tools",
)

#: Types accepted by the stock OpenViking 0.5.2 memory templates. Session
#: creation always starts from this constant; tools/skills are not session
#: memory types.
DEFAULT_SESSION_MEMORY_TYPES: tuple[str, ...] = (
    "preferences",
    "entities",
    "events",
    "cases",
    "trajectories",
    "experiences",
)


def idle_only_auto_commit_policy() -> dict[str, int]:
    """Return a fresh policy that disables token/message thresholds.

    OpenViking 0.4.19 treats a threshold of 0 as disabled. Only the 24h idle
    trigger remains active.
    """

    return {
        "pending_token_threshold": 0,
        "message_count_threshold": 0,
        "idle_timeout_seconds": 86_400,
        "keep_recent_count": 0,
        "min_commit_interval_seconds": 0,
    }


class SessionPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self_enabled: bool = True
    peer_enabled: bool = False
    working_memory: bool = False
    memory_types: tuple[str, ...] = DEFAULT_SESSION_MEMORY_TYPES

    @model_validator(mode="after")
    def _validate_memory_types(self) -> "SessionPolicyConfig":
        unknown = set(self.memory_types) - set(DEFAULT_SESSION_MEMORY_TYPES)
        if unknown:
            raise ValueError(f"unsupported memory types: {sorted(unknown)}")
        if tuple(self.memory_types) != DEFAULT_SESSION_MEMORY_TYPES:
            raise ValueError(
                "memory_types must equal DEFAULT_SESSION_MEMORY_TYPES"
            )
        return self

    def memory_policy(self) -> dict[str, Any]:
        return {
            "self": {"enabled": self.self_enabled},
            "peer": {"enabled": self.peer_enabled},
            "working_memory": {"enabled": self.working_memory},
            "memory_types": list(self.memory_types),
        }


class CaptureStreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    redis_url: str = ""
    stream_key_prefix: str = "myflow:memory:capture"
    shard_count: int = Field(default=64, ge=1, le=1024)
    consumer_group_prefix: str = "myflow-memory-workers"
    consumer_name_prefix: str = "myflow-worker"
    consumers_per_shard: Literal[1] = 1
    worker_singleton_key: str = "myflow:memory:worker:singleton"
    singleton_lease_seconds: int = Field(default=60, ge=5, le=3600)
    maxlen: int = Field(default=100_000, ge=1)
    block_ms: int = Field(default=5_000, ge=1)
    claim_idle_ms: int = Field(default=60_000, ge=1)
    enqueue_timeout_ms: int = Field(default=50, ge=1, le=10_000)


class CaptureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skip_greetings: bool = True
    skip_emoji_only: bool = True
    extra_skip_tokens: tuple[str, ...] = ()
    missing_turn_end_timeout_seconds: int = Field(default=86_400, ge=1)
    dedup_lease_seconds: int = Field(default=60, ge=5, le=3600)

    @model_validator(mode="after")
    def _validate_extra_skip_tokens(self) -> "CaptureConfig":
        from agentkit.agents.memory.openviking.policy import validate_extra_skip_tokens

        validate_extra_skip_tokens(self.extra_skip_tokens)
        return self


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_days: int = Field(default=30, ge=1)
    suggestion_days: int = Field(default=7, ge=1)
    dedup_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _dedup_not_shorter_than_session(self) -> "RetentionConfig":
        # A dedup row must outlive the session it protects; clearing it early
        # recreates the append-success/DB-failure duplicate window.
        if self.dedup_days < self.session_days:
            self.dedup_days = self.session_days
        return self


class ProfileSuggesterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "agent_default"
    timeout_seconds: float = Field(default=5.0, gt=0.0)


class PersonalizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    identity_file: str = "config/personalization/identity.md"
    soul_file: str = "config/personalization/soul.md"
    profile_seed_file: str = "config/personalization/profile.md"
    max_tokens: int = Field(default=1800, ge=1, le=16_000)
    include_empty_fields: bool = False


class SessionSkillsConfig(BaseModel):
    """Import OpenViking session-extracted skills into the Git skill catalog.

    OpenViking exposes the generated skill URIs only after the commit task
    reaches a terminal state. The worker polls that task, reads each generated
    SKILL.md and creates an extracted, unscanned draft for the user to review,
    scan and activate in the normal skills UI.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    poll_interval_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    task_timeout_seconds: float = Field(default=180.0, gt=0.0, le=3600.0)
    max_skills_per_task: int = Field(default=20, ge=1, le=100)


class OpenVikingMemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Safe default: an explicit block must opt in. Without this, adding only
    # a URL/key to config.yaml would silently replace canonical memory.
    enabled: bool = False
    provider: Literal["openviking"] = "openviking"
    required: bool = False

    url: str = "http://127.0.0.1:1933"
    auth_mode: Literal["trusted"] = "trusted"
    account: str = "myflow"
    root_api_key: str = ""
    timeout_seconds: float = Field(default=10.0, gt=0.0)
    user_id_salt: str = ""
    studio_url: str = ""

    session: SessionPolicyConfig = Field(default_factory=SessionPolicyConfig)
    session_skills: SessionSkillsConfig = Field(default_factory=SessionSkillsConfig)
    session_skills: SessionSkillsConfig = Field(default_factory=SessionSkillsConfig)
    capture_stream: CaptureStreamConfig = Field(default_factory=CaptureStreamConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    profile_suggester: ProfileSuggesterConfig = Field(default_factory=ProfileSuggesterConfig)
    personalization: PersonalizationConfig = Field(default_factory=PersonalizationConfig)

    def session_create_options(self) -> dict[str, Any]:
        return {
            "memory_policy": self.session.memory_policy(),
            "auto_commit_policy": idle_only_auto_commit_policy(),
        }