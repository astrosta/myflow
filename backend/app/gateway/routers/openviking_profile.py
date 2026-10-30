"""Profile API for the additive OpenViking memory adapter.

This router is not mounted in the default app yet. It mirrors the profile
contract and can replace the legacy memory profile route during cutover.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from agentkit.agents.memory.openviking.profile import ProfileConflict, ProfileService
from backend.app.gateway.deps import get_openviking_profile_service, require_scope
from backend.app.gateway.errors import InvalidMemoryProfile, ProfileConflictAPI
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/memory", tags=["openviking-memory"])


class ProfilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any]
    etag: str


class ProfileDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    etag: str


class ProfileResponse(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)
    etag: str
    updated_at: str


def _user_id(caller: RunCaller) -> str:
    return caller.user_id or "default"


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
) -> ProfileResponse:
    view = await service.get_snapshot(_user_id(caller))
    return ProfileResponse(fields=view.fields, etag=view.etag, updated_at=view.updated_at)


@router.patch("/profile", response_model=ProfileResponse)
async def patch_profile(
    request: Request,
    body: ProfilePatchRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
) -> ProfileResponse:
    try:
        view = await service.patch(_user_id(caller), body.fields, etag=body.etag)
    except ProfileConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    except ValueError as exc:
        raise InvalidMemoryProfile(str(exc)) from exc
    return ProfileResponse(fields=view.fields, etag=view.etag, updated_at=view.updated_at)


@router.delete("/profile", response_model=ProfileResponse)
async def clear_profile(
    request: Request,
    body: ProfileDeleteRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
) -> ProfileResponse:
    try:
        view = await service.clear(_user_id(caller), etag=body.etag)
    except ProfileConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    return ProfileResponse(fields=view.fields, etag=view.etag, updated_at=view.updated_at)


@router.delete("/profile/{field_path:path}", response_model=ProfileResponse)
async def delete_profile_field(
    request: Request,
    field_path: str,
    body: ProfileDeleteRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
) -> ProfileResponse:
    try:
        view = await service.delete_field(_user_id(caller), field_path, etag=body.etag)
    except ProfileConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    except ValueError as exc:
        raise InvalidMemoryProfile(str(exc)) from exc
    return ProfileResponse(fields=view.fields, etag=view.etag, updated_at=view.updated_at)


__all__ = ["router"]
