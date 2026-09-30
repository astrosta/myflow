"""
RunCreateRequest(HTTP和内部launch共用)
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentkit.runtime.runs import RunRecord


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_id: str | None = Field(default=None, description="agent name")
    input: dict[str, Any] | None =  Field(default=None, description="input data(e.g. {messages: [...]})")
    command: dict[str, Any] | None =  Field(default=None, description="Langgraph Command")
    config: dict[str, Any] | None =  Field(default=None, description="RunnableConfig overrides")
    context: dict[str, Any] | None =  Field(default=None, description="context overrides (model_name, thinking_enabled, etc.)")
    metadata: dict[str, Any] | None =  Field(default=None, description="Metadata")
    checkpoint_id: str | None =  Field(defaut=None, description="Checkpoint ID")
    checkpoint: dict[str, Any] | None =  Field(default=None, description="Checkpoint")
    stream_mode: list[str] | None =  Field(default=None, description="Stream mode")
    on_disconnect: Literal["cancel", "continue"] = "cancel"
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"

class RunCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["interrupt", "rollback"] = "interrupt"
    wait: bool = False         # 等到 worker 收尾完成再返回（对齐官方 SDK 的 runs.cancel）

class CallerKind(StrEnum):
    HTTP = "http"
    INTERNAL = "internal"

@dataclass(frozen=True, slots=True)
class RunCaller:
    kind: CallerKind
    user_id: str | None #这一轮归属的user_id
    scopes: frozenset[str] | None = None #权限集合
    user_role: str | None = None #用户角色

    @property
    def is_internal(self) -> bool:
        return self.kind is CallerKind.INTERNAL
    
    def allows(self,  permission: str) -> bool:
        return self.scopes is None or permission in self.scopes

    @classmethod
    def internal(cls, *, owner_user_id: str) -> RunCaller:
        return cls(kind=CallerKind.INTERNAL, user_id=owner_user_id)

@dataclass(frozen=True, slots=True)
class RunLaunch:
    record: RunRecord
    reused: bool 