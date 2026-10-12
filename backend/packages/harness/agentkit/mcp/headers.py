"""Header 名大小写与非法值（M7 §5.6）。

HTTP 字段名大小写不敏感，但一路上的 dict 都是大小写敏感的：静态写了 authorization、
注入写 Authorization，两个都到达 httpx，服务端单值访问器拿到的是**静态那个**，
优先级被静默反转。
"""

from __future__ import annotations

from collections.abc import Mapping


def illegal_header_value_reason(value: str) -> str | None:
    """返回"为什么非法"的短语；**绝不把 value 编进返回值**（§5.6）。

    这个字符串会经 ToolErrorHandlingMiddleware 进模型可见消息、checkpoint 和 trace；
    h11 自己的异常消息里带着完整凭据值，所以传输层之前必须先拦下来。
    """
    if any(ord(ch) > 0x7E or ord(ch) < 0x20 for ch in value):
        return "header value contains non-ASCII or control characters"
    if value != value.strip(" \t"):
        return "header value has leading or trailing whitespace"
    return None


def apply_header_overrides(
    base: Mapping[str, str] | None, override: Mapping[str, str] | None
) -> dict[str, str]:
    """大小写不敏感地覆盖：只有大小写不同的旧键被删掉，注入方赢 —— 最终只留一个键（§5.6）。

    两个键同时到达 httpx 时，服务端单值访问器读到的可能是静态那个，优先级被静默反转。
    """
    merged = dict(base or {})
    lowered = {key.lower(): key for key in merged}
    for key, value in (override or {}).items():
        existing = lowered.get(key.lower())
        if existing is not None and existing != key:
            del merged[existing]  # 否则两个键都到达 httpx，服务端单值访问器取到静态那个
            lowered.pop(key.lower(), None)
        merged[key] = value
        lowered[key.lower()] = key
    return merged


def duplicate_header_spellings(headers: Mapping[str, str] | None) -> tuple[str, ...]:
    """同一个 header 出现两种拼写 → 返回那些拼写（配置加载时直接拒，§5.6）。"""
    seen: dict[str, list[str]] = {}
    for key in headers or {}:
        seen.setdefault(key.lower(), []).append(key)
    return tuple(sorted(key for keys in seen.values() if len(keys) > 1 for key in keys))
