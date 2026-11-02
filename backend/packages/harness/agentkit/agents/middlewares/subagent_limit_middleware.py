"""槽位耗尽时**剥调用**，不报错（M8 §10.3）。

这是 **lead 图**上的职责，不是执行器的：执行器已经在跑了，它只能失败；而"这一轮少派两个"
只有在模型刚发出 tool_calls、ToolNode 还没跑之前才有意义。

为什么剥掉比报错好：报错的话模型多半换个说法再派一次，用户会看到一串"已达上限"的失败卡片，
最后还是没拿到结果；删掉调用之后，这一轮**一定有产出**——要么是已有结果综合出来的答案，
要么是一句明确的"剩下的没做"。

顺序（``after_model`` 是**反序**执行的，所以这一条排在 DurableContext 之前跑）：

1. 数本轮 ``tool_calls`` 里 ``name=="task"`` 的下标
2. ``prior = count(delegations where run_id == current_run_id)``（缺 run_id → 整个 thread，偏严）
3. ``allowed = min(max_concurrent, max(0, max_total - prior))``
4. 超出的 task 调用丢掉；同一 ``id`` 换掉最后一条 AIMessage（``add_messages`` 按 id 替换）
5. 同步 provider 的原始 tool_call 元数据，否则留下悬空调用（M6 §5.1）
6. ``allowed == 0`` 时强制 ``finish_reason="stop"``，并追加一句可见说明
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import suppress

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware

from agentkit.agents.middlewares.message_utils import clone_ai_message_with_tool_calls
from agentkit.subagents.config import TASK_TOOL_NAME

logger = logging.getLogger(__name__)

#: 总量用尽时的可见说明。写进**模型和用户都看得到**的那条 AI 消息里。
LIMIT_NOTICE = (
    "[已达子代理上限] 本次运行的子代理委派额度已用尽。"
    "请基于已有结果完成剩余工作，或向用户说明哪些没有做。"
)


def format_limit_notice(
    *,
    max_concurrent: int,
    remaining: int,
    dropped_labels: list[str],
) -> str:
    """区分单轮并行超限和本 run 总量用尽：只有后者才说"额度已用尽"。"""
    labels = "；".join(dropped_labels) if dropped_labels else "未命名任务"
    if remaining <= 0:
        return (
            f"[已达子代理上限] 本次运行的子代理委派额度已用尽。"
            f"以下任务未执行：{labels}。"
            "请基于已有结果完成剩余工作，或向用户说明哪些没有做。"
        )
    return (
        f"[已达子代理上限] 本轮最多并行 {max_concurrent} 个子代理，"
        f"已执行前 {max_concurrent} 个；以下任务未执行：{labels}。"
        f"拿到结果后如仍需要，可以再委派（本次运行还剩 {remaining} 次），或自己完成。"
    )


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """构造函数只吃**已经 clamp 好**的数字——中间件不许自己再读 YAML（§10.1）。

    ``max_concurrent`` 由装配层用 ``SubagentRuntime.effective_max_concurrent()`` 解析
    （提示词、这里、控制器必须读同一个数），``max_total_per_run`` 则每 run 现读当前
    ``AppConfig``（这一条可热重载）。
    """

    def __init__(self, *, max_concurrent: int, max_total_per_run: int) -> None:
        super().__init__()
        self._max_concurrent = max(1, int(max_concurrent))
        self._max_total_per_run = max(0, int(max_total_per_run))

    @property
    def max_concurrent(self) -> int:
        """单轮并行上限（装配时 clamp 到进程槽位；提示词读的是同一个数）。"""
        return self._max_concurrent

    @property
    def max_total_per_run(self) -> int:
        """一次 run 的累计上限。"""
        return self._max_total_per_run

    def after_model(self, state, runtime) -> dict | None:
        return self._strip(state, runtime)

    async def aafter_model(self, state, runtime) -> dict | None:
        return self._strip(state, runtime)

    def _strip(self, state, runtime) -> dict | None:
        message = _last_ai_message(state)
        if message is None:
            return None
        calls = [
            call for call in (getattr(message, "tool_calls", None) or []) if isinstance(call, Mapping)
        ]
        task_positions = [
            index for index, call in enumerate(calls) if call.get("name") == TASK_TOOL_NAME
        ]
        if not task_positions:
            return None

        prior = _prior_delegations(state, runtime)
        allowed = min(
            self._max_concurrent,
            max(0, self._max_total_per_run - prior),
        )
        if len(task_positions) <= allowed:
            return None

        dropped = set(task_positions[allowed:])
        kept = [call for index, call in enumerate(calls) if index not in dropped]
        dropped_calls = [calls[index] for index in task_positions[allowed:]]
        remaining = max(0, self._max_total_per_run - prior - allowed)
        notice = format_limit_notice(
            max_concurrent=self._max_concurrent,
            remaining=remaining,
            dropped_labels=[_call_label(call) for call in dropped_calls],
        )
        logger.info(
            "SubagentLimitMiddleware: dropped %d of %d task call(s) (allowed=%d, max_concurrent=%d)",
            len(dropped),
            len(task_positions),
            allowed,
            self._max_concurrent,
        )
        stripped = clone_ai_message_with_tool_calls(message, kept)
        stripped = _with_notice(stripped, notice, force_stop=allowed == 0)
        return {"messages": [stripped]}


def _last_ai_message(state) -> object | None:
    for message in reversed(list(state.get("messages") or [])):
        if getattr(message, "type", None) == "ai":
            return message
    return None


def _prior_delegations(state, runtime) -> int:
    """本 run 已经派出去几条（含进行中的）。**缺 run_id 时整个 thread 都算**（§10.2：偏严）。"""
    delegations = state.get("delegations") or []
    run_id = _run_id(runtime)
    counted = 0
    for entry in delegations:
        if not isinstance(entry, Mapping):
            continue
        if run_id is None or entry.get("run_id") == run_id:
            counted += 1
    return counted


def _run_id(runtime) -> str | None:
    context = getattr(runtime, "context", None)
    if isinstance(context, Mapping):
        value = context.get("run_id")
        if isinstance(value, str) and value:
            return value
    return None


def _call_label(call: Mapping) -> str:
    args = call.get("args") or {}
    if isinstance(args, str):
        with suppress(json.JSONDecodeError, TypeError, ValueError):
            args = json.loads(args)
    if not isinstance(args, Mapping):
        args = {}
    text = str(args.get("description") or args.get("prompt") or "").strip()
    if not text:
        return "未命名任务"
    return " ".join(text.split())[:40]


def _with_notice(message, notice: str, *, force_stop: bool) -> object:
    """把可见说明写进 AI 消息；``allowed == 0`` 时顺带把 ``finish_reason`` 拽成 stop。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        new_content: object = f"{content}\n\n{notice}" if content.strip() else notice
    elif isinstance(content, list):
        new_content = [*content, {"type": "text", "text": notice}]
    else:
        new_content = notice
    update: dict = {"content": new_content}
    if force_stop:
        update["response_metadata"] = {
            **(getattr(message, "response_metadata", None) or {}),
            "finish_reason": "stop",
        }
    return message.model_copy(update=update)


__all__ = ["LIMIT_NOTICE", "SubagentLimitMiddleware", "format_limit_notice"]
