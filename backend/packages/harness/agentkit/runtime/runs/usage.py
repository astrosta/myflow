"""父 run 的 token 累加器（M3）+ M8 §15 的外部用量入账口。

lead 自己的调用在 ``on_llm_end`` 里就地累加；子代理跑在**隔离循环**上，它们的用量由
``task`` 工具在父循环里交回来（``record_external_llm_usage_records``，§7.5）。两条路都落到
同一份 :class:``RunTotals```，但**分开两栏**（``lead_agent_tokens`` / ``subagent_tokens``）——
"M8 的账算在这次 run 上，但不混进 lead 的消耗"。

累加器是**无锁**的读-改-写字段：只允许父循环写（§7.5）。
"""

import time
from typing import Any, Mapping
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from agentkit.runtime.runs.manager import RunManager
from agentkit.runtime.runs.totals import RunTotals
from agentkit.runtime.runs.usage_metrics import (
    MIDDLEWARE_CALLER_PREFIX,
    UNKNOWN_MODEL,
    LlmUsage,
    extract_llm_usage,
)

PROGRESS_REPORT_INTERVAL_SECONDS = 5.0

#: 外部记录按 §15 的 ``caller`` 分栏：M3 的三个维度是 lead / subagent / middleware。
SUBAGENT_COLUMN = "subagent"
MIDDLEWARE_COLUMN = "middleware"
EXTERNAL_COLUMNS = (SUBAGENT_COLUMN, MIDDLEWARE_COLUMN)


class UsageCollector(AsyncCallbackHandler):
    run_inline = True

    #: §7.2 / §15：这个 handler 绑定**父循环**（累加器无锁、且持有 RunManager）。不加这个标记，
    #: 子代理会把父 run 的收集器继承进隔离循环，于是同一个子代理调用**记两遍**——一遍作为 lead
    #: 消耗（继承的回调），一遍作为 subagent（§15 的显式上报）——而且隔离循环那一遍还会静默
    #: 丢更新。M8 的交付路径是显式的：holder → ``task`` 工具 → ``record_external_llm_usage_records``。
    myflow_loop_bound = True

    def __init__(self, run_manager: RunManager, run_id: str) -> None:
        self._run_manager = run_manager
        self._run_id = run_id
        self._by_model: dict[str, dict[str, int]] = {}
        self._calls = 0
        self._counted_llm_run_ids: set[str] = set()
        self._last_report = time.monotonic()
        # §15：外部用量**单独成栏**，所以单独一份桶。合进 ``_by_model`` 会让
        # ``lead_agent_tokens`` 把子代理的消耗也算进去（``RunTotals`` 的守恒断言会拦下来）。
        self._external_by_model: dict[str, dict[str, int]] = {}
        self._external_input: dict[str, int] = {column: 0 for column in EXTERNAL_COLUMNS}
        self._external_output: dict[str, int] = {column: 0 for column in EXTERNAL_COLUMNS}
        self._external_calls: dict[str, int] = {column: 0 for column in EXTERNAL_COLUMNS}
        #: 幂等：同一条记录（同一个 ``(execution_id, llm_run_id)``）只入账一次。
        self._external_seen: set[tuple[str, str]] = set()

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
            return

        rid = str(run_id) if run_id is not None else None
        if rid is not None and rid in self._counted_llm_run_ids:
            return
        if rid is not None:
            self._counted_llm_run_ids.add(rid)
        self._calls += 1
        model = usage.model or UNKNOWN_MODEL
        bucket = self._by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
        bucket["input"] += usage.input_tokens
        bucket["output"] += usage.output_tokens
        bucket["cache_read"] += usage.cache_read_tokens

    def record_external_llm_usage_records(self, records: Any) -> None:
        """§15 第 3 点：把**子代理**的用量汇进本次 run 的账。

        只有父循环能调（§7.5）：这里的字段是无锁的读-改-写，从隔离循环或工作线程写会**静默**
        丢更新。``task`` 工具按方法名找到本方法（``RunJournal`` 也实现同名转发，两条路都
        汇到这里）。交付方 ``SubagentResult.claim_usage_report`` 已经保证只报一次，这里的
        ``(execution_id, llm_run_id)`` 去重是第二道保险（§7.2 的"重复计账"）。
        """
        for record in records or ():
            if not isinstance(record, Mapping):
                continue
            usage = _usage_of_record(record)
            if usage.total_tokens <= 0:
                continue  # 缺省 ≠ 0（§15）
            key = _record_identity(record)
            if key is not None:
                if key in self._external_seen:
                    continue
                self._external_seen.add(key)
            column = _caller_column(record.get("caller"))
            self._external_calls[column] += 1
            self._external_input[column] += usage.input_tokens
            self._external_output[column] += usage.output_tokens
            model = usage.model or UNKNOWN_MODEL
            bucket = self._external_by_model.setdefault(
                model, {"input": 0, "output": 0, "cache_read": 0}
            )
            bucket["input"] += usage.input_tokens
            bucket["output"] += usage.output_tokens
            bucket["cache_read"] += usage.cache_read_tokens

    def totals(self) -> RunTotals:
        input_tokens = sum(bucket["input"] for bucket in self._by_model.values())
        output_tokens = sum(bucket["output"] for bucket in self._by_model.values())
        external_input = sum(self._external_input.values())
        external_output = sum(self._external_output.values())
        return RunTotals(
            total_input_tokens=input_tokens + external_input,
            total_output_tokens=output_tokens + external_output,
            llm_call_count=self._calls + sum(self._external_calls.values()),
            lead_agent_tokens=input_tokens+output_tokens,
            subagent_tokens=(
                self._external_input[SUBAGENT_COLUMN] + self._external_output[SUBAGENT_COLUMN]
            ),
            middleware_tokens=(
                self._external_input[MIDDLEWARE_COLUMN] + self._external_output[MIDDLEWARE_COLUMN]
            ),
            token_usage_by_model=_merge_by_model(self._by_model, self._external_by_model),
        )
    async def maybe_report(self) -> None:
        now = time.monotonic()
        if now - self._last_report >= PROGRESS_REPORT_INTERVAL_SECONDS:
            self._last_report = now
            await self._run_manager.update_run_progress(self._run_id, self.totals())

def _usage_of_record(record: Mapping[str, Any]) -> LlmUsage:
    """一条记录 → 用量。键名是 §15 定的；缺的当 0，**负数/布尔当没写**。"""
    return LlmUsage(
        input_tokens=_non_negative(record.get("input_tokens")),
        output_tokens=_non_negative(record.get("output_tokens")),
        cache_read_tokens=_non_negative(record.get("cache_read_tokens")),
        model=_record_model(record),
    )


def _record_identity(record: Mapping[str, Any]) -> tuple[str, str] | None:
    execution_id = record.get("execution_id")
    llm_run_id = record.get("llm_run_id")
    if isinstance(execution_id, str) and isinstance(llm_run_id, str):
        return (execution_id, llm_run_id)
    return None


def _caller_column(caller: object) -> str:
    """§15：``caller`` 决定这笔外部用量进哪一栏（M3 的 lead / subagent / middleware 三维度）。

    认不出来的一律算子代理：外部记录目前只有子代理执行器一个来源，宁可记在"委派"栏，
    也不要因为一个拼错的 caller 把用量丢掉（``RunTotals`` 的守恒断言会立刻发现总数对不上）。
    """
    if isinstance(caller, str) and caller.startswith(MIDDLEWARE_CALLER_PREFIX):
        return MIDDLEWARE_COLUMN
    return SUBAGENT_COLUMN


def _record_model(record: Mapping[str, Any]) -> str | None:
    model = record.get("model")
    return model if isinstance(model, str) and model else None


def _non_negative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _merge_by_model(
    lead: Mapping[str, Mapping[str, int]],
    subagent: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    """按模型合并两栏：``token_usage_by_model`` 是"这次 run 在每个模型上花了多少"。"""
    merged = {model: dict(bucket) for model, bucket in lead.items()}
    for model, bucket in subagent.items():
        target = merged.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
        for key, value in bucket.items():
            target[key] = target.get(key, 0) + value
    return merged