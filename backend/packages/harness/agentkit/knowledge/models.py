"""Knowledge base data shapes shared by providers, stores, and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DocumentStatus = Literal["queued", "parsing", "chunking", "embedding", "ready", "failed"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"ready", "failed"})
IN_PROGRESS_STATUSES: frozenset[str] = frozenset({"parsing", "chunking", "embedding"})
RetrievalMode = Literal["vector", "hybrid"]


@dataclass(slots=True)
class KnowledgeBase:
    id: str
    user_id: str
    name: str
    type: str
    description: str | None
    document_count: int
    chunk_count: int
    created_at: str
    updated_at: str


@dataclass(slots=True)
class KnowledgeDocument:
    id: str
    user_id: str
    knowledge_base_id: str
    filename: str
    stored_name: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ParsedDocument:
    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class KnowledgeChunk:
    index: int
    content: str
    section: str = ""
    page: int = 0
    #: Text sent to the dense embedding service. FAQ sets this to the question;
    #: an empty value means "use content".
    embedding_text: str = ""
    #: Set only for children produced by the parent-child strategy.
    parent_id: str | None = None
    #: Transient parent body carried from chunking to ingestion; never persisted
    #: in the child collection.
    parent_content: str = ""


@dataclass(slots=True)
class KnowledgeParentChunk:
    parent_id: str
    content: str
    section: str = ""
    page: int = 0


@dataclass(slots=True)
class SearchOutcome:
    """One search: what was asked, what mode actually ran, and the hits used for context."""

    query: str
    hits: list[SearchHit]
    truncated: bool = False
    keyword_query: str = ""
    retrieval_mode: RetrievalMode = "vector"
    reranked: bool = False


@dataclass(slots=True)
class SearchHit:
    """score is always the value the current search sorted by; the rest explain why."""

    chunk_id: str
    document_id: str
    filename: str
    section: str
    content: str
    score: float
    knowledge_base_id: str = ""
    knowledge_base_name: str = ""
    knowledge_base_type: str = ""
    chunk_index: int = 0
    page: int = 0
    parent_id: str = ""
    rrf_score: float | None = None
    rerank_score: float | None = None
