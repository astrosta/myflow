"""台账：从 messages 抽委派条目 + 渲染给模型看（M8 §6.3 / §6.4）。

两条纪律都属于"看起来能省、省了就错"的那一类：

* **按 ``tool_call_id`` 配对**，不要靠"最后一条 ToolMessage"：一轮里可以有 3 个并行
  ``task`` 调用，尾部就近匹配会把三个结果串到同一个 id 上。
* **进行中的调用也要记一条 ``in_progress``**：压缩会把 messages 重写成一段摘要，
  这条是"已经派出去过"的唯一记忆；丢了它 lead 会再派一次同一个活。

渲染进**隐藏 HumanMessage 数据块**（M6 §4.5），**不进 SystemMessage**：委派结果是模型
自己写的，属于不可信通道——字段一律 html.escape，模型看到的是数据不是指令。

同一条委派会被两条路径写入（DurableContext 捕获、``task`` 工具返回 Command），
两边都走 ``make_delegation_entry``：brief 截断与 sha256 只有一套算法，
否则同一条委派在两次捕获里会得到两份指纹。
"""

from __future__ import annotations

import hashlib
import html
from collections.abc import Collection, Mapping, Sequence

from agentkit.agents.thread_state import (
    DELEGATION_STATUS_IN_PROGRESS,
    DELEGATION_STATUSES,
    STOP_REASONS,
    DelegationEntry,
)
from agentkit.persistence.base import now_iso
from agentkit.utils.messages import message_content_to_text

__all__ = [
    "DELEGATION_BRIEF_MAX_CHARS",
    "DELEGATION_DESCRIPTION_MAX_CHARS",
    "DELEGATION_LEDGER_RENDER_CHAR_BUDGET",
    "GUARDRAIL_GUIDANCE",
    "IN_PROGRESS_GUIDANCE",
    "LEDGER_HEADER",
    "RUN_START_MESSAGE_IDS_KEY",
    "TASK_TOOL_NAME",
    "extract_delegation_entries",
    "make_delegation_entry",
    "render_delegation_ledger",
    "result_sha256",
    "truncate_brief",
    "truncate_description",
]

#: 委派工具名。M8 只有这一个（``batch_task`` 是刻意不做的，§20）。
TASK_TOOL_NAME = "task"

#: §6.1 的两个截断上限。
DELEGATION_DESCRIPTION_MAX_CHARS = 200
DELEGATION_BRIEF_MAX_CHARS = 2000

#: §6.4 的渲染预算。条目本身有界（50 × 2KB），渲染必须再收一次。
DELEGATION_LEDGER_RENDER_CHAR_BUDGET = 6000

LEDGER_HEADER = "## 目前已有的委派"

#: §6.4：命中护栏时必须出现的指引，否则 lead 会把部分结果当成干净完成。
GUARDRAIL_GUIDANCE = (
    "触发了护栏上限，但已有部分结果；复用它、缩小范围重新委派，或基于部分结果继续"
)

#: §6.4：进行中的条目必须阻止 lead 再派一次同一个活。``task`` 是同步的，模型没法"等"。
IN_PROGRESS_GUIDANCE = "已经委派过；不要再委派同一个任务；基于已有结果继续"

#: 确定性头尾截断标记：同一份结果永远给同一段 brief（**不是** LLM 摘要）。
_BRIEF_MARKER = "\n...\n"

#: §10.2 的 worker 会把"本 run 开始前就在 checkpoint 里的消息 id"放进 runtime.context。
#: 命中的 ``task`` 调用属于历史 run，不打当前 run_id——否则总量上限会被历史委派吃掉。
RUN_START_MESSAGE_IDS_KEY = "run_start_message_ids"

def truncate_description(text: object, *, limit: int = DELEGATION_DESCRIPTION_MAX_CHARS) -> str:
    """§6.1：截断到 200 字。换行折成空格，台账每行保持一行。"""
    collapsed = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    return collapsed[:limit]


def truncate_brief(text: str, *, limit: int = DELEGATION_BRIEF_MAX_CHARS) -> str:
    """确定性**头尾**截断（§6.1）：结论和收尾的命令行都在，中间省略。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    keep = limit - len(_BRIEF_MARKER)
    if keep < 2:
        return text[:limit]
    head = keep // 2
    tail = keep - head
    return text[:head] + _BRIEF_MARKER + text[-tail:]


def result_sha256(text: str) -> str:
    """全文指纹（64 位小写 hex）。读侧校验形状，不重算（全文不在 state 里）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_delegation_entry(
    *,
    id: str,
    description: object = "",
    subagent_type: object = "",
    status: str = DELEGATION_STATUS_IN_PROGRESS,
    run_id: str | None = None,
    result: str | None = None,
    stop_reason: str | None = None,
    created_at: str | None = None,
) -> DelegationEntry:
    """造一条台账条目（§6.1）。``task`` 工具与 DurableContext 捕获共用这个入口。

    ``id`` 是 **tool_call_id**（对外相关键），不是 ``execution_id``：provider 的
    tool_call_id 不保证跨 run 唯一，而台账本来就是按 run 归属的（§2）。

    ``status`` / ``stop_reason`` 写错就抛：一个拼错的状态会变成"永远非终态"的条目，
    卡片转圈、总量上限也数不对——这种错要在测试里就炸，不要等到线上。
    """
    entry_id = str(id or "").strip()
    if not entry_id:
        raise ValueError("delegation entry needs a non-empty id (the task tool_call_id)")
    if status not in DELEGATION_STATUSES:
        raise ValueError(
            f"unknown delegation status {status!r}; expected one of {sorted(DELEGATION_STATUSES)}"
        )
    if stop_reason is not None and stop_reason not in STOP_REASONS:
        raise ValueError(f"unknown stop_reason {stop_reason!r}; expected one of {sorted(STOP_REASONS)}")

    entry: DelegationEntry = {
        "id": entry_id,
        "description": truncate_description(description),
        "subagent_type": str(subagent_type or ""),
        "status": status,
        "created_at": created_at or now_iso(),
    }
    if run_id:
        entry["run_id"] = run_id
    if stop_reason:
        entry["stop_reason"] = stop_reason
    text = result or ""
    if text.strip():
        entry["result_brief"] = truncate_brief(text)
        entry["result_sha256"] = result_sha256(text)
    return entry


def extract_delegation_entries(
    messages: Sequence[object],
    *,
    run_id: str | None = None,
    boundary_message_ids: Collection[str] | None = None,
    now: str | None = None,
) -> list[DelegationEntry]:
    """把 messages 里的 ``task`` 调用与它们的 ToolMessage 抽成条目（§6.3）。

    * 结果按 ``tool_call_id`` 配对：一轮 3 个并行 task 各归各的。
    * 没有结果的调用记 ``in_progress``：压缩之后它是"已经派出去过"的唯一记忆。
    * ``boundary_message_ids``（§10.2）：本 run 开始前就在 checkpoint 里的消息 id。
      命中的调用属于历史 run，**不**打当前 run_id；没给边界时保守地按当前 run 归属
      （偏严不偏松，和 Limit 中间件缺 run_id 时的策略一致）。
    * 状态只认 ToolMessage 上的**标记字段**，绝不解析散文（§13.2）：没有标记时，
      工具自己报错算 ``failed``，其余算 ``completed``。
    """
    calls: dict[str, tuple[object, bool]] = {}
    results: dict[str, object] = {}
    boundary = frozenset(boundary_message_ids or ())

    for message in messages:
        tool_calls = getattr(message, "tool_calls", None)
        if isinstance(tool_calls, list):
            message_id = getattr(message, "id", None)
            historical = isinstance(message_id, str) and message_id in boundary
            for call in tool_calls:
                if not isinstance(call, Mapping) or call.get("name") != TASK_TOOL_NAME:
                    continue
                call_id = call.get("id")
                if isinstance(call_id, str) and call_id and call_id not in calls:
                    calls[call_id] = (call.get("args"), historical)
            continue
        call_id = getattr(message, "tool_call_id", None)
        if isinstance(call_id, str) and call_id:
            results.setdefault(call_id, message)

    entries: list[DelegationEntry] = []
    for call_id, (args, historical) in calls.items():
        entry_run_id = None if historical else run_id
        result = results.get(call_id)
        if result is None:
            entries.append(
                make_delegation_entry(
                    id=call_id,
                    run_id=entry_run_id,
                    status=DELEGATION_STATUS_IN_PROGRESS,
                    created_at=now,
                    **_call_identity(args),
                )
            )
            continue
        entries.append(
            _entry_from_result(call_id, result, args=args, run_id=entry_run_id, now=now)
        )
    return entries


def render_delegation_ledger(
    entries: Sequence[Mapping] | None,
    *,
    budget: int = DELEGATION_LEDGER_RENDER_CHAR_BUDGET,
) -> str:
    """把台账渲染成有界文本（§6.4）。没有条目 → 空串（不留空标签给模型猜）。

    填充顺序**从最新往旧**：模型最需要的是刚发生的委派；装不下就丢掉更老的。
    输出顺序仍是时间顺序（老的在上）。
    """
    blocks: list[str] = []
    used = len(LEDGER_HEADER)
    for entry in reversed(list(entries or [])):
        if not isinstance(entry, Mapping):
            continue
        block = _render_entry(entry)
        if blocks and used + 1 + len(block) > budget:
            break
        blocks.append(block)
        used += 1 + len(block)
    if not blocks:
        return ""
    text = LEDGER_HEADER + "\n" + "\n".join(reversed(blocks))
    return text[:budget] if len(text) > budget else text


def _entry_from_result(call_id: str, result: object, *, args: object, run_id: str | None, now: str | None) -> DelegationEntry:
    kwargs = getattr(result, "additional_kwargs", None)
    kwargs = kwargs if isinstance(kwargs, Mapping) else {}
    status = kwargs.get("subagent_status")
    if status not in DELEGATION_STATUSES:
        status = "failed" if getattr(result, "status", None) == "error" else "completed"
    stop_reason = kwargs.get("subagent_stop_reason")
    entry = make_delegation_entry(
        id=call_id,
        run_id=run_id,
        status=status,
        stop_reason=stop_reason if stop_reason in STOP_REASONS else None,
        created_at=now,
        **_call_identity(args),
    )
    brief = kwargs.get("subagent_result_brief")
    if isinstance(brief, str) and brief.strip():
        # task 工具已经盖过章：brief 是它截过的、sha 是全文的——原样沿用，
        # 重算会让同一条委派在两条写入路径上得到两份指纹。
        sha = kwargs.get("subagent_result_sha256")
        entry["result_brief"] = brief[:DELEGATION_BRIEF_MAX_CHARS]
        entry["result_sha256"] = sha if isinstance(sha, str) and sha else result_sha256(brief)
        return entry
    content = message_content_to_text(getattr(result, "content", ""))
    if content.strip():
        entry["result_brief"] = truncate_brief(content)
        entry["result_sha256"] = result_sha256(content)
    return entry


def _call_identity(args: object) -> dict[str, object]:
    """从 task 调用参数里取"给人看的一行"：``description`` 优先，否则 ``prompt`` 首行。"""
    if not isinstance(args, Mapping):
        return {"description": "", "subagent_type": ""}
    description = args.get("description")
    prompt = args.get("prompt")
    subagent_type = args.get("subagent_type")
    text = description if isinstance(description, str) and description.strip() else ""
    if not text and isinstance(prompt, str) and prompt.strip():
        text = prompt.strip().splitlines()[0]
    return {
        "description": text,
        "subagent_type": subagent_type if isinstance(subagent_type, str) else "",
    }


def _render_entry(entry: Mapping) -> str:
    status = _escape(entry.get("status")) or "unknown"
    stop_reason = entry.get("stop_reason")
    label = f"{status}, {_escape(stop_reason)}" if stop_reason else status
    subagent_type = _escape(entry.get("subagent_type")) or "unknown"
    description = _escape(entry.get("description"))
    entry_id = _escape(entry.get("id"))
    head = f"- [{label}] {subagent_type}"
    if description:
        head += f" · {description}"
    head += f" (id={entry_id})"

    lines = [head]
    if stop_reason:
        lines.append(f"  {GUARDRAIL_GUIDANCE}")
    if entry.get("status") == DELEGATION_STATUS_IN_PROGRESS:
        lines.append(f"  {IN_PROGRESS_GUIDANCE}")
    brief = entry.get("result_brief")
    if isinstance(brief, str) and brief:
        sha = entry.get("result_sha256")
        marker = f" (sha256={_escape(sha)[:12]}…)" if isinstance(sha, str) and sha else ""
        lines.append(f"  结果{marker}: {_escape(brief)}")
    return "\n".join(lines)


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)
