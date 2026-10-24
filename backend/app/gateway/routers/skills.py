"""技能路由（M7 §2.4 / M11 Task 15）。prefix=/skills，create_app 再加 /api。

- GET /api/skills：任何登录用户（skills:read）。斜杠菜单与设置页共用。
  来源决定数据从哪里来：filesystem 扫盘；git 读进程级快照（公共 + 本人私有）。
- PUT /api/skills/{skill_id}/preference：**普通用户唯一的写操作**——开关"我用不用"。
  只写 skills_prefs.json；git 来源的键是内部 skill_id。
- PATCH /api/skills/{skill_id}：admin 的**全局**开关，动所有人。
  git 来源写 PostgreSQL 的 skill_settings + outbox，然后刷新快照。
- POST /api/skills/reload：admin。git 来源重建快照，两种来源都清 prompt 段落 LRU。

Git 草稿的作者路由在 skill_drafts.py（create_app 里先于本 router 注册，
/skills/mine 不会被 /skills/{skill_id} 捕获）；本模块仍不声明创建路由。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from backend.app.gateway.deps import require_scope
from backend.app.gateway.models.request import (
    SkillId,
    SkillPreferenceRequest,
    SkillUpdateRequest,
)
from backend.app.gateway.models.response import SkillReloadResponse, SkillResponse, SkillsListResponse
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.services import RunPermissionDenied
from backend.app.gateway.skills_catalog import (
    list_skills_for_api,
    reload_skills,
    runtime_for_request,
    set_global_skill_enabled,
    set_skill_preference,
)

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillsListResponse)
async def list_skills(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],
    scope: Annotated[Literal["effective", "global"], Query()] = "effective",
) -> SkillsListResponse:
    """默认给**有效状态**；admin 传 ?scope=global 才额外看到全局值。"""
    if scope == "global" and not caller.allows("admin"):
        raise RunPermissionDenied("missing scope: admin")
    skills = await list_skills_for_api(
        runtime=runtime_for_request(request),
        user_id=caller.user_id,
        include_global=scope == "global",
        is_admin=caller.allows("admin"),
    )
    return SkillsListResponse(skills=skills)


# 必须写在 PATCH /{skill_id} 前面：反了之后一旦再加 GET /skills/{skill_id}，reload 会被当成技能 id。
@router.post("/reload", response_model=SkillReloadResponse)
async def reload_skill_routes(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> SkillReloadResponse:
    """只刷新本进程；别的 worker 要各自打（§2.4）或等下一次刷新。"""
    del caller
    await reload_skills(runtime=runtime_for_request(request))
    return SkillReloadResponse()


@router.put("/{skill_id}/preference", response_model=SkillResponse)
async def set_preference(
    request: Request,
    skill_id: SkillId,
    body: SkillPreferenceRequest,
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],  # ★ 普通用户
) -> SkillResponse:
    """唯一对普通用户开放的写操作：只影响自己，不碰技能正文，也不 reload 全局配置。"""
    return await set_skill_preference(
        runtime=runtime_for_request(request),
        user_id=caller.user_id,
        skill_id=skill_id,
        enabled=body.enabled,
    )


@router.patch("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    request: Request,
    skill_id: SkillId,
    body: SkillUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],  # ★ 全局开关
) -> SkillResponse:
    """全局开关：PG 事务（setting + outbox + audit）之后刷新快照并清索引缓存。"""
    return await set_global_skill_enabled(
        runtime=runtime_for_request(request),
        skill_id=skill_id,
        enabled=body.enabled,
        actor=caller.user_id,
    )
