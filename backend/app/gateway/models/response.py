"""Response models. Wire conversion lives here so handlers do not assemble JSON by hand."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentkit.config.model_config import ModelConfig
from agentkit.persistence.base import iso_utc
from agentkit.runtime.runs import RunRecord


class RunResponse(BaseModel):
    run_id: str
    thread_id: str
    assistant_id: str | None = None
    status: str
    on_disconnect: Literal["cancel", "continue"] = "cancel"
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"
    metadata: dict[str, Any] = Field(default_factory=dict)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    model_name: str | None = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0


class ThreadResponse(BaseModel):
    thread_id: str
    incarnation: str | None = None
    assistant_id: str | None = None
    display_name: str | None = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class ThreadStateResponse(BaseModel):
    values: dict[str, Any]
    checkpoint_id: str | None = None
    checkpoint_ns: str | None = None


class RunStateResponse(BaseModel):
    values: dict[str, Any]
    checkpoint_id: str | None = None
    checkpoint_ns: str | None = None
    run_id: str
    status: str
    error: str | None = None


class CancelResponse(BaseModel):
    outcome: Literal["cancelled", "requested", "taken_over"]
    run: RunResponse | None = None


class DeletedResponse(BaseModel):
    deleted: str


class ModelResponse(BaseModel):
    name: str
    model: str
    display_name: str | None = None
    description: str | None = None
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    context_window: int | None = None


class ModelsListResponse(BaseModel):
    models: list[ModelResponse]


class UploadedFileInfo(BaseModel):
    filename: str
    original_filename: str
    size: int
    virtual_path: str
    modified: str


class UploadResponse(BaseModel):
    files: list[UploadedFileInfo]


class UploadListResponse(BaseModel):
    files: list[UploadedFileInfo]
    count: int


class DeletedUploadResponse(BaseModel):
    deleted: str


_MODEL_WIRE_FIELDS = (
    "name",
    "model",
    "display_name",
    "description",
    "supports_thinking",
    "supports_reasoning_effort",
    "supports_vision",
    "context_window",
)


class ThreadCompactResponse(BaseModel):
    thread_id: str
    compacted: bool
    reason: str | None = None
    removed_message_count: int = 0
    preserved_message_count: int = 0
    summary_updated: bool = False
    checkpoint_id: str | None = None
    total_tokens: int | None = None


class HistoryEntry(BaseModel):
    checkpoint_id: str
    parent_checkpoint_id: str | None = None
    created_at: str | None = None
    next: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)


class BranchPoint(BaseModel):
    checkpoint_id: str
    parent_checkpoint_id: str
    created_at: str | None = None


class BranchListResponse(BaseModel):
    branches: list[BranchPoint]


class MessageResponse(BaseModel):
    thread_id: str
    run_id: str
    seq: int
    event_type: str
    content: dict[str, Any] | str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class Page[T](BaseModel):
    data: list[T]
    next_cursor: str | None = None
    has_more: bool = False


_RUN_WIRE_FIELDS = set(RunResponse.model_fields)
_THREAD_FIELDS = set(ThreadResponse.model_fields)


def run_to_wire(record: RunRecord | Mapping[str, Any]) -> RunResponse:
    """Live RunRecord and store dicts share one exit. Process-local fields are not on RunResponse."""
    raw = record.__dict__ if isinstance(record, RunRecord) else dict(record)
    kwargs = dict(raw.get("kwargs") or {})
    kwargs.pop("secrets", None)
    data = {key: raw.get(key) for key in _RUN_WIRE_FIELDS}
    for key in ("created_at", "updated_at"):
        data[key] = _wire_time(data.get(key))
    for key in ("total_input_tokens", "total_output_tokens", "llm_call_count"):
        if data.get(key) is None:
            data[key] = 0
    status = data.get("status")
    data["status"] = getattr(status, "value", status)
    disconnect = data.get("on_disconnect")
    data["on_disconnect"] = getattr(disconnect, "value", disconnect) or "cancel"
    data["kwargs"] = kwargs
    data["metadata"] = dict(raw.get("metadata") or {})
    return RunResponse.model_validate(data)


def thread_to_wire(row: Mapping[str, Any]) -> ThreadResponse:
    """threads_meta row. user_id stays off the wire."""
    data = {key: row.get(key) for key in _THREAD_FIELDS}
    data["metadata"] = dict(row.get("metadata") or {})
    for key in ("created_at", "updated_at"):
        data[key] = _wire_time(data.get(key))
    return ThreadResponse.model_validate(data)


def message_to_wire(event: Mapping[str, Any]) -> MessageResponse:
    if event.get("category") not in (None, "message"):
        raise RuntimeError("message_to_wire only accepts category='message'")
    data = {key: event.get(key) for key in MessageResponse.model_fields}
    data["metadata"] = dict(event.get("metadata") or {})
    data["created_at"] = _wire_time(event.get("created_at"))
    return MessageResponse.model_validate(data)


def model_to_wire(cfg: ModelConfig) -> ModelResponse:
    """Copy the public whitelist. model_dump() would leak api_key and provider settings."""
    return ModelResponse.model_validate({key: getattr(cfg, key) for key in _MODEL_WIRE_FIELDS})


def _wire_time(value: Any) -> str | None:
    if isinstance(value, datetime):
        return iso_utc(value)
    if value in (None, ""):
        return None
    return str(value)
