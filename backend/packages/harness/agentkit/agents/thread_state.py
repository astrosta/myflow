"""Checkpoint schema. Reducer slots are fixed here; later milestones only fill them."""

from __future__ import annotations

from typing import Annotated, NotRequired, TypedDict

from langchain.agents import AgentState

_LEDGER_MAX_ENTRIES = 50


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


class DelegationEntry(TypedDict, total=False):
    id: str


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
    """Merge by id, keep first-seen order, drop the oldest past 50. None and [] leave the ledger."""
    if not new:
        return list(existing or [])
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for entry in [*(existing or []), *new]:
        entry_id = entry["id"]
        if entry_id not in by_id:
            order.append(entry_id)
        by_id[entry_id] = entry
    merged = [by_id[entry_id] for entry_id in order]
    if len(merged) > _LEDGER_MAX_ENTRIES:
        return merged[-_LEDGER_MAX_ENTRIES:]
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
