"""共享容量控制器（M8 §10.1）。

Gateway 启动时建**一个**进程级 ``SubagentRuntime``，然后必须把**同一个实例**喂给
``task`` 闭包、``SubagentLimitMiddleware`` 和 ``SubagentExecutor``：

> 直接调 factory 的测试如果每次 new 一个 Runtime，就会有 N 套各 3 槽的控制器，上限形同虚设。

进程级单例走 ``get_subagent_runtime()``，和 M2 的 sandbox provider 同一个套路（懒建 + 锁），
所以 ``assemble_agent`` 不需要在签名上多一个参数也能拿到同一份容量。

容量本身的冻结规则见 §4.2：``subagent_runtime.*`` 是 startup-only（构造时 ``freeze()`` 深拷贝），
而 ``subagents.max_total_per_run`` **可以热重载**——所以每 run 的实时值由装配层从当前
``AppConfig`` 现读，不从这个快照里拿。
"""

from __future__ import annotations

import logging
import threading

from agentkit.config import AppConfig, SubagentRuntimeConfig, get_app_config
from agentkit.subagents.capacity import CapacitySnapshot, SubagentExecutionCapacity
from agentkit.subagents.executor import SubagentExecutor

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOTAL_PER_RUN = 6


class SubagentRuntime:
    """进程级共享的容量控制器 + 执行器。"""

    def __init__(
        self,
        config: SubagentRuntimeConfig,
        *,
        max_total_per_run: int = DEFAULT_MAX_TOTAL_PER_RUN,
        app_config: AppConfig | None = None,
        lazy_init: bool = True,
    ) -> None:
        # 深拷贝：热重载改 YAML 不许抬高已经建好的控制器容量（§4.2）
        self.config = config.model_copy(deep=True)
        #: 构建时的快照。每 run 的实时值由装配层从当前 AppConfig 现读（这一条可热重载）。
        self.max_total_per_run = max_total_per_run
        self.app_config = app_config
        self.execution_capacity = SubagentExecutionCapacity(self.config)
        self.executor = SubagentExecutor(
            app_config=app_config,
            lazy_init=lazy_init,
            capacity=self.execution_capacity,
        )

    @classmethod
    def from_app_config(cls, app_config: AppConfig, *, lazy_init: bool = True) -> "SubagentRuntime":
        return cls(
            app_config.subagent_runtime.freeze(),
            max_total_per_run=app_config.subagents.max_total_per_run,
            app_config=app_config,
            lazy_init=lazy_init,
        )

    # ---- §9 的接缝（SubagentDispatcher）----------------------------------

    def start(self, dispatch) -> None:
        """把执行器提交到隔离循环；准入在隔离循环上做（§10.2）。"""
        self.executor.start(dispatch)

    def effective_max_concurrent(self, requested: int | None = None) -> int:
        """单轮并行数 = min(请求值, 冻结的 max_running, 64)（§4.2）。"""
        return self.config.effective_max_concurrent(requested)

    def snapshot(self) -> CapacitySnapshot:
        return self.execution_capacity.snapshot()


_default_runtime: SubagentRuntime | None = None
_runtime_lock = threading.Lock()


def get_subagent_runtime(app_config: AppConfig | None = None) -> SubagentRuntime:
    """进程级单例：第一个启用子代理的 run 顺手建它，之后所有人共用。"""
    global _default_runtime
    with _runtime_lock:
        if _default_runtime is not None:
            return _default_runtime
        runtime = SubagentRuntime.from_app_config(app_config or get_app_config())
        _default_runtime = runtime
        return runtime


def reset_subagent_runtime() -> None:
    """测试与进程关停用：丢掉单例（容量计数一起丢）。"""
    global _default_runtime
    with _runtime_lock:
        _default_runtime = None


__all__ = [
    "DEFAULT_MAX_TOTAL_PER_RUN",
    "SubagentRuntime",
    "get_subagent_runtime",
    "reset_subagent_runtime",
]
