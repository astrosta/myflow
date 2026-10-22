"""Checkpoint schema. Reducer slots are fixed here; later milestones only fill them."""

from __future__ import annotations

from typing import Annotated, NotRequired, TypedDict

from langchain.agents import AgentState

#: 台账最多留几条（M8 §6.1）。50 条 × (brief ≤ 2000 字) 已经是 checkpoint 的上限；
#: 更早的历史靠 ``subagent.end`` 事件翻，不要往 state 里塞（M1 §2.3）。
DELEGATION_LEDGER_MAX_ENTRIES = 50

#: 非终态：已经派出去、还没有 ToolMessage 的委派。压缩之后它是"已经派出去过"的
#: **唯一**记忆；丢了它 lead 会再派一次同一个活（§6.3）。
DELEGATION_STATUS_IN_PROGRESS = "in_progress"

#: 终态。**不可被非终态覆盖**（§6.2）：取消竞态里 worker 晚到的 ``in_progress``
#: 不能把已经落地的结论复活成"还在跑"。
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out", "polling_timed_out"})

#: 台账允许出现的全部 status。§9.3 的跨语言契约
#: （``contracts/subagent_status_contract.json``）必须与它双向一致。
DELEGATION_STATUSES = frozenset({DELEGATION_STATUS_IN_PROGRESS, *TERMINAL_STATUSES})

#: 三条护栏轴（§13）。命中了状态仍然是 completed / failed，**另外**带一个 stop_reason；
#: 不新增状态枚举值，老消费者直接忽略这个可选字段。
STOP_REASONS = frozenset({"token_capped", "turn_capped", "loop_capped"})


class SandboxState(TypedDict):
    sandbox_id: str


class ThreadDataState(TypedDict, total=False):
    """Absolute host paths for this thread's sandbox directories."""

    workspace_path: str
    uploads_path: str
    outputs_path: str


class ViewedImageData(TypedDict):
    """Metadata only. Image bytes stay on disk."""

    mime_type: str
    size: int
    actual_path: str


class DelegationEntry(TypedDict):
    """一条委派台账（M8 §6.1）。

    **state 里不放完整结果**：完整文本在 ``ToolMessage.content`` 和 ``subagent.end``
    事件里，这里只留 brief + sha256，免得 checkpoint 被 50 次委派撑爆。

    这张表**不写** ``receipt_verdict`` / ``acceptance_verdict``（§20）：M8 不做回执
    校验，填了前端会当成已校验。
    """

    id: str
    description: str
    subagent_type: str
    status: str
    created_at: str
    run_id: NotRequired[str]
    result_brief: NotRequired[str]
    result_sha256: NotRequired[str]
    stop_reason: NotRequired[str]


class SkillEntry(TypedDict):
    name: str
    path: str
    description: str


def merge_sandbox(existing, new):
    """Same sandbox_id may be written twice in one step. Two different ids are a bug."""
    if new is None:
        return existing
    if existing is None:
        return new
    if existing.get("sandbox_id") == new.get("sandbox_id"):
        return existing
    raise ValueError(
        f"Conflicting sandbox state: {existing.get('sandbox_id')!r} != {new.get('sandbox_id')!r}"
    )


def _merge_unique(existing, new):
    """Append and dedupe, first-seen order wins. None means the node did not touch the list."""
    if new is None:
        return list(existing or [])
    return list(dict.fromkeys([*(existing or []), *new]))


def merge_artifacts(existing, new):
    return _merge_unique(existing, new)


def merge_promoted(existing, new):
    return _merge_unique(existing, new)


def merge_todos(existing, new):
    """None means untouched. A list, including [], is an explicit replacement."""
    return existing if new is None else new


def merge_viewed_images(existing, new):
    """None means untouched. {} clears. Any other dict merges, new keys winning."""
    if new is None:
        return dict(existing or {})
    if len(new) == 0:
        return {}
    return {**(existing or {}), **new}


def merge_delegations(existing, new):
    """台账 reducer（M8 §6.2）：

    * ``new`` 为 None / [] → 保留 existing（这个节点没碰台账）
    * 按 id 合并，**首次出现顺序**保留
    * 已是终态的条目，拒绝被非终态覆盖（取消竞态：晚到的 ``in_progress`` 不能复活它）
    * ``created_at`` / ``run_id`` 以首次写入为准
    * 超过 50 条截**最老的**

    终态之间的覆盖（例如 ``timed_out`` → ``completed``）由写入方负责：执行器用
    ``try_set_terminal`` 保证"第一次终态获胜"，reducer 只保证"不能复活"。
    """
    if not new:
        return list(existing or [])
    by_id: dict[str, DelegationEntry] = {}
    order: list[str] = []
    for entry in [*(existing or []), *new]:
        entry_id = entry["id"]
        previous = by_id.get(entry_id)
        if (
            previous is not None
            and previous["status"] in TERMINAL_STATUSES
            and entry["status"] not in TERMINAL_STATUSES
        ):
            continue
        if previous is None:
            order.append(entry_id)
        else:
            if previous.get("created_at"):
                entry = {**entry, "created_at": previous["created_at"]}
            if previous.get("run_id") and not entry.get("run_id"):
                entry = {**entry, "run_id": previous["run_id"]}
        by_id[entry_id] = entry
    merged = [by_id[entry_id] for entry_id in order]
    if len(merged) > DELEGATION_LEDGER_MAX_ENTRIES:
        return merged[-DELEGATION_LEDGER_MAX_ENTRIES:]
    return merged


def merge_skill_context(existing, new):
    """Merge by skill name. None and [] leave the current list."""
    if not new:
        return list(existing or [])
    by_name: dict[str, dict] = {}
    order: list[str] = []
    for entry in [*(existing or []), *new]:
        name = entry["name"]
        if name not in by_name:
            order.append(name)
        by_name[name] = entry
    return [by_name[name] for name in order]


SandboxStateField = Annotated[SandboxState | None, merge_sandbox]

THREAD_STATE_REDUCER_FIELDS = frozenset({
    "messages",
    "sandbox",
    "artifacts",
    "todos",
    "viewed_images",
    "delegations",
    "skill_context",
    "promoted",
})


class ThreadState(AgentState):
    # Inherited from AgentState: messages.

    sandbox: SandboxStateField
    artifacts: Annotated[list[str], merge_artifacts]
    todos: Annotated[list | None, merge_todos]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    delegations: Annotated[list[DelegationEntry], merge_delegations]
    skill_context: Annotated[list[SkillEntry], merge_skill_context]
    promoted: Annotated[list[str], merge_promoted]

    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    uploaded_files: NotRequired[list[dict] | None]
    summary_text: NotRequired[str | None]


def get_thread_state_schema():
    """Checkpoint schema for rollback writes. Channel additions belong here."""
    return ThreadState
