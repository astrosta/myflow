"""步骤捕获与事件映射（M8 §14，D8）。

**纯函数**：不启动图、不碰 store，所以单元测试不需要 Gateway。三个入口：

1. ``capture_new_step_messages`` —— 走**新增的尾部**，不是 ``messages[-1]``：一轮里模型调了
   3 个工具时，ToolNode 会在**一个超级步**里追加 3 条 ToolMessage，只看最后一条会丢两条。
2. ``build_subagent_step`` —— 一步的载荷，**有界**（§14.2）：一次 ``write_file`` 的全文
   不能变成无界的一行事件。
3. ``subagent_run_event`` —— custom 帧 → ``SubagentEventRow``（§14.3）；不合规的块返回 None。

只捕获 ``AIMessage`` / ``ToolMessage``；去重有 id 按 id、没 id 按整 dict。

**事件种类的词表不在这里**：它在 ``runtime/events/catalog.py`` 的 ``RunEventDefinition``` 里
（进程级常量、可当 key、被契约测试反射）。这里的 ``SubagentEventRow`` 是"一帧 → 一行"的
实例，每帧新建、用完交给 store。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, MutableSequence, MutableSet, Sequence
from dataclasses import dataclass
from typing import Any

from agentkit.runtime.events.catalog import (
    SUBAGENT_END_EVENT,
    SUBAGENT_START_EVENT,
    SUBAGENT_STEP_EVENT,
)
from agentkit.utils.messages import message_content_to_text

#: §14.2：一步的文本上限。工具参数 JSON 超长时改成截断预览并标 ``args_truncated``。
SUBAGENT_STEP_MAX_CHARS = 8192

#: 只有这两种消息进事件流：用户的、和工具结果的，都不是"子代理的步骤"。
CAPTURABLE_MESSAGE_TYPES = frozenset({"ai", "tool"})

#: §3 的 custom 事件名 → 持久化事件。``task_running`` 每条新步骤一行。
_DEFINITION_BY_CUSTOM_TYPE = {
    "task_started": SUBAGENT_START_EVENT,
    "task_running": SUBAGENT_STEP_EVENT,
    "task_completed": SUBAGENT_END_EVENT,
    "task_failed": SUBAGENT_END_EVENT,
    "task_cancelled": SUBAGENT_END_EVENT,
    "task_timed_out": SUBAGENT_END_EVENT,
}


@dataclass(frozen=True, slots=True)
class SubagentEventRow:
    """**一条待落库的事件**（实例），不是"存在哪些事件"（词表条目）。

    词表条目是 ``runtime/events/catalog.py`` 里的 ``RunEventDefinition``：进程级常量、只有
    ``(event_type, category)``、可当 dict key、被契约测试反射。这里相反——每一帧 custom 都
    新建一个，装的是那一帧经校验和有界化之后的 ``content`` / ``metadata``，用完就交给 store。

    还缺 ``thread_id`` / ``run_id`` / ``user_id``：它们是 run 级身份，由 worker 侧的
    ``_SubagentEventBuffer._envelope`` 补上（§14.3）——所以这一层是纯函数，不需要 store
    和 Gateway 就能单测。
    """

    event_type: str
    category: str
    content: dict[str, Any]
    metadata: dict[str, Any]


# ---------------------------------------------------------------- §14.1 步骤捕获


def capture_new_step_messages(
    messages: Sequence[Any],
    captured: MutableSequence[dict[str, Any]],
    seen_ids: MutableSet[str],
    processed_count: int,
) -> int:
    """把新出现的可捕获消息追加到 ``captured``，返回新的游标（§14.1）。

    三个分支：

    * ``total > processed_count``：正常推进，逐条看新增的尾部
    * ``total < processed_count``：**压缩收缩**（Summarization 用
      ``RemoveMessage(REMOVE_ALL_MESSAGES)`` 重写整个消息通道）→ 游标重置到新的尾部，
      靠 id/内容去重防止把压缩前的步骤重发一遍
    * 等长：原地替换（同一条消息被改写）只看 ``messages[-1]``

    **不变量**：重置之后"等长"分支只重看最后一条，所以压缩**不得**在游标以下插入一条新的
    可捕获消息。今天 Summarization 把摘要放进 ``summary_text``，messages 通道只留已经见过的
    尾巴。将来若有中间件违反这条，重置分支要改成全量重扫。
    """
    total = len(messages)
    if total < processed_count:
        processed_count = total
    if total > processed_count:
        for index in range(processed_count, total):
            capture_step_message(messages[index], captured, seen_ids, index=index)
        return total
    if messages:
        capture_step_message(messages[-1], captured, seen_ids, index=total - 1)
    return max(processed_count, total)


def capture_step_message(
    message: Any,
    captured: MutableSequence[dict[str, Any]],
    seen_ids: MutableSet[str],
    *,
    index: int,
) -> bool:
    """捕获一条消息（``AIMessage`` / ``ToolMessage``）。返回是否**新**捕获。

    ``index`` 是它在 messages 通道里的位置：``task_running`` 的 ``message_index`` 靠它，
    前端折叠卡片也按它排序。
    """
    if getattr(message, "type", None) not in CAPTURABLE_MESSAGE_TYPES:
        return False
    payload = build_subagent_step(message)
    identity = step_identity(message, payload)
    if identity in seen_ids:
        return False
    seen_ids.add(identity)
    captured.append({"message_index": index, "message": payload})
    return True


def build_subagent_step(message: Any) -> dict[str, Any]:
    """一步的 JSON-safe、有界载荷（§14.2）。事件要过 SSE 序列化，所以只放基本类型。"""
    payload: dict[str, Any] = {"type": str(getattr(message, "type", "") or "")}
    for key in ("id", "name", "tool_call_id"):
        value = getattr(message, key, None)
        if isinstance(value, str) and value:
            payload[key] = value
    text = message_content_to_text(getattr(message, "content", ""))
    if len(text) > SUBAGENT_STEP_MAX_CHARS:
        payload["content"] = text[:SUBAGENT_STEP_MAX_CHARS]
        payload["content_truncated"] = True
    else:
        payload["content"] = text
    tool_calls = getattr(message, "tool_calls", None)
    if isinstance(tool_calls, list) and tool_calls:
        payload["tool_calls"] = [
            build_subagent_tool_call(call) for call in tool_calls if isinstance(call, Mapping)
        ]
    return payload


def build_subagent_tool_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """工具调用的摘要。参数超长 → 截断预览 + ``args_truncated``（§14.2）。"""
    args = call.get("args")
    text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
    payload: dict[str, Any] = {
        "name": str(call.get("name") or ""),
        "id": str(call.get("id") or ""),
    }
    if len(text) > SUBAGENT_STEP_MAX_CHARS:
        payload["args_preview"] = text[:SUBAGENT_STEP_MAX_CHARS]
        payload["args_truncated"] = True
    else:
        payload["args"] = text
    return payload


def step_identity(message: Any, payload: Mapping[str, Any]) -> str:
    """去重键：有 id 按 id，没 id 按整 dict（§14.1）。"""
    message_id = getattr(message, "id", None)
    if isinstance(message_id, str) and message_id:
        return f"id:{message_id}"
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "sha1:" + hashlib.sha1(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- §14.3 事件映射


def subagent_run_event(chunk: object) -> SubagentEventRow | None:
    """把一条 ``task_*`` custom 帧映射成持久化事件；不合规的块返回 ``None``（丢弃，不抛）。

    拒掉的两种（§14.3）：没有非空 ``task_id`` 的块；``task_running`` 缺少非负
    ``message_index`` 或 message 对象。``task_id`` 用的是 **provider 的 tool_call_id**
    （对外相关键），不是 ``execution_id``。
    """
    if not isinstance(chunk, Mapping):
        return None
    definition = _DEFINITION_BY_CUSTOM_TYPE.get(chunk.get("type"))
    if definition is None:
        return None
    task_id = chunk.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return None

    if definition is SUBAGENT_STEP_EVENT:
        step = _step_content(chunk)
        if step is None:
            return None
        content: dict[str, Any] = step
    elif definition is SUBAGENT_START_EVENT:
        content = _present(chunk, ("description", "subagent_type", "model_name"))
    else:
        content = _end_content(chunk)

    metadata = {"task_id": task_id}
    metadata.update(_present(chunk, ("description", "subagent_type", "model_name")))
    if definition is SUBAGENT_STEP_EVENT:
        metadata["message_index"] = content["message_index"]
    return SubagentEventRow(
        event_type=definition.event_type,
        category=definition.category,
        content=content,
        metadata=metadata,
    )


def _step_content(chunk: Mapping[str, Any]) -> dict[str, Any] | None:
    index = chunk.get("message_index")
    message = chunk.get("message")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    if not isinstance(message, Mapping):
        return None
    return {"message_index": index, "message": dict(message)}


def _end_content(chunk: Mapping[str, Any]) -> dict[str, Any]:
    """终态：content 带 status / model_name / usage / result（有则带，无则缺）。"""
    content = _present(chunk, ("status", "stop_reason", "model_name", "usage"))
    result = chunk.get("result")
    if isinstance(result, str) and result:
        # §14.3：结果正文截断到与 step 相同的上限；**完整结果以 ToolMessage 为准**。
        content["result"] = result[:SUBAGENT_STEP_MAX_CHARS]
        if len(result) > SUBAGENT_STEP_MAX_CHARS:
            content["result_truncated"] = True
    return content


def _present(chunk: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """只抄有值的键：缺省和 0 / 空串不是一回事（§15）。"""
    return {key: chunk[key] for key in keys if chunk.get(key) is not None}


__all__ = [
    "CAPTURABLE_MESSAGE_TYPES",
    "SUBAGENT_STEP_MAX_CHARS",
    "SubagentEventRow",
    "build_subagent_step",
    "build_subagent_tool_call",
    "capture_new_step_messages",
    "capture_step_message",
    "step_identity",
    "subagent_run_event",
]
