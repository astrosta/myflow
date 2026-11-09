"""Knowledge base HTTP surface. Ownership always comes from the auth middleware."""

from __future__ import annotations

from typing import Annotated

from agentkit.knowledge.citations import citations_for_hits
from agentkit.knowledge.errors import (
    InvalidKnowledgeRequest,
    KnowledgeBaseNotFound,
    KnowledgeDisabled,
    KnowledgeDocumentNotFound,
)
from agentkit.knowledge.service import status_from_config
from fastapi import APIRouter, Depends, File, Path, Query, Request, UploadFile

from backend.app.gateway.deps import (
    get_knowledge_config,
    get_knowledge_service,
    get_knowledge_service_optional,
    require_scope,
)
from backend.app.gateway.models.request import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseUpdateRequest,
    KnowledgeTypeLiteral,
)
from backend.app.gateway.models.response import (
    KnowledgeBaseListResponse,
    KnowledgeBaseResponse,
    KnowledgeChunkResponse,
    KnowledgeChunksResponse,
    KnowledgeCitationResponse,
    KnowledgeDeletedResponse,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentResponse,
    KnowledgeSearchHitResponse,
    KnowledgeSearchResponse,
    KnowledgeStatusResponse,
    KnowledgeUploadResponse,
    knowledge_base_to_wire,
    knowledge_document_to_wire,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
KnowledgeId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


def _user(caller: RunCaller) -> str:
    if not caller.user_id:
        raise KnowledgeDisabled("Knowledge base requires an authenticated user")
    return caller.user_id


@router.get("/status", response_model=KnowledgeStatusResponse)
async def status(
    request: Request,
    _caller: Annotated[RunCaller, Depends(require_scope("knowledge:read"))],
) -> KnowledgeStatusResponse:
    service = get_knowledge_service_optional(request)
    if service is not None:
        data = service.status()
    else:
        data = status_from_config(get_knowledge_config(request))
    return KnowledgeStatusResponse.model_validate(data)


@router.get("/bases", response_model=KnowledgeBaseListResponse)
async def list_bases(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:read"))],
) -> KnowledgeBaseListResponse:
    service = get_knowledge_service(request)
    bases = await service.list_bases(_user(caller))
    return KnowledgeBaseListResponse(
        bases=[knowledge_base_to_wire(item) for item in bases], count=len(bases)
    )


@router.post("/bases", status_code=201, response_model=KnowledgeBaseResponse)
async def create_base(
    payload: KnowledgeBaseCreateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:write"))],
) -> KnowledgeBaseResponse:
    service = get_knowledge_service(request)
    base = await service.create_base(
        user_id=_user(caller),
        name=payload.name,
        knowledge_type=payload.type,
        description=payload.description,
    )
    return knowledge_base_to_wire(base)


@router.get("/bases/{base_id}", response_model=KnowledgeBaseResponse)
async def get_base(
    base_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:read"))],
) -> KnowledgeBaseResponse:
    service = get_knowledge_service(request)
    return knowledge_base_to_wire(await service.get_base(_user(caller), base_id))


@router.patch("/bases/{base_id}", response_model=KnowledgeBaseResponse)
async def update_base(
    base_id: KnowledgeId,
    payload: KnowledgeBaseUpdateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:write"))],
) -> KnowledgeBaseResponse:
    service = get_knowledge_service(request)
    base = await service.update_base(
        user_id=_user(caller), base_id=base_id, name=payload.name, description=payload.description
    )
    return knowledge_base_to_wire(base)


@router.delete("/bases/{base_id}", response_model=KnowledgeDeletedResponse)
async def delete_base(
    base_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:write"))],
) -> KnowledgeDeletedResponse:
    service = get_knowledge_service(request)
    deleted = await service.delete_base(user_id=_user(caller), base_id=base_id)
    if not deleted:
        raise KnowledgeBaseNotFound("Knowledge base not found")
    return KnowledgeDeletedResponse(deleted=base_id)


@router.get("/bases/{base_id}/documents", response_model=KnowledgeDocumentListResponse)
async def list_documents(
    base_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:read"))],
) -> KnowledgeDocumentListResponse:
    service = get_knowledge_service(request)
    documents = await service.list_documents(_user(caller), base_id)
    return KnowledgeDocumentListResponse(
        documents=[knowledge_document_to_wire(item) for item in documents],
        count=len(documents),
    )


@router.post(
    "/bases/{base_id}/documents", status_code=202, response_model=KnowledgeUploadResponse
)
async def upload_documents(
    base_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:write"))],
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> KnowledgeUploadResponse:
    if not files:
        raise InvalidKnowledgeRequest("At least one file is required")
    service = get_knowledge_service(request)
    documents = await service.add_documents(
        user_id=_user(caller), base_id=base_id, uploads=files
    )
    return KnowledgeUploadResponse(
        documents=[knowledge_document_to_wire(item) for item in documents],
        count=len(documents),
    )


@router.get(
    "/bases/{base_id}/documents/{document_id}", response_model=KnowledgeDocumentResponse
)
async def get_document(
    base_id: KnowledgeId,
    document_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:read"))],
) -> KnowledgeDocumentResponse:
    service = get_knowledge_service(request)
    document = await service.get_document(_user(caller), document_id)
    if document.knowledge_base_id != base_id:
        from agentkit.knowledge.errors import KnowledgeDocumentNotFound

        raise KnowledgeDocumentNotFound("Document not found")
    return knowledge_document_to_wire(document)


@router.get(
    "/bases/{base_id}/documents/{document_id}/chunks",
    response_model=KnowledgeChunksResponse,
)
async def list_document_chunks(
    base_id: KnowledgeId,
    document_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:read"))],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeChunksResponse:
    """One page of chunks for the document detail page.

    Chunk bodies live in the vector store; metadata and ownership still come
    from the SQL catalog.  The response is sorted by chunk_index regardless of
    the vector store's row order.
    """
    service = get_knowledge_service(request)
    document = await service.get_document(_user(caller), document_id)
    if document.knowledge_base_id != base_id:
        raise KnowledgeDocumentNotFound("Document not found")
    chunks, total = await service.list_chunks(
        _user(caller), document_id, offset=offset, limit=limit
    )
    end = offset + len(chunks)
    return KnowledgeChunksResponse(
        document_id=document.id,
        chunks=[
            KnowledgeChunkResponse(
                chunk_id=f"{document.id}:{chunk.index}",
                chunk_index=chunk.index,
                section=chunk.section,
                page=chunk.page,
                content=chunk.content,
                parent_id=chunk.parent_id or "",
            )
            for chunk in chunks
        ],
        count=total,
        next_offset=end if end < total else None,
    )


@router.post(
    "/bases/{base_id}/documents/{document_id}/reindex",
    status_code=202,
    response_model=KnowledgeDocumentResponse,
)
async def reindex_document(
    base_id: KnowledgeId,
    document_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:write"))],
) -> KnowledgeDocumentResponse:
    service = get_knowledge_service(request)
    document = await service.get_document(_user(caller), document_id)
    if document.knowledge_base_id != base_id:
        from agentkit.knowledge.errors import KnowledgeDocumentNotFound

        raise KnowledgeDocumentNotFound("Document not found")
    refreshed = await service.reindex_document(user_id=_user(caller), document_id=document_id)
    return knowledge_document_to_wire(refreshed)


@router.delete(
    "/bases/{base_id}/documents/{document_id}", response_model=KnowledgeDeletedResponse
)
async def delete_document(
    base_id: KnowledgeId,
    document_id: KnowledgeId,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:write"))],
) -> KnowledgeDeletedResponse:
    service = get_knowledge_service(request)
    document = await service.get_document(_user(caller), document_id)
    if document.knowledge_base_id != base_id:
        from agentkit.knowledge.errors import KnowledgeDocumentNotFound

        raise KnowledgeDocumentNotFound("Document not found")
    deleted = await service.delete_document(user_id=_user(caller), document_id=document_id)
    return KnowledgeDeletedResponse(deleted=deleted)


@router.get("/search", response_model=KnowledgeSearchResponse)
async def search(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("knowledge:read"))],
    q: Annotated[str, Query(min_length=1, max_length=2000)],
    keyword: Annotated[str | None, Query(max_length=2000)] = None,
    knowledge_type: Annotated[KnowledgeTypeLiteral | None, Query(alias="type")] = None,
    base_id: Annotated[KnowledgeId | None, Query()] = None,
    top_k: Annotated[int | None, Query(ge=1, le=10)] = None,
) -> KnowledgeSearchResponse:
    service = get_knowledge_service(request)
    outcome = await service.search(
        user_id=_user(caller),
        query=q,
        keyword_query=keyword,
        knowledge_base_type=knowledge_type,
        base_ids=[base_id] if base_id else None,
        top_k=top_k,
    )
    sources = citations_for_hits(outcome.hits)
    citation_ids = {item["chunk_id"]: item["citation_id"] for item in sources}
    return KnowledgeSearchResponse(
        query=outcome.query,
        keyword_query=outcome.keyword_query,
        retrieval_mode=outcome.retrieval_mode,
        reranked=outcome.reranked,
        citations=[KnowledgeCitationResponse(**item) for item in sources],
        results=[
            KnowledgeSearchHitResponse(
                chunk_id=hit.chunk_id,
                citation_id=citation_ids.get(hit.chunk_id, ""),
                document_id=hit.document_id,
                filename=hit.filename,
                section=hit.section,
                page=hit.page,
                chunk_index=hit.chunk_index,
                parent_id=hit.parent_id,
                score=hit.score,
                knowledge_base_id=hit.knowledge_base_id,
                knowledge_base_name=hit.knowledge_base_name,
                knowledge_base_type=hit.knowledge_base_type,
                content=hit.content,
                rrf_score=hit.rrf_score,
                rerank_score=hit.rerank_score,
            )
            for hit in outcome.hits
        ],
        truncated=outcome.truncated,
    )
