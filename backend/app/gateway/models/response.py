"""Response models. Wire conversion lives here so handlers do not assemble JSON by hand."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ReasoningEffortResponse(BaseModel):
    id: str
    label: str
    description: str | None = None
    default: bool = False


class ModelResponse(BaseModel):
    name: str
    model: str
    display_name: str | None = None
    description: str | None = None
    #: 厂商展示名：输入框里模型列表上方的那行小字（config 的 provider，或从 use 推导）
    provider: str = ""
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    context_window: int | None = None
    reasoning_efforts: list[ReasoningEffortResponse] = []
    default_reasoning_effort: str | None = None
    #: config = 配置里写死的条目；vendor = 厂商 /models 报出来、复用了同一份 provider 配置
    source: str = "config"
    available: bool = True


class ModelsListResponse(BaseModel):
    models: list[ModelResponse]
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    #: 厂商目录拉取失败的原因（只影响列表来源，不影响可用性）
    vendor_errors: list[str] = []


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

#: 由 ModelConfig 的属性算出来，而不是直接 getattr 拷贝
#: （reasoning_efforts 是对象列表，default_reasoning_effort 是派生属性）。


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


class RunEventResponse(BaseModel):
    seq: int
    run_id: str
    event_type: str
    category: str
    content: Any
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


def run_event_to_wire(event: Mapping[str, Any]) -> RunEventResponse:
    """Read-only ledger row. user_id and other persistence columns stay off the wire."""
    data = {key: event.get(key) for key in RunEventResponse.model_fields}
    data["metadata"] = dict(event.get("metadata") or {})
    data["created_at"] = _wire_time(event.get("created_at"))
    return RunEventResponse.model_validate(data)


def model_to_wire(cfg: ModelConfig) -> ModelResponse:
    """只拷白名单字段；model_dump() 会把 api_key 和 provider 设置一起漏出去。"""
    from agentkit.models.catalog import provider_label

    data = {key: getattr(cfg, key) for key in _MODEL_WIRE_FIELDS}
    data["provider"] = provider_label(cfg)
    data["reasoning_efforts"] = [effort_to_wire(option) for option in cfg.reasoning_efforts]
    data["default_reasoning_effort"] = cfg.default_reasoning_effort
    return ModelResponse.model_validate(data)


def catalog_model_to_wire(model: Any) -> ModelResponse:
    """厂商目录条目（含厂商新报出来的模型）转 wire。"""

    return ModelResponse(
        name=model.name,
        model=model.model,
        display_name=model.display_name,
        description=model.description,
        provider=model.provider,
        supports_thinking=model.supports_thinking,
        supports_reasoning_effort=model.supports_reasoning_effort,
        supports_vision=model.supports_vision,
        context_window=model.context_window,
        reasoning_efforts=[effort_to_wire(option) for option in model.reasoning_efforts],
        default_reasoning_effort=model.default_reasoning_effort,
        source=model.source,
        available=model.available,
    )


def effort_to_wire(option: Any) -> dict[str, Any]:
    return {
        "id": option.id,
        "label": option.display,
        "description": option.description,
        "default": option.default,
    }


def _wire_time(value: Any) -> str | None:
    if isinstance(value, datetime):
        return iso_utc(value)
    if value in (None, ""):
        return None
    return str(value)

# ---------------------------------------------------------------------------
# M7 §2.4：MCP 配置与技能目录
# ---------------------------------------------------------------------------


class SkillResponse(BaseModel):
    """category 是 public / custom，不是磁盘路径。

    M7 的五个字段保持不变（filesystem 来源仍然只回填它们）。Git 来源在此之上
    追加目录字段：skill_id/scope/版本/commit/path/status。新增字段全部可空，
    旧客户端只读它认识的字段就不会看到形状变化。

    - enabled 是用户开关（Git 私有技能持久化在 skills.enabled）；
    - owner_user_id 只返回给所有者；
    - container_file 是沙箱路径，私有技能只对所有者（且 provider 支持按用户挂载时）返回。
    """

    name: str
    description: str
    category: Literal["public", "custom", "builtin"]
    enabled: bool
    # ---- Git catalog fields (Task 15) ----
    skill_id: str | None = None
    scope: Literal["public", "private", "builtin"] | None = None
    skill_version: str | None = None
    version_id: str | None = None
    git_commit: str | None = None
    skill_path: str | None = None
    status: str | None = None
    owner_user_id: str | None = None
    container_file: str | None = None


class SkillsListResponse(BaseModel):
    skills: list[SkillResponse]


class SkillReloadResponse(BaseModel):
    reloaded: bool = True  # 只表示这个 worker


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


class KnowledgeTypeStatus(BaseModel):
    value: str
    strategy: str
    chunk_size_tokens: int
    chunk_overlap_tokens: int


class KnowledgeStatusResponse(BaseModel):
    enabled: bool
    parser: str
    parser_mode: str
    embedding_model: str
    embedding_dimensions: int
    vector_store: str
    collection: str
    allowed_extensions: list[str]
    text_extensions: list[str]
    max_file_size_bytes: int
    max_total_size_bytes: int
    max_files_per_request: int
    max_documents_per_base: int
    types: list[KnowledgeTypeStatus]


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    type: str
    description: str | None = None
    document_count: int
    chunk_count: int
    created_at: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    bases: list[KnowledgeBaseResponse]
    count: int


class KnowledgeDocumentResponse(BaseModel):
    id: str
    knowledge_base_id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    chunk_count: int
    error_message: str | None = None
    created_at: str
    updated_at: str


class KnowledgeDocumentListResponse(BaseModel):
    documents: list[KnowledgeDocumentResponse]
    count: int


class KnowledgeChunkResponse(BaseModel):
    chunk_id: str
    chunk_index: int
    section: str = ""
    page: int = 0
    content: str
    #: Set for children of the parent-child strategy; empty for flat chunks.
    parent_id: str = ""


class KnowledgeChunksResponse(BaseModel):
    document_id: str
    chunks: list[KnowledgeChunkResponse]
    count: int
    next_offset: int | None = None


class KnowledgeUploadResponse(BaseModel):
    documents: list[KnowledgeDocumentResponse]
    count: int


class KnowledgeCitationResponse(BaseModel):
    citation_id: str
    chunk_id: str
    document_id: str
    knowledge_base_id: str = ""
    knowledge_base_name: str = ""
    knowledge_base_type: str = ""
    filename: str = ""
    section: str = ""
    page: int = 0
    chunk_index: int = 0
    quote: str = ""


class KnowledgeSearchHitResponse(BaseModel):
    chunk_id: str
    citation_id: str = ""
    document_id: str
    filename: str
    section: str
    page: int
    chunk_index: int
    parent_id: str = ""
    score: float
    knowledge_base_id: str
    knowledge_base_name: str
    knowledge_base_type: str
    content: str
    rrf_score: float | None = None
    rerank_score: float | None = None


class KnowledgeSearchResponse(BaseModel):
    query: str
    keyword_query: str = ""
    retrieval_mode: str = "vector"
    reranked: bool = False
    results: list[KnowledgeSearchHitResponse]
    citations: list[KnowledgeCitationResponse] = Field(default_factory=list)
    truncated: bool = False


class KnowledgeDeletedResponse(BaseModel):
    deleted: str


def knowledge_base_to_wire(item) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=item.id,
        name=item.name,
        type=item.type,
        description=item.description,
        document_count=item.document_count,
        chunk_count=item.chunk_count,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def knowledge_document_to_wire(item) -> KnowledgeDocumentResponse:
    return KnowledgeDocumentResponse(
        id=item.id,
        knowledge_base_id=item.knowledge_base_id,
        filename=item.filename,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        status=item.status,
        chunk_count=item.chunk_count,
        error_message=item.error_message,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )