"""越权尝试的审计日志（M9 §2.16）。

**admin 不是数据超级用户**：能管平台，不能读别人的对话。管理接口的"全局查询"
只出现在迁移脚本与 CLI 里。

普通用户的 404 不记日志——thread id 可枚举，记了就是噪音；
admin 会话请求别人的 thread 得到 **404**（与普通用户完全一样）但日志里留一条，
它是合规审计要看的东西。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("backend.app.gateway.audit")

ADMIN_CROSS_USER_DENIED = "admin_cross_user_access_denied"


def log_admin_cross_user_denied(*, user_id: str | None, resource: str, resource_id: str) -> None:
    """记录**尝试**，不确认资源是否存在（那需要一次全局查询，§2.16 约束 1）。"""
    logger.warning(
        "%s user_id=%s resource=%s resource_id=%s",
        ADMIN_CROSS_USER_DENIED,
        user_id,
        resource,
        resource_id,
    )
