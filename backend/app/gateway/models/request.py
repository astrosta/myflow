"""Request models and path constraints. extra=forbid so unknown SDK fields are 422."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field

from agentkit.config.summarization_config import ContextSize

_THREAD_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
ThreadId = Annotated[str, Path(pattern=_THREAD_ID_PATTERN)]
RunId = Annotated[str, Path(pattern=_THREAD_ID_PATTERN)]
ModelName = Annotated[str, Path(pattern=r"^[A-Za-z0-9._-]{1,128}$")]
JsonScalar = str | int | float | bool | None


class ThreadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str | None = Field(default=None, pattern=_THREAD_ID_PATTERN)
    assistant_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] | None = None


class ThreadPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any] | None = None


class ThreadSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, JsonScalar] | None = None
    status: Literal["idle", "running", "success", "error", "timeout", "interrupted"] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=256)
    order: Literal["asc", "desc"] = "desc"


class ThreadStateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] | None = None
    checkpoint_id: str | None = Field(default=None, max_length=128)
    checkpoint: dict[str, Any] | None = None


class ThreadCompactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    keep: ContextSize | None = None
    model_name: str | None = Field(default=None, max_length=128)

# ---------------------------------------------------------------------------
# 技能启用开关
# ---------------------------------------------------------------------------

#: 技能目录名、PATCH 的 id 共用这一条：斜杠和 .. 进不了文件名。
#: Git 目录里同一个位置放的是内部 skill_id（UUID/十六进制），形状落在同一字符集里。
_NAME = r"^[A-Za-z0-9_-]{1,64}$"
SkillName = Annotated[str, Path(pattern=_NAME)]
SkillId = Annotated[str, Path(pattern=_NAME)]


class SkillPreferenceRequest(BaseModel):
    """用户对已生效私有技能的启用开关；持久化在 skills.enabled。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

KnowledgeTypeLiteral = Literal["technical", "operations", "faq", "security", "api", "legal"]


class KnowledgeBaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    type: KnowledgeTypeLiteral
    description: str | None = Field(default=None, max_length=2000)
class KnowledgeBaseUpdateRequest(BaseModel):
    """Type is intentionally absent: changing it would invalidate every chunk."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)