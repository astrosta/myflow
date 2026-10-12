"""MCP 配置路由（M7 §2.4）。prefix=/mcp，create_app 再加 /api。

读改写都在 asyncio.to_thread 里完成，锁在**那个线程里**拿（§2.2）。这个文件里不出现
extensions_config_write_lock，也不直接 open(extensions_config.json)——磁盘、打码、
双锁全在 mcp_wire.py 和 agentkit.config.extensions_config。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends

from agentkit.mcp.cache import reset_local_mcp_cache
from backend.app.gateway.deps import require_scope
from backend.app.gateway.mcp_wire import (
    mask_mcp_config,
    read_extensions_config,
    replace_mcp_servers,
    set_mcp_server_enabled,
)
from backend.app.gateway.models.request import McpConfigUpdateRequest, McpServerEnabledPatch
from backend.app.gateway.models.response import McpCacheResetResponse, McpConfigResponse
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/config", response_model=McpConfigResponse)
async def get_mcp_config(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> McpConfigResponse:
    """打码之后的形状仍是运维配置，所以是 admin，不是 skills:read。"""
    del caller
    raw = await asyncio.to_thread(read_extensions_config)
    return McpConfigResponse.model_validate(mask_mcp_config(raw))


# ⚠ 这里**故意没有** PUT /config：整份替换 = 新建 + 删除服务器，属于"后续阶段的创建
#   动作"，需要业务管理员（能力位 mcp:create）。M7 只提供"已有服务器的启停"——注册一个
#   拒绝一切请求的 PUT 反而是误导：路由不存在比 403 更诚实。


@router.patch("/config", response_model=McpConfigResponse)
async def patch_mcp_server(
    body: McpServerEnabledPatch,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> McpConfigResponse:
    del caller
    saved = await asyncio.to_thread(set_mcp_server_enabled, body.name, body.enabled)
    return McpConfigResponse.model_validate(mask_mcp_config(saved))


@router.post("/cache/reset", response_model=McpCacheResetResponse)
async def reset_mcp_cache(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> McpCacheResetResponse:
    """只清本进程；多 worker 要逐个打，或者等内容签名自己失效（§2.3）。"""
    del caller
    await asyncio.to_thread(reset_local_mcp_cache)
    return McpCacheResetResponse()