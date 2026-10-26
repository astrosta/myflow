"""Structured proposals exchanged between LangMem and the host.

These models are deliberately storage-free.  LangMem may create them, but
only the host validates and persists them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentkit.agents.memory.canonical.evidence import EvidenceTier


class SemanticDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4000)
    topic_key: str | None = Field(default=None, max_length=255)
    scope_condition: str = Field(default="global", max_length=255)
    evidence_quote: str = Field(min_length=1, max_length=2000)
    evidence_tier: EvidenceTier


class ExistingSemantic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    document: SemanticDocument


class SemanticExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    document: SemanticDocument | None = None
    remove: bool = False


class ProfileValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | list[str]
    evidence_tier: EvidenceTier
    evidence_quote: str = Field(min_length=1, max_length=2000)


class ProfileDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: dict[str, ProfileValue] = Field(default_factory=dict)
    communication: dict[str, ProfileValue] = Field(default_factory=dict)
    work: dict[str, ProfileValue] = Field(default_factory=dict)
    accessibility: dict[str, ProfileValue] = Field(default_factory=dict)
    boundaries: ProfileValue | None = None


class ProfileProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, ProfileValue] = Field(default_factory=dict)


class DehydratedEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_intent: str = Field(min_length=1, max_length=2000)
    environment: dict[str, Any] = Field(default_factory=dict)
    trajectory_digest: str = Field(default="", max_length=4000)
    final_outcome: str = Field(default="SUCCESS", pattern=r"^(SUCCESS|PARTIAL|FAILURE|ABORTED)$")
    key_takeaway: str = Field(default="", max_length=2000)
    feedback_signal: str = Field(default="NONE", max_length=64)
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    is_skill_candidate: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    task_class: str | None = Field(default=None, max_length=128)
    key_steps: list[str] = Field(default_factory=list)
    trigger_text: str | None = Field(default=None, max_length=1000)
    blocker: bool = False


class EpisodeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: DehydratedEpisode


class SkillProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = ""
    diff: str | None = None
    files: dict[str, str] = Field(default_factory=dict)
    same_sop: bool = True


@dataclass(frozen=True, slots=True)
class TurnDigest:
    intent: str
    outcome: str
    tool_calls: tuple[str, ...]
    summary: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSummary:
    name: str
    status: str
    summary: str


@dataclass(frozen=True, slots=True)
class OperatorContext:
    metadata: dict[str, Any] = field(default_factory=dict)
