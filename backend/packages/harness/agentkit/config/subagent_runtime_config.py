"""``subagent_runtime:`` 段落（M8 §4.2）——**进程级容量，四个字段全部 startup-only**。

准入控制器在 Gateway 启动时构造（§10.1 / §10.2），热重载改 YAML **不会**重建它。
所以四条都进 M0 的 ``STARTUP_ONLY_FIELDS``：不登记的话，运维改了 ``max_running``
发现没生效，却没有任何提示。

**热重载不能让任何一层宣告比已创建的控制器更多的容量**——提示词说"你可以并发 8 个"
而控制器只有 3 个槽，模型会一直撞墙。控制器拿 ``freeze()`` 的深拷贝，之后
AppConfig 怎么改都不影响它。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agentkit.config.reload_boundary import format_field_description
from agentkit.config.subagents_config import (
    SUBAGENT_CONCURRENCY_CEILING,
    SubagentConfigModel,
    effective_subagent_concurrency,
)

DEFAULT_MAX_RUNNING = 3
DEFAULT_MAX_QUEUED = 64
DEFAULT_QUEUE_TIMEOUT_SECONDS = 300

AdmissionPolicy = Literal["queue", "reject"]


class SubagentRuntimeConfig(SubagentConfigModel):
    """进程级容量与准入策略。构造参数，不是每 run 读的配置。"""

    max_running: int = Field(
        default=DEFAULT_MAX_RUNNING,
        ge=1,
        le=SUBAGENT_CONCURRENCY_CEILING,
        description=format_field_description(
            "subagent_runtime.max_running",
            field_doc="同时在跑的子代理数。提示词与 SubagentLimitMiddleware 的并发上限也来自这里。",
        ),
    )
    max_queued: int = Field(
        default=DEFAULT_MAX_QUEUED,
        ge=0,
        description=format_field_description(
            "subagent_runtime.max_queued",
            field_doc="等待队列容量。0 = 槽位耗尽直接拒绝，等价 admission_policy=reject。",
        ),
    )
    admission_policy: AdmissionPolicy = Field(
        default="queue",
        description=format_field_description(
            "subagent_runtime.admission_policy",
            field_doc="槽位耗尽时排队（queue）还是立刻失败（reject）。两者都不报工具错误，只是失败得更早。",
        ),
    )
    queue_timeout_seconds: int = Field(
        default=DEFAULT_QUEUE_TIMEOUT_SECONDS,
        ge=0,
        description=format_field_description(
            "subagent_runtime.queue_timeout_seconds",
            field_doc="排队等待上限（秒）。超时按准入失败处理（admission_failure=True），不消耗成功委派语义。",
        ),
    )

    def freeze(self) -> SubagentRuntimeConfig:
        """控制器持有的那份深拷贝（§10.1）。

        ``SubagentRuntime.from_app_config`` 建控制器时必须调它：之后 YAML 怎么热重载，
        已经宣告出去的容量都不变。
        """
        return self.model_copy(deep=True)

    def effective_max_concurrent(self, requested: int | None = None) -> int:
        """单轮并行数 = min(请求值, 冻结的 max_running, 64)（§4.2）。"""
        return effective_subagent_concurrency(requested, max_running=self.max_running)
