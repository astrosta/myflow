"""Memory management HTTP surface.

The page lists, previews, edits and deletes OpenViking memories. This router only
proxies protocol calls through the runtime facade: no memory content is stored in
MyFlow. The .abstract.md / .overview.md sidecars are read-only by construction
(see agentkit.agents.memory.openviking.library).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from agentkit.agents.memory.openviking.library import (
    InvalidMemoryURI,
    MEMORY_LIBRARY_TYPES,
    MEMORY_TYPE_LABELS,
    MemoryItemNotFound,
    MemoryUnavailable,
    MemoryWriteConflict,
)
from backend.app.gateway.deps import get_memory_library, require_scope
from backend.app.gateway.errors import (
    InvalidMemoryURIAPI,
    MemoryConflictAPI,
    MemoryNotFoundAPI,
    MemoryRequestInvalid,
    MemoryRuntimeUnavailable,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryTypeResponse(BaseModel):
    type: str
    label: str


class MemoryTypesResponse(BaseModel):
    items: list[MemoryTypeResponse]


class MemoryItemResponse(BaseModel):
    uri: str
    name: str
    folder: str
    memory_type: str
    abstract: str
    abstract_truncated: bool
    updated_at: str


class MemoryItemListResponse(BaseModel):
    type: str
    items: list[MemoryItemResponse]
    has_more: bool
    limit: int


class TocEntryResponse(BaseModel):
    kind: str
    title: str
    depth: int
    uri: str | None = None
    navigable: bool = False


class MemoryDetailResponse(BaseModel):
    uri: str
    folder: str
    memory_type: str
    name: str
    overview: str
    toc: list[TocEntryResponse]
    overview_missing: bool


class MemoryContentResponse(BaseModel):
    uri: str
    content: str
    sha256: str
    size_bytes: int


class MemoryContentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    content: str
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


def _user(caller: RunCaller) -> str:
    return caller.user_id or "default"


def _raise_mapped(exc: Exception) -> None:
    if isinstance(exc, InvalidMemoryURI):
        raise InvalidMemoryURIAPI(str(exc)) from exc
    if isinstance(exc, MemoryItemNotFound):
        raise MemoryNotFoundAPI("memory not found") from exc
    if isinstance(exc, MemoryWriteConflict):
        raise MemoryConflictAPI(
            "memory changed since it was read; refresh before saving"
        ) from exc
    if isinstance(exc, MemoryUnavailable):
        raise MemoryRuntimeUnavailable(str(exc)) from exc
    if isinstance(exc, ValueError):
        raise MemoryRequestInvalid(str(exc)) from exc
    raise exc


def _content_response(content: Any) -> MemoryContentResponse:
    return MemoryContentResponse(
        uri=content.uri,
        content=content.content,
        sha256=content.sha256,
        size_bytes=content.size_bytes,
    )


@router.get("/items/types", response_model=MemoryTypesResponse)
async def memory_types(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    library: Annotated[Any, Depends(get_memory_library)],
) -> MemoryTypesResponse:
    """The six manageable types; 503 when long-term memory is not enabled."""

    del request, caller, library
    return MemoryTypesResponse(
        items=[
            MemoryTypeResponse(type=memory_type, label=MEMORY_TYPE_LABELS[memory_type])
            for memory_type in MEMORY_LIBRARY_TYPES
        ]
    )


@router.get("/items", response_model=MemoryItemListResponse)
async def list_memory_items(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    library: Annotated[Any, Depends(get_memory_library)],
    type: Annotated[str, Query(min_length=1, max_length=32)] = "preferences",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MemoryItemListResponse:
    """Summaries only: one filter-only find per type, never full content."""

    if type not in MEMORY_LIBRARY_TYPES:
        raise InvalidMemoryURIAPI(f"unsupported memory type: {type}")
    try:
        page = await library.list_memory_items(_user(caller), type, limit=limit)
    except Exception as exc:  # noqa: BLE001 - mapped below
        _raise_mapped(exc)
        raise
    return MemoryItemListResponse(
        type=type,
        items=[
            MemoryItemResponse(
                uri=item.uri,
                name=item.name,
                folder=item.folder,
                memory_type=item.memory_type,
                abstract=item.abstract,
                abstract_truncated=item.abstract_truncated,
                updated_at=item.updated_at,
            )
            for item in page.items
        ],
        has_more=page.has_more,
        limit=page.limit,
    )


@router.get("/items/detail", response_model=MemoryDetailResponse)
async def memory_detail(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    library: Annotated[Any, Depends(get_memory_library)],
    uri: Annotated[str, Query(min_length=1, max_length=2048)],
) -> MemoryDetailResponse:
    """The .overview.md TOC for the memory folder (ls fallback when missing)."""

    del request
    try:
        detail = await library.memory_detail(_user(caller), uri)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)
        raise
    return MemoryDetailResponse(
        uri=detail.uri,
        folder=detail.folder,
        memory_type=detail.memory_type,
        name=detail.name,
        overview=detail.overview,
        toc=[
            TocEntryResponse(
                kind=entry.kind,
                title=entry.title,
                depth=entry.depth,
                uri=entry.uri,
                navigable=entry.navigable,
            )
            for entry in detail.toc
        ],
        overview_missing=detail.overview_missing,
    )


@router.get("/items/content", response_model=MemoryContentResponse)
async def read_memory_content(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    library: Annotated[Any, Depends(get_memory_library)],
    uri: Annotated[str, Query(min_length=1, max_length=2048)],
) -> MemoryContentResponse:
    """L2 body of one memory file plus the sha used for optimistic editing."""

    del request
    try:
        content = await library.read_memory_content(_user(caller), uri)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)
        raise
    return _content_response(content)


@router.put("/items/content", response_model=MemoryContentResponse)
async def update_memory_content(
    request: Request,
    body: MemoryContentUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    library: Annotated[Any, Depends(get_memory_library)],
) -> MemoryContentResponse:
    """Replace one memory file; sidecars are rejected before any write."""

    del request
    try:
        content = await library.update_memory_content(
            _user(caller),
            body.uri,
            body.content,
            expected_sha256=body.expected_sha256,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)
        raise
    return _content_response(content)


@router.delete("/items", status_code=204)
async def delete_memory_item(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    library: Annotated[Any, Depends(get_memory_library)],
    uri: Annotated[str, Query(min_length=1, max_length=2048)],
) -> Response:
    """Delete exactly one memory file; directories and sidecars are rejected."""

    del request
    try:
        await library.delete_memory_item(_user(caller), uri)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)
        raise
    return Response(status_code=204)


__all__ = ["router"]
