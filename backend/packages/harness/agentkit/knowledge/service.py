"""Knowledge base orchestration: CRUD, background ingestion, reindex, search."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from agentkit.config.runtime_paths import resolve_path
from agentkit.knowledge.chunking import chunk_document
from agentkit.knowledge.config import KnowledgeBaseConfig
from agentkit.knowledge.embedding import OpenAICompatibleEmbedder
from agentkit.knowledge.errors import (
    InvalidKnowledgeRequest,
    KeywordSearchUnavailable,
    KnowledgeBaseNotFound,
    KnowledgeBusy,
    KnowledgeDisabled,
    KnowledgeDocumentNotFound,
    KnowledgeLimitReached,
    KnowledgeUnavailable,
    KnowledgeUploadTooLarge,
)
from agentkit.knowledge.models import (
    KnowledgeBase,
    KnowledgeDocument,
    ParsedDocument,
    RetrievalMode,
    SearchHit,
    SearchOutcome,
)
from agentkit.knowledge.parsers.mineru import build_parser
from agentkit.knowledge.parsers.text import LocalTextParser
from agentkit.knowledge.rerank import RerankScore, Reranker, build_reranker
from agentkit.knowledge.store.base import KnowledgeStore
from agentkit.knowledge.store.memory import MemoryKnowledgeStore
from agentkit.knowledge.tokenizer import TokenCounter, build_token_counter
from agentkit.knowledge.types import KnowledgeBaseType, parse_type
from agentkit.knowledge.vector_store.milvus import MilvusVectorStore

logger = logging.getLogger(__name__)


class UploadStream(Protocol):
    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


_SAFE_FILENAME = re.compile(r"^[^/\\\x00]{1,255}$")


def _safe_error(exc: BaseException) -> str:
    from agentkit.knowledge.errors import KnowledgeError

    if isinstance(exc, KnowledgeError):
        return str(exc)[:500]
    return f"{type(exc).__name__}: 处理失败，请重试"


def _validate_filename(filename: str | None) -> str:
    if not filename or not _SAFE_FILENAME.match(filename) or filename in {".", ".."}:
        raise InvalidKnowledgeRequest("Invalid upload filename")
    return filename


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _validate_base_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise InvalidKnowledgeRequest("Knowledge base name must not be empty")
    if len(value) > 120:
        raise InvalidKnowledgeRequest("Knowledge base name must be at most 120 characters")
    return value


class KnowledgeService:
    def __init__(
        self,
        config: KnowledgeBaseConfig,
        *,
        store: KnowledgeStore,
        parser=None,
        text_parser=None,
        embedder=None,
        vector_store=None,
        reranker: Reranker | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._parser = parser or build_parser(config.parse)
        self._text_parser = text_parser or LocalTextParser()
        self._embedder = embedder or OpenAICompatibleEmbedder(config.embedding)
        self._vector_store = vector_store or MilvusVectorStore(
            config.vector_store,
            dimensions=config.embedding.dimensions,
            keyword_analyzer=config.retrieval.keyword.analyzer,
            bm25_k1=config.retrieval.keyword.bm25_k1,
            bm25_b=config.retrieval.keyword.bm25_b,
        )
        # None means the rerank stage is switched off in config; ordering stays RRF.
        self._reranker = reranker if reranker is not None else build_reranker(config.retrieval.rerank)
        self._token_counter = token_counter or build_token_counter(config.chunking.tokenizer).counter
        self._root = resolve_path(config.storage_dir)
        self._semaphore = asyncio.Semaphore(config.max_ingest_concurrency)
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False

    # ---- bases -----------------------------------------------------------

    async def create_base(
        self, *, user_id: str, name: str, knowledge_type: str | KnowledgeBaseType,
        description: str | None = None,
    ) -> KnowledgeBase:
        if not self._config.enabled:
            raise KnowledgeDisabled("Knowledge base is disabled")
        value = _validate_base_name(name)
        if await self._store.count_bases(user_id) >= self._config.max_knowledge_bases_per_user:
            raise KnowledgeLimitReached("Knowledge base limit reached")
        try:
            parsed_type = parse_type(knowledge_type)
        except ValueError as exc:
            raise InvalidKnowledgeRequest(f"Unknown knowledge base type: {knowledge_type}") from exc
        return await self._store.create_base(
            user_id=user_id, name=value, knowledge_type=parsed_type, description=description
        )

    async def list_bases(self, user_id: str) -> list[KnowledgeBase]:
        return await self._store.list_bases(user_id)

    async def get_base(self, user_id: str, base_id: str) -> KnowledgeBase:
        base = await self._store.get_base(user_id, base_id)
        if base is None:
            raise KnowledgeBaseNotFound("Knowledge base not found")
        return base

    async def update_base(
        self, *, user_id: str, base_id: str, name: str | None = None, description: str | None = None
    ) -> KnowledgeBase:
        await self.get_base(user_id, base_id)
        value = _validate_base_name(name) if name is not None else None
        return await self._store.update_base(
            user_id=user_id, base_id=base_id, name=value, description=description
        )

    async def delete_base(self, *, user_id: str, base_id: str) -> bool:
        base = await self._store.get_base(user_id, base_id)
        if base is None:
            return False
        documents = await self._store.list_documents(user_id, base_id)
        await self._cancel_tasks(doc.id for doc in documents)
        await self._vector_store.delete_base(user_id=user_id, knowledge_base_id=base_id)
        deleted = await self._store.delete_base(user_id, base_id)
        if deleted:
            for document in documents:
                self._unlink_document(document)
        return deleted

    # ---- documents -------------------------------------------------------

    async def list_documents(self, user_id: str, base_id: str) -> list[KnowledgeDocument]:
        await self.get_base(user_id, base_id)
        return await self._store.list_documents(user_id, base_id)

    async def get_document(self, user_id: str, document_id: str) -> KnowledgeDocument:
        document = await self._store.get_document(user_id, document_id)
        if document is None:
            raise KnowledgeDocumentNotFound("Document not found")
        return document

    async def add_documents(
        self, *, user_id: str, base_id: str, uploads: Sequence[UploadStream]
    ) -> list[KnowledgeDocument]:
        if not self._config.enabled:
            raise KnowledgeDisabled("Knowledge base is disabled")
        await self.get_base(user_id, base_id)
        if not uploads:
            raise InvalidKnowledgeRequest("At least one file is required")
        if len(uploads) > self._config.max_files_per_request:
            raise InvalidKnowledgeRequest("Too many files in one request")
        existing = await self._store.count_documents(user_id, base_id)
        if existing + len(uploads) > self._config.max_documents_per_base:
            raise KnowledgeLimitReached("This knowledge base has reached its document limit")

        created: list[KnowledgeDocument] = []
        total = 0
        try:
            for upload in uploads:
                filename = _validate_filename(upload.filename)
                if not self._config.allows(filename):
                    raise InvalidKnowledgeRequest(f"File type is not allowed: {_extension(filename) or 'unknown'}")
                document_id = uuid4().hex
                suffix = "." + _extension(filename) if _extension(filename) else ""
                stored_name = document_id + suffix
                target = self._document_path(user_id, base_id, stored_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                size = await self._write_upload(upload, target, remaining=self._config.max_total_size_bytes - total)
                total += size
                try:
                    document = await self._store.create_document(
                        document_id=document_id,
                        user_id=user_id,
                        knowledge_base_id=base_id,
                        filename=filename,
                        stored_name=stored_name,
                        mime_type=(
                            upload.content_type
                            or mimetypes.guess_type(filename)[0]
                            or "application/octet-stream"
                        )[:128],
                        size_bytes=size,
                    )
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                created.append(document)
                self._start(document)
            return created
        except Exception:
            for document in created:
                await self._cancel_tasks([document.id])
                try:
                    await self._store.delete_document(user_id, document.id)
                except Exception:
                    logger.debug("failed to roll back knowledge document", exc_info=True)
                self._unlink_document(document)
            raise

    async def reindex_document(self, *, user_id: str, document_id: str) -> KnowledgeDocument:
        self._ensure_idle(document_id)
        await self.get_document(user_id, document_id)
        await self._store.prepare_reindex(user_id, document_id)
        try:
            await self._vector_store.delete_document(user_id=user_id, document_id=document_id)
        except Exception as exc:
            await self._store.update_document_status(
                user_id, document_id, status="failed", chunk_count=0, error_message=_safe_error(exc)
            )
            raise
        refreshed = await self.get_document(user_id, document_id)
        self._start(refreshed)
        return refreshed

    async def delete_document(self, *, user_id: str, document_id: str) -> str:
        await self._cancel_tasks([document_id])
        document = await self.get_document(user_id, document_id)
        await self._vector_store.delete_document(user_id=user_id, document_id=document_id)
        await self._store.delete_document(user_id, document_id)
        self._unlink_document(document)
        return document_id

    async def wait_for_idle(self, timeout: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("knowledge ingestion did not finish")
            await asyncio.wait(list(self._tasks.values()), timeout=remaining)

    # ---- ingestion -------------------------------------------------------

    def _start(self, document: KnowledgeDocument) -> None:
        existing = self._tasks.get(document.id)
        if existing is not None and not existing.done():
            raise KnowledgeBusy("Document is already being processed")
        task = asyncio.create_task(self._ingest(document.id, document.user_id), name=f"knowledge:{document.id}")
        self._tasks[document.id] = task
        task.add_done_callback(lambda finished: self._tasks.pop(document.id, None) if self._tasks.get(document.id) is finished else None)

    async def _ingest(self, document_id: str, user_id: str) -> None:
        async with self._semaphore:
            try:
                await self._ingest_inner(document_id, user_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "knowledge ingestion failed document_id=%s error_type=%s",
                    document_id, type(exc).__name__, exc_info=True,
                )
                try:
                    await self._vector_store.delete_document(user_id=user_id, document_id=document_id)
                except Exception:
                    logger.debug("failed to clean partial vectors", exc_info=True)
                try:
                    await self._store.update_document_status(
                        user_id, document_id, status="failed", chunk_count=0, error_message=_safe_error(exc)
                    )
                except KnowledgeDocumentNotFound:
                    pass

    async def _ingest_inner(self, document_id: str, user_id: str) -> None:
        document = await self._store.get_document(user_id, document_id)
        if document is None:
            return
        base = await self._store.get_base(user_id, document.knowledge_base_id)
        if base is None:
            raise KnowledgeBaseNotFound("Knowledge base not found")
        path = self._document_path(user_id, document.knowledge_base_id, document.stored_name)
        if not path.is_file():
            raise InvalidKnowledgeRequest("Uploaded file is missing")

        await self._store.update_document_status(user_id, document_id, status="parsing")
        parsed: ParsedDocument
        if self._config.is_text_file(document.filename):
            parsed = await self._text_parser.parse(path, filename=document.filename)
        else:
            parsed = await self._parser.parse(path, filename=document.filename)

        from agentkit.knowledge.text import clean_text

        cleaned = clean_text(parsed.markdown)
        if len(cleaned) < 20:
            raise InvalidKnowledgeRequest("No usable text was extracted from this document")

        await self._store.update_document_status(user_id, document_id, status="chunking")
        chunks = chunk_document(
            cleaned, base.type, self._config, token_counter=self._token_counter
        )
        if not chunks:
            raise InvalidKnowledgeRequest("Chunking produced no content")

        await self._store.update_document_status(user_id, document_id, status="embedding")
        vectors = await self._embedder.embed_documents([chunk.content for chunk in chunks])
        await self._vector_store.upsert_document(
            user_id=user_id,
            knowledge_base_id=base.id,
            knowledge_base_name=base.name,
            knowledge_base_type=base.type,
            document=document,
            chunks=chunks,
            vectors=vectors,
        )
        current = await self._store.get_document(user_id, document_id)
        if current is None:
            await self._vector_store.delete_document(user_id=user_id, document_id=document_id)
            return
        await self._store.update_document_status(
            user_id, document_id, status="ready", chunk_count=len(chunks), error_message=None
        )

    # ---- search ----------------------------------------------------------

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        keyword_query: str | None = None,
        knowledge_base_type: str | KnowledgeBaseType | None = None,
        knowledge_base_name: str | None = None,
        base_ids: Sequence[str] | None = None,
        top_k: int | None = None,
    ) -> SearchOutcome:
        """Dense + BM25 recall, RRF fusion, optional rerank. See design doc §7.4.

        query drives the vector channel and the rerank query; keyword_query drives BM25.
        Both are model input only: identity still comes from the caller.
        """

        text = query.strip()
        keyword = (keyword_query or "").strip()
        if not text and not keyword:
            raise InvalidKnowledgeRequest("Search query must not be empty")
        # A keyword-only call still needs a dense vector, so the keyword string doubles as text.
        text = text or keyword
        keyword = keyword or text
        bases = await self._store.list_bases(user_id)
        selected_type: KnowledgeBaseType | None = None
        if knowledge_base_type is not None:
            try:
                selected_type = parse_type(knowledge_base_type)
            except ValueError as exc:
                raise InvalidKnowledgeRequest(
                    f"Unknown knowledge base type: {knowledge_base_type}"
                ) from exc
            bases = [base for base in bases if base.type == selected_type.value]
        if knowledge_base_name:
            wanted = knowledge_base_name.strip().casefold()
            bases = [base for base in bases if base.name.casefold() == wanted]
        if base_ids is not None:
            wanted_ids = set(base_ids)
            bases = [base for base in bases if base.id in wanted_ids]
        if not bases:
            return SearchOutcome(
                query=text, hits=[], truncated=False, keyword_query=keyword
            )
        base_ids = [base.id for base in bases]
        limit = max(1, min(top_k or self._config.retrieval.top_k, 10))
        candidates = max(limit, self._config.retrieval.candidate_k)
        vector = await self._embedder.embed_query(text)
        hits, retrieval_mode = await self._recall(
            user_id=user_id,
            vector=vector,
            keyword=keyword,
            base_ids=base_ids,
            candidates=candidates,
        )
        hits, reranked = await self._rerank(text, hits)
        limited = hits[:limit]
        remaining = self._config.retrieval.max_context_chars
        output: list[SearchHit] = []
        truncated = False
        for hit in limited:
            if remaining <= 0:
                truncated = True
                break
            content = hit.content
            if len(content) > remaining:
                content = content[:remaining]
                truncated = True
            output.append(
                SearchHit(
                    chunk_id=hit.chunk_id, document_id=hit.document_id, filename=hit.filename,
                    section=hit.section, content=content, score=hit.score,
                    knowledge_base_id=hit.knowledge_base_id,
                    knowledge_base_name=hit.knowledge_base_name,
                    knowledge_base_type=hit.knowledge_base_type,
                    chunk_index=hit.chunk_index, page=hit.page,
                    rrf_score=hit.rrf_score, rerank_score=hit.rerank_score,
                )
            )
            remaining -= len(content)
        return SearchOutcome(
            query=text,
            hits=output,
            truncated=truncated,
            keyword_query=keyword,
            retrieval_mode=retrieval_mode,
            reranked=reranked,
        )

    async def _recall(
        self,
        *,
        user_id: str,
        vector: Sequence[float],
        keyword: str,
        base_ids: Sequence[str],
        candidates: int,
    ) -> tuple[list[SearchHit], RetrievalMode]:
        """Two-channel recall with an explicit fallback when BM25 is not available."""

        keyword_config = self._config.retrieval.keyword
        if keyword_config.enabled and keyword:
            try:
                hits = await self._vector_store.hybrid_search(
                    user_id=user_id,
                    vector=vector,
                    keyword_query=keyword,
                    base_ids=base_ids,
                    limit=candidates,
                    rrf_k=self._config.retrieval.rrf_k,
                )
            except KeywordSearchUnavailable as exc:
                logger.warning(
                    "knowledge keyword channel unavailable, falling back to dense only: %s", exc
                )
            else:
                return hits, "hybrid"
        hits = await self._vector_store.search(
            user_id=user_id,
            vector=vector,
            base_ids=base_ids,
            limit=candidates,
            score_threshold=self._config.retrieval.score_threshold,
        )
        return hits, "vector"

    async def _rerank(
        self, query: str, hits: list[SearchHit]
    ) -> tuple[list[SearchHit], bool]:
        """Rerank is a precision boost: any failure keeps the fusion order and says so."""

        if self._reranker is None or len(hits) < 2:
            return hits, False
        try:
            scores = await self._reranker.rerank(
                query=query, documents=[hit.content for hit in hits]
            )
        except KnowledgeUnavailable as exc:
            logger.warning("knowledge rerank failed, keeping fusion order: %s", exc)
            return hits, False
        except Exception:
            logger.warning("knowledge rerank failed unexpectedly", exc_info=True)
            return hits, False
        ordered = self._apply_rerank(hits, scores)
        if not ordered:
            return hits, False
        return ordered, True

    @staticmethod
    def _apply_rerank(hits: list[SearchHit], scores: Sequence[RerankScore]) -> list[SearchHit]:
        ordered: list[SearchHit] = []
        for item in scores:
            if item.index < 0 or item.index >= len(hits):
                continue
            ordered.append(
                replace(hits[item.index], score=item.score, rerank_score=item.score)
            )
        return ordered

    # ---- lifecycle / helpers --------------------------------------------

    async def recover_interrupted(self) -> int:
        interrupted = await self._store.list_interrupted()
        for document in interrupted:
            try:
                await self._vector_store.delete_document(
                    user_id=document.user_id, document_id=document.id
                )
            except Exception:
                logger.debug("failed to clean interrupted vectors", exc_info=True)
        return await self._store.mark_interrupted_failed()

    async def aclose(self) -> None:
        self._closed = True
        await self._cancel_tasks(list(self._tasks))
        for component in (
            self._parser, self._text_parser, self._embedder, self._vector_store, self._reranker
        ):
            close = getattr(component, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.debug("knowledge component close failed", exc_info=True)

    def _document_path(self, user_id: str, base_id: str, stored_name: str) -> Path:
        user = re.sub(r"[^A-Za-z0-9_-]", "-", user_id or "default")
        return self._root / "users" / user / "knowledge_bases" / base_id / "documents" / stored_name

    async def _write_upload(self, upload: UploadStream, target: Path, *, remaining: int) -> int:
        limit = min(self._config.max_file_size_bytes, remaining)
        if limit <= 0:
            raise KnowledgeUploadTooLarge("Upload exceeds the size limit")
        part = target.with_name(target.name + ".part")
        size = 0
        try:
            with part.open("wb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise KnowledgeUploadTooLarge("Upload exceeds the size limit")
                    await asyncio.to_thread(handle.write, chunk)
            if size == 0:
                raise InvalidKnowledgeRequest("Empty files are not allowed")
            part.replace(target)
            return size
        except Exception:
            part.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

    async def _cancel_tasks(self, document_ids) -> None:
        tasks = []
        for document_id in list(document_ids):
            task = self._tasks.pop(document_id, None)
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("knowledge task ended with an error", exc_info=True)

    def _ensure_idle(self, document_id: str) -> None:
        task = self._tasks.get(document_id)
        if task is not None and not task.done():
            raise KnowledgeBusy("Document is already being processed")

    def _unlink_document(self, document: KnowledgeDocument) -> None:
        path = self._document_path(document.user_id, document.knowledge_base_id, document.stored_name)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("failed to remove knowledge file", exc_info=True)

    def status(self) -> dict[str, Any]:
        return status_from_config(self._config)


def status_from_config(config: KnowledgeBaseConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "parser": config.parse.provider,
        "parser_mode": config.parse.mode,
        "embedding_model": config.embedding.model,
        "embedding_dimensions": config.embedding.dimensions,
        "vector_store": config.vector_store.provider,
        "collection": config.vector_store.collection,
        "allowed_extensions": list(config.allowed_extensions),
        "text_extensions": list(config.text_extensions),
        "max_file_size_bytes": config.max_file_size_bytes,
        "max_total_size_bytes": config.max_total_size_bytes,
        "max_files_per_request": config.max_files_per_request,
        "max_documents_per_base": config.max_documents_per_base,
        "retrieval": {
            "mode": "hybrid" if config.retrieval.keyword.enabled else "vector",
            "keyword_enabled": config.retrieval.keyword.enabled,
            "keyword_analyzer": config.retrieval.keyword.analyzer,
            "rrf_k": config.retrieval.rrf_k,
            "rerank_enabled": config.retrieval.rerank.enabled,
            "rerank_model": config.retrieval.rerank.model if config.retrieval.rerank.enabled else "",
        },
        "types": [
            {
                "value": item.value,
                "strategy": config.type_config(item).strategy,
                "chunk_size_tokens": config.type_config(item).chunk_size_tokens,
                "chunk_overlap_tokens": config.type_config(item).chunk_overlap_tokens,
            }
            for item in KnowledgeBaseType
        ],
    }


_service: KnowledgeService | None = None


def make_knowledge_store() -> KnowledgeStore:
    from agentkit.knowledge.store.sql import SqlKnowledgeStore
    from agentkit.persistence.engine import get_session_factory

    factory = get_session_factory()
    return MemoryKnowledgeStore() if factory is None else SqlKnowledgeStore(factory)


def build_knowledge_service(
    config: KnowledgeBaseConfig, *, store: KnowledgeStore | None = None
) -> KnowledgeService:
    return KnowledgeService(config, store=store or make_knowledge_store())


async def configure_knowledge_service(
    config: KnowledgeBaseConfig, *, store: KnowledgeStore | None = None
) -> KnowledgeService | None:
    global _service
    if not config.enabled:
        if _service is not None:
            await _service.aclose()
            _service = None
        return None
    if _service is not None:
        await _service.aclose()
    _service = build_knowledge_service(config, store=store)
    await _service.recover_interrupted()
    return _service


def get_knowledge_service() -> KnowledgeService:
    if _service is None:
        raise KnowledgeDisabled("Knowledge base is disabled or not initialized")
    return _service


async def reset_knowledge_service() -> None:
    global _service
    if _service is not None:
        await _service.aclose()
    _service = None
