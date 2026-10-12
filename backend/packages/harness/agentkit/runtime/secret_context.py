"""运行上下文里的保留键与 owner token（M7 §3.6/§3.9）。

slash 源路径和工具策略决策都写在 runtime.context 的保留键里。runtime.context 会被
调用方合并，所以读必须持有组装时生成、**只传给 Activation 与 Policy 这一对**的 token：
没有 token 的伪造路径必须当无。

这些键全部进 REDACTED_CONTEXT_KEYS：可观察 / 持久化的 context 副本里看不到它们
（网关的 redact_context_secrets 读这个集合）。
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from collections.abc import Mapping, MutableMapping

logger = logging.getLogger(__name__)

#: 请求级密钥（M3 白名单放行）：值请求级、绝不进消息、绝不落盘（§4/M7 §5.5）。
REQUEST_SECRETS_KEY = "secrets"

SLASH_SKILL_SOURCE_CONTEXT_KEY = "__slash_skill_secret_source"
SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY = "__skill_tool_policy_decision"
SKILL_ACTIVATION_STATE_CONTEXT_KEY = "__skill_activation_state"

#: 这些键不许出现在被持久化 / 被回显的 context 副本里。
REDACTED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        SLASH_SKILL_SOURCE_CONTEXT_KEY,
        SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
        SKILL_ACTIVATION_STATE_CONTEXT_KEY,
    }
)

_TOKEN_BYTES = 24


def extract_request_secrets(context: Mapping[str, Any] | None) -> dict[str, str]:
    """从 runtime.context 里取请求级密钥。

    **必须在工具/middleware 里从 runtime 读**，不要在工具内部调 get_config()["context"]：
    运行上下文挂在 runtime 上，不在传给子 runnable 的 RunnableConfig 里（§5.4）。
    """
    if not isinstance(context, Mapping):
        return {}
    raw = context.get(REQUEST_SECRETS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(value, str) and value
    }


def new_owner_token() -> str:
    """组装时生成一次，只传给 Activation 和 Policy 这一对（§3.6）。"""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def write_secret_context(
    context: Mapping[str, Any] | MutableMapping[str, Any] | None,
    key: str,
    value: Any,
    *,
    token: str,
) -> bool:
    """把 value 包在 {token, value} 里写进保留键。context 不可写时返回 False。"""
    if not isinstance(context, MutableMapping):
        logger.debug("runtime.context is not mutable; dropping %s", key)
        return False
    context[key] = {"token": token, "value": value}
    return True


def read_secret_context(
    context: Mapping[str, Any] | None, key: str, *, token: str
) -> Any | None:
    """token 对不上（伪造 / 外来 / 换了一对中间件）一律当无。"""
    if not isinstance(context, Mapping):
        return None
    entry = context.get(key)
    if not isinstance(entry, Mapping):
        return None
    stored = entry.get("token")
    if not isinstance(stored, str) or not secrets.compare_digest(stored, token):
        return None
    return entry.get("value")


def clear_secret_context(
    context: Mapping[str, Any] | MutableMapping[str, Any] | None, key: str, *, token: str
) -> None:
    if not isinstance(context, MutableMapping):
        return
    if read_secret_context(context, key, token=token) is not None:
        context.pop(key, None)
