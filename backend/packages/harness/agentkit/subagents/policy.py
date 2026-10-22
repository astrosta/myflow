"""一次 run 的委派策略快照（M8 §16）。

一个 lead agent 可以声明 ``allowed_subagents``：

* ``None`` = 全部允许
* ``[]`` = 硬拒绝（提示词里不出现委派段，``task`` 工具也不装进工具表）
* 列表 = 白名单

**策略在 run 开始时快照一次，``task`` 工具内部绝不重读可变的 agent 配置**：否则运行
中途有人改了 YAML，同一次 run 的前后两次委派就会用两套策略。

同一份快照要喂给三处，只过滤一边就是坏的：

1. 提示词里的**发现**（``{subagent_section}`` 列出的类型）
2. ``task`` 的**执行**（``list_subagent_names`` 与白名单判定）
3. ``SubagentLimitMiddleware`` 的 ``max_concurrent``（和提示词里的数字必须是同一个）

本模块刻意**很轻**（只 import 配置层与 ``registry``）：worker、prompt、assembly 都要用它，
而 ``subagents.runtime``` 那条链会绕回 ``agents.middlewares.assembly``（成环）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentkit.config import AppConfig
from agentkit.config.subagents_config import effective_subagent_concurrency

logger = logging.getLogger(__name__)

#: 快照在 ``runtime.context`` 里的两个键。``task`` 工具 §9.1 第 1 步读的就是它们。
ALLOWED_SUBAGENTS_CONTEXT_KEY = "allowed_subagents"
SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY = "subagent_max_concurrent"

#: 快照带进子代理 context 时也带上这两个键（``SubagentDispatch.allowed_subagents`` 用它）。
SNAPSHOT_CONTEXT_KEYS = (ALLOWED_SUBAGENTS_CONTEXT_KEY, SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY)


@dataclass(frozen=True, slots=True)
class SubagentPolicy:
    """不可变的委派策略快照：改了配置也影响不到已经发出去的那一份。"""

    allowed: list[str] | None = None
    max_concurrent: int = 1

    @property
    def enabled(self) -> bool:
        """``[]`` = 硬拒绝。这时提示词不出现委派段，``task`` 也不进工具表。"""
        return self.allowed != []

    @property
    def parallel(self) -> bool:
        """只有 > 1 时才允许在提示词里教模型并行（§16）。"""
        return self.max_concurrent > 1

    def to_context(self) -> dict[str, Any]:
        """写进 ``runtime.context`` 的那两个键。``allowed`` 复制一份：快照不许被改。"""
        return {
            ALLOWED_SUBAGENTS_CONTEXT_KEY: None if self.allowed is None else list(self.allowed),
            SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY: self.max_concurrent,
        }


def snapshot_subagent_policy(
    app_config: AppConfig,
    *,
    max_running: int | None = None,
    requested: int | None = None,
) -> SubagentPolicy:
    """把"这次 run 允许怎么委派"定死（§16）。

    ``max_running`` 应当来自**冻结**的控制器（``runtime.config.max_running``，§10.1）：
    热重载抬不高已经宣告出去的容量。没给就退回当前 AppConfig（直接调 factory / 单测），
    两者都会被 ``effective_subagent_concurrency`` 夹进 1–64。
    """
    declared = app_config.agents.subagent_max_concurrent
    frozen = max_running if max_running is not None else app_config.subagent_runtime.max_running
    return SubagentPolicy(
        allowed=_normalize_allowed(app_config.agents.allowed_subagents),
        max_concurrent=effective_subagent_concurrency(
            declared if requested is None else requested, max_running=frozen
        ),
    )


def allowed_subagents_from_context(context: object) -> list[str] | None:
    """执行侧读快照（§9.1 第 1 步）。

    缺失（没快照过）→ ``None`` = 全部允许；**畸形 → ``[]`` = 硬拒绝**：策略读不出来时
    宁可不让派，也不要按"全部允许"放行。
    """
    if not isinstance(context, Mapping) or ALLOWED_SUBAGENTS_CONTEXT_KEY not in context:
        return None
    return _normalize_allowed(context.get(ALLOWED_SUBAGENTS_CONTEXT_KEY))


def max_concurrent_from_context(context: object, fallback: int) -> int:
    """执行/组装侧读同一个数；缺失或畸形 → ``fallback``（已经 clamp 过的那份）。"""
    if isinstance(context, Mapping):
        value = context.get(SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
    return fallback


def _normalize_allowed(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        return list(raw)
    logger.warning("malformed allowed_subagents policy %r; refusing to delegate", raw)
    return []


__all__ = [
    "ALLOWED_SUBAGENTS_CONTEXT_KEY",
    "SNAPSHOT_CONTEXT_KEYS",
    "SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY",
    "SubagentPolicy",
    "allowed_subagents_from_context",
    "max_concurrent_from_context",
    "snapshot_subagent_policy",
]
