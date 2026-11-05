"""Graph-level human confirmation for the profile write tool.

The tool itself remains a pure writer. Pausing belongs to the agent graph:
HumanInTheLoopMiddleware raises a LangGraph interrupt after the model asks
for update_user_profile and before the tool node executes. The client
resumes the same thread with Command(resume={"decisions": [...]}).
"""

from __future__ import annotations

import json
from typing import Any

from langchain.agents.middleware import HumanInTheLoopMiddleware

from agentkit.agents.memory.openviking.profile_tools import TOOL_NAME
from agentkit.agents.memory.profile_schema import PROFILE_FIELD_SPECS

_SPECS_BY_PATH = {spec.path: spec for spec in PROFILE_FIELD_SPECS}


def describe_profile_update(
    tool_call: dict[str, Any],
    state: Any,
    runtime: Any,
) -> str:
    """Human-readable confirmation text shown before the write."""

    del state, runtime
    args = tool_call.get("args")
    fields = args.get("fields") if isinstance(args, dict) else None
    lines = [
        "模型准备更新你的「我的画像」。",
        "只有你确认后才会写入；拒绝则不会修改。",
    ]
    if isinstance(fields, dict) and fields:
        lines.extend(["", "将要变更："])
        for path, value in fields.items():
            spec = _SPECS_BY_PATH.get(path)
            label = spec.label if spec is not None else path
            rendered = "（删除）" if value is None else json.dumps(value, ensure_ascii=False)
            lines.append(f"- {label}（{path}）：{rendered}")
    else:
        lines.extend(["", "（这次调用没有可识别的字段变更）"])

    note = args.get("note") if isinstance(args, dict) else None
    if isinstance(note, str) and note.strip():
        lines.extend(["", f"说明：{note.strip()}"])
    return "\n".join(lines)


def make_profile_interrupt_middleware() -> HumanInTheLoopMiddleware:
    """Interrupt only update_user_profile; other tools stay automatic."""

    return HumanInTheLoopMiddleware(
        interrupt_on={
            TOOL_NAME: {
                "allowed_decisions": ["approve", "reject"],
                "description": describe_profile_update,
            }
        },
    )


__all__ = ["describe_profile_update", "make_profile_interrupt_middleware"]
