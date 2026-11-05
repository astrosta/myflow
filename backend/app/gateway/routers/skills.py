"""技能目录路由。prefix=/skills，create_app 再加 /api。

- GET /api/skills：内置技能 + 当前用户已生效的私有技能。没有公共目录，也不会
  返回其他用户的技能。
- PUT /api/skills/{skill_id}/preference：当前用户对已生效私有技能的启用开关，
  持久化在 skills.enabled，并同步向量投影。
- POST /api/skills/reload：admin 运维刷新快照/缓存，与审核/发布无关。

技能的创建、保存、扫描、生效、删除在 skill_drafts.py。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.app.gateway.deps import require_scope
from backend.app.gateway.models.request import SkillId, SkillPreferenceRequest
from backend.app.gateway.models.response import SkillReloadResponse, SkillResponse, SkillsListResponse
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.skills_catalog import (
    list_skills_for_api,
    reload_skills,
    runtime_for_request,
    set_skill_preference,
)

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillsListResponse)
async def list_skills(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],
) -> SkillsListResponse:
    """Return built-ins plus the caller's active private skills.

    Draft versions are intentionally absent: they become visible here only
    after the owner completes the security-scan -> activate flow.
    """
    skills = await list_skills_for_api(
        runtime=runtime_for_request(request),
        user_id=caller.user_id,
    )
    return SkillsListResponse(skills=skills)


# Must stay before /{skill_id} routes: "reload" is not a skill id.
@router.post("/reload", response_model=SkillReloadResponse)
async def reload_skill_routes(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> SkillReloadResponse:
    """Refresh this process and bump the catalog epoch for other workers."""
    del caller
    await reload_skills(runtime=runtime_for_request(request))
    return SkillReloadResponse()


@router.put("/{skill_id}/preference", response_model=SkillResponse)
async def set_preference(
    request: Request,
    skill_id: SkillId,
    body: SkillPreferenceRequest,
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],
) -> SkillResponse:
    """Only affects the caller; never touches skill content or another user."""
    return await set_skill_preference(
        runtime=runtime_for_request(request),
        user_id=caller.user_id,
        skill_id=skill_id,
        enabled=body.enabled,
    )


__all__ = ["router"]
