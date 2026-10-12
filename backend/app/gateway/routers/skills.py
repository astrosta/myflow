"""技能路由（M7 §2.4）。prefix=/skills，create_app 再加 /api。

- GET /api/skills：任何登录用户（skills:read）。斜杠菜单与设置页共用。
- PUT /api/skills/{name}/preference：**普通用户在 M7 里唯一的写操作**——开关"我用不用"。
- PATCH /api/skills/{name}：admin 的**全局**开关，动所有人。
- POST /api/skills/reload：admin，只清本进程的 prompt 段落 LRU。

M7 没有"创建"类路由：POST /api/skills 是 404（不是 403）——创建能力确实没实现，
而不是"你没权限"。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from backend.app.gateway.deps import require_scope
from backend.app.gateway.models.request import (
    SkillName,
    SkillPreferenceRequest,
    SkillUpdateRequest,
)
from backend.app.gateway.models.response import SkillReloadResponse, SkillResponse, SkillsListResponse
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.services import RunPermissionDenied
from backend.app.gateway.skills_catalog import (
    patch_skill_enabled,
    reload_skill_caches,
    scan_skills_for_api,
    set_user_skill_pref,
)

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillsListResponse)
async def list_skills(
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],
    scope: Annotated[Literal["effective", "global"], Query()] = "effective",
) -> SkillsListResponse:
    """默认给**有效状态**；admin 传 ?scope=global 才额外看到全局值。"""
    if scope == "global" and not caller.allows("admin"):
        raise RunPermissionDenied("missing scope: admin")
    include_global = scope == "global"
    skills = await asyncio.to_thread(
        scan_skills_for_api, user_id=caller.user_id, include_global=include_global
    )
    return SkillsListResponse(skills=skills)


# 必须写在 PATCH /{name} 前面：反了之后一旦再加 GET /skills/{name}，reload 会被当成技能名。
@router.post("/reload", response_model=SkillReloadResponse)
async def reload_skills(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> SkillReloadResponse:
    """只清本进程；别的 worker 要各自打（§2.4）。"""
    del caller
    await asyncio.to_thread(reload_skill_caches)
    return SkillReloadResponse()


@router.put("/{name}/preference", response_model=SkillResponse)
async def set_skill_preference(
    name: SkillName,
    body: SkillPreferenceRequest,
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],  # ★ 普通用户
) -> SkillResponse:
    """唯一对普通用户开放的写操作：只影响自己，不碰技能正文，也不 reload 全局配置。"""
    return await asyncio.to_thread(set_user_skill_pref, caller.user_id, name, body.enabled)


@router.patch("/{name}", response_model=SkillResponse)
async def update_skill(
    name: SkillName,
    body: SkillUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],  # ★ 全局开关
) -> SkillResponse:
    """读-改-写-重载整个循环在 worker 线程里、持双锁完成（§2.2）。"""
    del caller
    return await asyncio.to_thread(patch_skill_enabled, name, body.enabled)
