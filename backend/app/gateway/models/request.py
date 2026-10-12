"""Request models and path constraints. extra=forbid so unknown SDK fields are 422."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

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
# M7 §2.4：MCP 配置与技能启用开关
# ---------------------------------------------------------------------------

#: 技能目录名、PATCH 的 name、mcpServers 的键共用这一条：斜杠和 .. 进不了文件名。
_NAME = r"^[A-Za-z0-9_-]{1,64}$"
SkillName = Annotated[str, Path(pattern=_NAME)]
McpServerName = Annotated[str, StringConstraints(pattern=_NAME)]
McpTransport = Literal["stdio", "sse", "http", "streamable_http"]


def _normalize_transport(value: Any) -> Any:
    """transport 名统一成小写 + 下划线：HTTP / streamable-http 都要认。"""
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_")
        return normalized or None
    return value


class McpUserAuthWrite(BaseModel):
    """per-user 凭据层（§5.5）。值可以写 $ENV，GET 时整层打码。"""

    model_config = ConfigDict(extra="forbid")

    users: dict[str, dict[str, str]] = Field(default_factory=dict)
    on_missing: Literal["fail_closed", "passthrough"] = "fail_closed"


class McpServerWrite(BaseModel):
    """写入用的 MCP 服务器对象（§2.4）。type 与 transport 都在时 type 赢（§2.1）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    type: McpTransport | None = None
    transport: McpTransport | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    tool_name_prefix: bool = True
    session_init_timeout: float | None = 60
    tool_call_timeout: float | None = 60
    headers_from_context: dict[str, str] = Field(default_factory=dict)
    user_auth: McpUserAuthWrite | None = None

    @field_validator("type", "transport", mode="before")
    @classmethod
    def _accept_transport_spellings(cls, value: Any) -> Any:
        return _normalize_transport(value)

    @model_validator(mode="after")
    def _resolve_transport_alias(self) -> Self:
        if self.type is None:
            self.type = self.transport if self.transport is not None else "stdio"
        return self

    def to_config_dict(self) -> dict[str, Any]:
        """写进文件的形状：别名 transport 不落盘，空的可选容器省略（PUT 是整份替换）。"""
        data = self.model_dump(by_alias=True, exclude_none=True, exclude={"transport"})
        for key in ("args", "env", "headers", "headers_from_context"):
            if not data.get(key):
                data.pop(key, None)
        for key in ("session_init_timeout", "tool_call_timeout"):
            value = data.get(key)
            if isinstance(value, float) and value.is_integer():
                data[key] = int(value)
        return data


class McpConfigUpdateRequest(BaseModel):
    """整份替换 mcpServers。**M7 不注册这条路由**（整份替换 = 新建 + 删除，属创建动作）。

    模型现在就定义好：后续阶段的 PUT /api/mcp/config 用它，能力位 mcp:create 也已定名。
    没有 skills、没有 mcpInterceptors —— 那两类会 import 代码。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mcp_servers: dict[McpServerName, McpServerWrite] = Field(alias="mcpServers")


class McpServerEnabledPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME)
    enabled: bool


class SkillUpdateRequest(BaseModel):
    """**全局**开关（admin）。改的是 extensions_config.json 里的 skills.<name>.enabled。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class SkillPreferenceRequest(BaseModel):
    """**个人**开关（任何登录用户）。只写 skills_prefs.json，不影响别人。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
