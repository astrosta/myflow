"""Caller identity, launch result, and the run request models shared with internal callers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from agentkit.authz import ADMIN_ROLE
from agentkit.runtime.runs import RunRecord
from backend.app.gateway.models.request import _THREAD_ID_PATTERN
from backend.app.gateway.models.response import run_to_wire

_THREAD_ID = re.compile(_THREAD_ID_PATTERN)

__all__ = [
    "CallerKind",
    "RunCaller",
    "RunCancelRequest",
    "RunCreateRequest",
    "RunLaunch",
    "run_to_wire",
]


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_id: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] | None = None
    command: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    checkpoint_id: str | None = Field(default=None, max_length=128)
    checkpoint: dict[str, Any] | None = None
    stream_mode: list[str] | str | None = None
    stream_subgraphs: bool = False
    on_disconnect: Literal["cancel", "continue"] = "cancel"
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"
    webhook: None = None
    on_completion: None = None
    after_seconds: None = None
    feedback_keys: None = None
    if_not_exists: Literal["create"] = "create"
    stream_resumable: Literal[False] | None = None

    @model_validator(mode="after")
    def validate_configurable_thread_id(self) -> RunCreateRequest:
        if not isinstance(self.config, dict):
            return self
        configurable = self.config.get("configurable")
        if not isinstance(configurable, dict) or "thread_id" not in configurable:
            return self
        raw = configurable["thread_id"]
        if raw is not None and not _THREAD_ID.match(str(raw)):
            raise PydanticCustomError("thread_id", "thread_id must match ^[A-Za-z0-9_-]{1,64}$")
        return self

    @field_validator(
        "stream_resumable",
        "webhook",
        "on_completion",
        "after_seconds",
        "feedback_keys",
        "if_not_exists",
        "multitask_strategy",
        mode="before",
    )
    @classmethod
    def reject_unsupported_run_options(cls, value: Any, info: ValidationInfo) -> Any:
        if info.field_name in {"multitask_strategy", "if_not_exists"} and not isinstance(value, str):
            return value
        allowed = {
            "webhook": None,
            "on_completion": None,
            "after_seconds": None,
            "feedback_keys": None,
            "if_not_exists": "create",
            "multitask_strategy": {"reject", "rollback", "interrupt"},
            "stream_resumable": {None, False},
        }[info.field_name]
        ok = value in allowed if isinstance(allowed, set) else value == allowed
        if not ok:
            raise PydanticCustomError(
                "unsupported_run_option",
                "Run option '{option}' is not supported",
                {"option": info.field_name},
            )
        return value

    @field_validator("stream_mode", mode="before")
    @classmethod
    def reject_unsupported_stream_modes(cls, value: Any) -> Any:
        if value is None:
            return value
        from agentkit.runtime.stream_modes import UnsupportedStreamModeError, normalize_stream_modes

        try:
            normalize_stream_modes(value)
        except UnsupportedStreamModeError as exc:
            raise PydanticCustomError(
                "unsupported_stream_mode",
                "Unsupported stream mode(s): {modes}",
                {"modes": ", ".join(exc.modes)},
            ) from exc
        return value


class RunCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["interrupt", "rollback"] = "interrupt"
    wait: bool = False


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
        """能力门。

        `admin` 有两个合法来源：会话用户的 `system_role`，或显式带 `admin` 的 scope 集合。
        **PAT 永远拿不到它**——`AuthService.authenticate_pat` 会把 `admin` 从 scope 里减掉，
        而 `PAT_SCOPE_MENU` 里根本没有这个值（§2.15"永不带 admin"）。
        其余能力按 scope 判，`scopes=None`（会话 / 内部调用方）表示不裁。
        """
        if permission == ADMIN_ROLE:
            return self.user_role == ADMIN_ROLE or (
                self.scopes is not None and ADMIN_ROLE in self.scopes
            )
        return self.scopes is None or permission in self.scopes

    @classmethod
    def internal(cls, *, owner_user_id: str) -> RunCaller:
        return cls(kind=CallerKind.INTERNAL, user_id=owner_user_id)

@dataclass(frozen=True, slots=True)
class RunLaunch:
    record: RunRecord
    reused: bool 