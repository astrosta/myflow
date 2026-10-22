"""Configurable knowledge-base pipeline. No service address has a code default."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentkit.knowledge.types import (
    KNOWLEDGE_BASE_TYPES,
    TYPE_STRATEGIES,
    KnowledgeBaseType,
)

DEFAULT_ALLOWED_EXTENSIONS = [
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "md", "markdown",
    "txt", "html", "htm", "csv", "epub",
]
DEFAULT_TEXT_EXTENSIONS = ["md", "markdown", "txt", "html", "htm", "csv"]
DEFAULT_SEPARATORS = [
    "\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ",
    "，", ", ", " ",
]
_EXTENSION = re.compile(r"^[a-z0-9][a-z0-9+_-]{0,15}$")
_COLLECTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")


class TokenizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["qwen", "approximate"] = "qwen"
    model: str = "Qwen/Qwen3-Embedding-8B"
    path: str = ""
    local_files_only: bool = False


class ChunkTypeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["heading_hierarchy", "procedure", "qa_pair", "clause", "api_reference"]
    chunk_size_tokens: int = Field(ge=16, le=8192)
    chunk_overlap_tokens: int = Field(ge=0, le=4096)

    @model_validator(mode="after")
    def _overlap_is_smaller(self) -> Self:
        if self.chunk_overlap_tokens >= self.chunk_size_tokens:
            raise ValueError("chunk_overlap_tokens must be smaller than chunk_size_tokens")
        return self


def _default_chunk_types() -> dict[str, ChunkTypeConfig]:
    return {
        "technical": ChunkTypeConfig(strategy="heading_hierarchy", chunk_size_tokens=512, chunk_overlap_tokens=64),
        "operations": ChunkTypeConfig(strategy="procedure", chunk_size_tokens=512, chunk_overlap_tokens=64),
        "faq": ChunkTypeConfig(strategy="qa_pair", chunk_size_tokens=384, chunk_overlap_tokens=0),
        "security": ChunkTypeConfig(strategy="clause", chunk_size_tokens=512, chunk_overlap_tokens=64),
        "api": ChunkTypeConfig(strategy="api_reference", chunk_size_tokens=768, chunk_overlap_tokens=0),
    }


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokenizer: TokenizerConfig = Field(default_factory=TokenizerConfig)
    min_chunk_tokens: int = Field(default=32, ge=0, le=1024)
    max_chunk_tokens: int = Field(default=1024, ge=32, le=8192)
    separators: list[str] = Field(default_factory=lambda: list(DEFAULT_SEPARATORS))
    types: dict[str, ChunkTypeConfig] = Field(default_factory=_default_chunk_types)

    @field_validator("separators")
    @classmethod
    def _separators_are_usable(cls, value: list[str]) -> list[str]:
        cleaned = [item for item in value if item]
        if not cleaned:
            raise ValueError("chunking.separators must contain at least one non-empty separator")
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def _fill_type_strategies(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw_types = data.get("types")
        if not isinstance(raw_types, dict):
            return data
        patched = dict(raw_types)
        for key, value in raw_types.items():
            if isinstance(value, dict) and "strategy" not in value:
                expected = TYPE_STRATEGIES.get(KnowledgeBaseType(key))
                if expected is not None:
                    patched[key] = {**value, "strategy": expected}
        return {**data, "types": patched}

    @model_validator(mode="after")
    def _types_are_complete_and_valid(self) -> Self:
        unknown = set(self.types) - {item.value for item in KNOWLEDGE_BASE_TYPES}
        if unknown:
            raise ValueError(f"unknown knowledge base chunking types: {sorted(unknown)}")
        defaults = _default_chunk_types()
        for knowledge_type in KNOWLEDGE_BASE_TYPES:
            key = knowledge_type.value
            self.types.setdefault(key, defaults[key])
        for knowledge_type in KNOWLEDGE_BASE_TYPES:
            configured = self.types[knowledge_type.value]
            expected = TYPE_STRATEGIES[knowledge_type]
            if configured.strategy != expected:
                raise ValueError(
                    f"chunking.types.{knowledge_type.value}.strategy must be {expected!r}"
                )
            if configured.chunk_size_tokens > self.max_chunk_tokens:
                raise ValueError(
                    f"chunking.max_chunk_tokens must be >= chunking.types."
                    f"{knowledge_type.value}.chunk_size_tokens"
                )
            if self.min_chunk_tokens > configured.chunk_size_tokens:
                raise ValueError(
                    f"chunking.min_chunk_tokens must be <= chunking.types."
                    f"{knowledge_type.value}.chunk_size_tokens"
                )
        return self


class LocalMinerUConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = ""
    file_parse_path: str = "/v1/file_parse"
    api_key: str = Field(default="", repr=False)
    response_format: Literal["zip", "json"] = "zip"
    tier: str = "flash"
    ocr_mode: str = "txt"
    timeout_seconds: float = Field(default=600.0, gt=0)
    max_download_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)


class CloudMinerUConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    api_key: str = Field(default="", repr=False)
    model_version: str = "pipeline"
    language: str = "ch"
    enable_formula: bool = True
    enable_table: bool = True
    is_ocr: bool = False
    poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    timeout_seconds: float = Field(default=900.0, gt=0)
    max_download_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)


class ParserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["mineru"] = "mineru"
    mode: Literal["local", "cloud"] = "local"
    local: LocalMinerUConfig = Field(default_factory=LocalMinerUConfig)
    cloud: CloudMinerUConfig = Field(default_factory=CloudMinerUConfig)
    max_download_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible"] = "openai_compatible"
    protocol: Literal["openai", "tei"] = "openai"
    base_url: str = ""
    path: str = "/embeddings"
    api_key: str = Field(default="", repr=False)
    model: str = "Qwen/Qwen3-Embedding-8B"
    dimensions: int = Field(default=4096, ge=1, le=65536)
    max_input_tokens: int = Field(default=8192, ge=1, le=131072)
    batch_size: int = Field(default=8, ge=1, le=512)
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=8)
    send_dimensions: bool = False
    query_instruction: str = (
        "Given a question, retrieve passages from the user's knowledge base that answer it"
    )
    document_instruction: str = ""


class MilvusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["milvus"] = "milvus"
    uri: str = ""
    token: str = Field(default="", repr=False)
    db_name: str = ""
    collection: str = "myflow_knowledge"
    metric_type: Literal["COSINE", "IP", "L2"] = "COSINE"
    index_type: str = "AUTOINDEX"
    consistency_level: Literal["Strong", "Session", "Bounded", "Eventually"] = "Session"
    flush: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("collection")
    @classmethod
    def _valid_collection(cls, value: str) -> str:
        if not _COLLECTION.fullmatch(value):
            raise ValueError("vector_store.collection must match ^[A-Za-z_][A-Za-z0-9_]{0,254}$")
        return value


class KeywordRetrievalConfig(BaseModel):
    """BM25 sparse channel. The analyzer is baked into the collection schema."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    analyzer: Literal["chinese", "english", "standard"] = "chinese"
    bm25_k1: float = Field(default=1.2, gt=0, le=10)
    bm25_b: float = Field(default=0.75, ge=0, le=1)


class RerankConfig(BaseModel):
    """Cross-encoder rerank stage. Disabled unless the deployment configures an endpoint."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    protocol: Literal["cohere", "tei"] = "cohere"
    base_url: str = ""
    path: str = "/rerank"
    api_key: str = Field(default="", repr=False)
    model: str = "Qwen/Qwen3-Reranker-8B"
    max_document_chars: int = Field(default=4000, ge=200, le=100000)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=1, ge=0, le=5)


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(default=5, ge=1, le=50)
    candidate_k: int = Field(default=20, ge=1, le=200)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    score_threshold: float | None = None
    max_context_chars: int = Field(default=8000, ge=200, le=100000)
    keyword: KeywordRetrievalConfig = Field(default_factory=KeywordRetrievalConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)

    @model_validator(mode="after")
    def _candidate_covers_top(self) -> Self:
        if self.candidate_k < self.top_k:
            raise ValueError("retrieval.candidate_k must be >= retrieval.top_k")
        return self


class KnowledgeBaseConfig(BaseModel):
    """knowledge_base: block. Disabled by default; enabled requires configured endpoints."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    storage_dir: str = ".myflow/knowledge"
    allowed_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_EXTENSIONS))
    text_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_TEXT_EXTENSIONS))
    max_file_size_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_total_size_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)
    max_files_per_request: int = Field(default=10, ge=1, le=50)
    max_knowledge_bases_per_user: int = Field(default=50, ge=1, le=10000)
    max_documents_per_base: int = Field(default=200, ge=1, le=10000)
    max_ingest_concurrency: int = Field(default=2, ge=1, le=16)
    parse: ParserConfig = Field(default_factory=ParserConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector_store: MilvusConfig = Field(default_factory=MilvusConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)

    @field_validator("allowed_extensions", "text_extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            ext = item.strip().lower().lstrip(".")
            if not _EXTENSION.fullmatch(ext):
                raise ValueError(f"invalid file extension: {item!r}")
            if ext not in cleaned:
                cleaned.append(ext)
        if not cleaned:
            raise ValueError("knowledge_base extension lists must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _budgets_are_consistent(self) -> Self:
        if self.max_total_size_bytes < self.max_file_size_bytes:
            raise ValueError("knowledge_base.max_total_size_bytes must be >= max_file_size_bytes")
        extra_text = set(self.text_extensions) - set(self.allowed_extensions)
        if extra_text:
            raise ValueError(
                "knowledge_base.text_extensions must be a subset of allowed_extensions: "
                f"{sorted(extra_text)}"
            )
        if self.enabled:
            missing: list[str] = []
            if self.parse.mode == "local":
                if not self.parse.local.endpoint.strip():
                    missing.append("parse.local.endpoint")
            else:
                if not self.parse.cloud.base_url.strip():
                    missing.append("parse.cloud.base_url")
                if not self.parse.cloud.api_key.strip():
                    missing.append("parse.cloud.api_key")
            if not self.embedding.base_url.strip():
                missing.append("embedding.base_url")
            if not self.vector_store.uri.strip():
                missing.append("vector_store.uri")
            if self.retrieval.rerank.enabled:
                if not self.retrieval.rerank.base_url.strip():
                    missing.append("retrieval.rerank.base_url")
                if not self.retrieval.rerank.model.strip():
                    missing.append("retrieval.rerank.model")
            if missing:
                raise ValueError(
                    "knowledge_base.enabled=true requires configured service addresses: "
                    + ", ".join(missing)
                )
        return self

    def allows(self, filename: str) -> bool:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return bool(suffix) and suffix in self.allowed_extensions

    def is_text_file(self, filename: str) -> bool:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return suffix in self.text_extensions

    def type_config(self, knowledge_type: KnowledgeBaseType | str) -> ChunkTypeConfig:
        key = knowledge_type.value if isinstance(knowledge_type, KnowledgeBaseType) else knowledge_type
        return self.chunking.types[key]
