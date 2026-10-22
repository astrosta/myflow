"""Knowledge domain errors. The gateway maps these to its uniform error body."""

from __future__ import annotations

from typing import ClassVar


class KnowledgeError(Exception):
    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "knowledge_error"


class KnowledgeDisabled(KnowledgeError):
    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "knowledge_disabled"


class KnowledgeUnavailable(KnowledgeError):
    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "knowledge_unavailable"


class KeywordSearchUnavailable(KnowledgeUnavailable):
    """The collection predates the BM25 sparse field; search degrades to dense only."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "keyword_search_unavailable"


class InvalidKnowledgeRequest(KnowledgeError):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_knowledge_request"


class KnowledgeBaseNotFound(KnowledgeError):
    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "knowledge_base_not_found"


class KnowledgeDocumentNotFound(KnowledgeError):
    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "knowledge_document_not_found"


class KnowledgeNameTaken(KnowledgeError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "knowledge_name_taken"


class KnowledgeLimitReached(KnowledgeError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "knowledge_limit_reached"


class KnowledgeUploadTooLarge(KnowledgeError):
    status_code: ClassVar[int] = 413
    code: ClassVar[str] = "knowledge_upload_too_large"


class KnowledgeBusy(KnowledgeError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "knowledge_busy"


class KnowledgeSearchFailed(KnowledgeError):
    status_code: ClassVar[int] = 502
    code: ClassVar[str] = "knowledge_search_failed"


class KnowledgeTypeImmutable(KnowledgeError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "knowledge_type_immutable"
