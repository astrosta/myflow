"""公开 mode 名和 LangGraph 内部 mode 名的映射。"""

from __future__ import annotations

from typing import Literal

from typing_extensions import get_args

RunStreamMode = Literal["values", "messages-tuple", "updates", "debug", "tasks", "checkpoints", "custom"]
SUPPORTED_RUN_STREAM_MODES: frozenset[str] = frozenset(get_args(RunStreamMode))

class UnsupportedStreamModeError(ValueError):
    def __init__(self, modes: list[str]) -> None:
        self.modes = tuple(dict.fromkeys(modes))          # 去重、保序，给 422 正文用
        super().__init__(f"Unsupported stream mode(s): {', '.join(self.modes)}")


def normalize_stream_modes(raw: list[str] | str | None) -> list[str]:
    """入口校验：None / [] → ["values"]；str → [str]；未知或非 str → UnsupportedStreamModeError。"""
    if raw is None:
        modes = ["values"]
    elif isinstance(raw, str):
        modes = [raw]
    else:
        modes = raw or ["values"]
    unsupported = [
        mode if isinstance(mode, str) else type(mode).__name__
        for mode in modes
        if not isinstance(mode, str) or mode not in SUPPORTED_RUN_STREAM_MODES
    ]
    if unsupported:
        raise UnsupportedStreamModeError(unsupported)
    return modes


def to_langgraph_stream_modes(raw: list[str] | str | None) -> list[str]:
    """公开名 → astream 的内部名。恒返回 list；只把 messages-tuple 换成 messages，其余原样。返回list,为了astream好处理"""
    mapped = ["messages" if mode == "messages-tuple" else mode for mode in normalize_stream_modes(raw)]
    return list(dict.fromkeys(mapped)) # 去重保序：values + messages-tuple + values → [values, messages]

