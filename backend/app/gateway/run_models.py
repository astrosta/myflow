"""
RunCreateRequest(HTTP和内部launch共用)
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentkit.persistence.base import iso_utc
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


def run_to_wire(record: RunRecord) -> dict[str, Any]:
    """Public run JSON for cancel(wait=true). Process-local fields stay off the response."""
    return {
        "run_id": record.run_id,
        "thread_id": record.thread_id,
        "assistant_id": record.assistant_id,
        "status": getattr(record.status, "value", record.status),
        "created_at": _wire_time(record.created_at),
        "updated_at": _wire_time(record.updated_at),
        "metadata": dict(record.metadata or {}),
        "kwargs": dict(record.kwargs or {}),
        "multitask_strategy": record.multitask_strategy,
        "on_disconnect": getattr(record.on_disconnect, "value", record.on_disconnect),
        "error": record.error,
    }


def _wire_time(value: Any) -> str | None:
    if isinstance(value, datetime):
        return iso_utc(value)
    if value in (None, ""):
        return None
    return str(value) 