"""M8 §15：子代理的 token 归到**父 run** 的账上（D10）。

两条通道，**不要用进程级全局缓存**：

1. 运行中：本收集器每完成一次 LLM 响应，把**累计快照**写进
   ``SubagentResult.token_usage_records``（持 holder 锁）。下一条 ``task_running`` 带上这份
   snapshot，前端折叠卡片能实时更新，而不必重算父 run 总计。
2. 终态：``task`` 工具把它汇总成 ``ToolMessage.additional_kwargs["subagent_token_usage"]``
   （§15 第 2 点）。刷新页面后卡片仍能显示。
3. 父账本：``task`` 工具在**父循环**里交付（§7.5）——本模块只负责**采集**。

这个 handler **不打** ``myflow_loop_bound`` 标记：它就该在隔离循环上跑（§7.2）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackManager
from langchain_core.outputs import LLMResult

from agentkit.runtime.runs.usage_metrics import (
    SUBAGENT_CALLER_PREFIX,
    UNKNOWN_MODEL,
    LlmUsage,
    extract_llm_usage,
)
from agentkit.subagents.results import SubagentResult

logger = logging.getLogger(__name__)

#: §15：``caller`` 用 ``subagent:{name}``，父账本按这个分栏（M3 已定 lead / subagent /
#: middleware 三个维度）。前缀常量与父账本共用，免得两处各写一份字符串。
CALLER_PREFIX = SUBAGENT_CALLER_PREFIX


class SubagentTokenCollector(AsyncCallbackHandler):
    """隔离循环上的用量采集器：一次委派一个实例（§12.5：并行子代理不许串号）。"""

    run_inline = True

    def __init__(
        self,
        holder: SubagentResult,
        *,
        subagent_name: str,
        model_name: str | None = None,
    ) -> None:
        super().__init__()
        self._holder = holder
        self._subagent_name = subagent_name
        #: provider 不报模型名时的兜底：dispatch 那一刻解析好的子代理模型（§4.3）。
        self._model_name = model_name
        self._records: list[dict[str, Any]] = []
        #: §15：按 LangChain ``run_id`` 去重——一次 LLM 回调可能打多次 ``on_llm_end``
        #: （流式提供商先回一次空 usage，再回放一次带 usage 的）。没给 run_id 时无法去重。
        self._counted: set[str] = set()
        self._anonymous = 0

    @property
    def caller(self) -> str:
        return f"{CALLER_PREFIX}{self._subagent_name}"

    def records(self) -> list[dict[str, Any]]:
        """到目前为止的**累计**记录（和 holder 里那份快照同一个内容）。"""
        return [dict(record) for record in self._records]

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        usage = extract_llm_usage(response)
        if not usage.known:
            # 缺省 ≠ 0：provider 没给就别估算，否则"未知"会被写成"没有消耗"（§15）。
            return
        if run_id is not None:
            key = str(run_id)
            if key in self._counted:
                return
            self._counted.add(key)
        else:
            key = f"anonymous:{self._anonymous}"
            self._anonymous += 1

        self._records.append(self._record(usage, key))
        # 累计快照是**替换**而不是追加（holder 的语义），所以这里给的是全量。
        self._holder.update_token_usage_records(self._records)

    def _record(self, usage: LlmUsage, key: str) -> dict[str, Any]:
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "model": usage.model or self._model_name or UNKNOWN_MODEL,
            "caller": self.caller,
            # ``execution_id`` 就是 ``holder.task_id``（§8）。它**只**给父账本做幂等去重用，
            # 不进任何对外载荷：SSE / ToolMessage 只暴露 ``tool_call_id``（§2 的编号纪律）。
            "execution_id": self._holder.task_id,
            "llm_run_id": key,
        }


def attach_token_collector(
    config: Mapping[str, Any],
    collector: SubagentTokenCollector,
    *,
    inherited: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把收集器挂到**子 run 的** ``callbacks`` 上，继承来的处理器一个都不丢（§7.2 / §15）。

    ``inherited`` 是父 run 的 ``RunnableConfig``（调用方已经读过 ``var_child_runnable_config``
    的那一份；隔离循环里拿到的是**拷贝并摘掉绑定父循环处理器**之后的结果）。不传就现读。

    为什么要显式带上继承的处理器：``ensure_config`` 是 ``update`` 语义——config 里出现
    ``callbacks`` 就会**替换**掉 ContextVar 那一份，而 §3.1 的帧发布依赖 LangGraph 自己的
    流处理器活在这条链上。所以这里做的是"追加"，不是"覆盖"。
    """
    parent = inherited if inherited is not None else _inherited_config()
    existing = parent.get("callbacks") if isinstance(parent, Mapping) else None
    merged = _append_handler(existing, collector)
    return {**dict(config), "callbacks": merged}


def _inherited_config() -> Mapping[str, Any]:
    from langchain_core.runnables.config import var_child_runnable_config

    current = var_child_runnable_config.get()
    return current if isinstance(current, Mapping) else {}


def _append_handler(existing: Any, collector: SubagentTokenCollector) -> Any:
    """``callbacks`` 有三种形状（manager / list / 单个 handler），三种都要能追加。"""
    if isinstance(existing, BaseCallbackManager):
        # ``copy()`` 是浅拷贝 + 新的 handler 列表：追加不会改到父 run 的那一份。
        merged = existing.copy()
        merged.handlers = [*merged.handlers, collector]
        merged.inheritable_handlers = [*merged.inheritable_handlers, collector]
        return merged
    if isinstance(existing, (list, tuple)):
        return [*existing, collector]
    if existing is None:
        return [collector]
    return [existing, collector]


__all__ = [
    "CALLER_PREFIX",
    "SubagentTokenCollector",
    "attach_token_collector",
]
