"""公开 mode 名和 LangGraph 内部 mode 名的映射。"""

from __future__ import annotations

# 请求里的公开名 → LangGraph astream 的 stream_mode。
# messages-tuple 是公开参数名；图内部和 SSE event 都叫 messages。
PUBLIC_TO_INTERNAL: dict[str, str] = {
    "values": "values",
    "messages-tuple": "messages",
    "updates": "updates",
    "custom": "custom",
}


class UnsupportedStreamModeError(Exception):
    """客户端请求了网关不提供的 stream mode。"""

    def __init__(self, mode: object) -> None:
        self.mode = mode
        supported = ", ".join(PUBLIC_TO_INTERNAL)
        super().__init__(f"Unsupported stream mode {mode!r}; supported modes: {supported}")


def normalize_stream_modes(raw: str | list[str] | None) -> list[str]:
    """校验公开 stream mode，并保留公开名。

    ``None`` 变成 ``["values"]``。单个字符串包成单项列表。
    内部名 ``messages``、以及 ``events`` 等未支持的名字抛
    ``UnsupportedStreamModeError``，不静默换成 ``values``。
    """
    if raw is None:
        return ["values"]

    modes = [raw] if isinstance(raw, str) else list(raw)
    for mode in modes:
        if mode not in PUBLIC_TO_INTERNAL:
            raise UnsupportedStreamModeError(mode)
    return modes


def to_internal_stream_modes(public_modes: list[str]) -> list[str]:
    """把已规范化的公开名映射成 LangGraph 内部 stream_mode。"""
    try:
        return [PUBLIC_TO_INTERNAL[mode] for mode in public_modes]
    except KeyError as exc:
        raise UnsupportedStreamModeError(exc.args[0]) from exc
