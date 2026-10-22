"""``task`` 工具测试用的执行侧替身（M8 §9）。

真正的执行器是 §7 的隔离循环（D3–5）。这里只实现 ``task`` 工具要求的**那一件事**：
**同步提交**，然后把 holder 推向终态——工具自己轮询，所以替身不需要事件循环。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agentkit.config import AppConfig
from agentkit.subagents.results import SubagentStatus


DEFAULT_MODELS = [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]


def make_app_config(*, allow_host_bash: bool = False, models: list[dict] | None = None, **subagents: Any) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": DEFAULT_MODELS if models is None else models,
            "sandbox": {
                "use": "agentkit.sandbox.local:LocalSandboxProvider",
                "allow_host_bash": allow_host_bash,
            },
            "subagents": subagents,
        }
    )


@dataclass
class FakeRuntime:
    """``ToolRuntime`` 的最小替身：工具只读 context / state / config / tools / stream_writer。"""

    context: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    events: list = field(default_factory=list)
    stream_error: Exception | None = None
    #: 前 N 个事件照发，第 N+1 个开始抛：用来区分"task_started 发失败"与"后续事件发失败"。
    fail_events_after: int | None = None

    def stream_writer(self, payload: Any) -> None:
        if self.stream_error is not None:
            raise self.stream_error
        if self.fail_events_after is not None and len(self.events) >= self.fail_events_after:
            raise RuntimeError("stream writer is gone")
        self.events.append(payload)

    def event_types(self) -> list[str]:
        return [str(event.get("type")) for event in self.events]


class FakeDispatcher:
    """记录提交、按脚本推进 holder。``delay=0`` 时同步推到终态（确定性测试用）。"""

    def __init__(
        self,
        *,
        terminal: SubagentStatus | None = SubagentStatus.COMPLETED,
        result: str | None = "the answer",
        error: str | None = None,
        stop_reason: str | None = None,
        steps: tuple[dict, ...] = (),
        late_steps: tuple[dict, ...] = (),
        usage: tuple[dict, ...] = (),
        delay: float = 0.0,
        fail_start: Exception | None = None,
    ) -> None:
        self.dispatches: list[Any] = []
        self._terminal = terminal
        self._result = result
        self._error = error
        self._stop_reason = stop_reason
        self._steps = steps
        self._late_steps = late_steps
        self._usage = usage
        self._delay = delay
        self._fail_start = fail_start

    @property
    def dispatch(self):
        return self.dispatches[0]

    def start(self, dispatch: Any) -> None:
        self.dispatches.append(dispatch)
        if self._fail_start is not None:
            raise self._fail_start
        holder = dispatch.holder
        holder.mark_running()
        if self._usage:
            holder.update_token_usage_records(self._usage)
        if self._steps:
            holder.append_steps(self._steps)
        if self._terminal is None:
            return  # 永远不终态：给"父侧轮询预算耗尽"用
        if self._delay <= 0:
            self._finish(dispatch)
            return
        threading.Thread(target=self._finish_later, args=(dispatch,), daemon=True).start()

    def _finish_later(self, dispatch: Any) -> None:
        import time

        time.sleep(self._delay)
        if self._late_steps:
            dispatch.holder.append_steps(self._late_steps)
        self._finish(dispatch)

    def _finish(self, dispatch: Any) -> None:
        assert self._terminal is not None
        dispatch.holder.try_set_terminal(
            self._terminal, result=self._result, error=self._error, stop_reason=self._stop_reason
        )


class FakeUsageRecorder:
    """``RunJournal`` 的最小替身（§15）：按方法名被认出来，不 import 持久化。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def record_external_llm_usage_records(self, records: Any) -> None:
        self.calls.append(list(records))


def make_tool(name: str) -> BaseTool:
    """真 ``BaseTool``：工具的 ``runtime.tools`` 只收 BaseTool 实例（§5.3）。"""

    def run() -> str:
        return name

    return StructuredTool.from_function(run, name=name, description=name)


__all__ = [
    "DEFAULT_MODELS",
    "FakeDispatcher",
    "FakeRuntime",
    "FakeUsageRecorder",
    "make_app_config",
    "make_tool",
]
