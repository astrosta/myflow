"""Principal：**构造入口只有一个**（M9 §2.10）。

```
runtime.context  ──build_principal_from_context──▶  Principal  ──AuthzProvider──▶  allow / deny
```

字段里 `user_id` 是**原始 identity**（授权过滤用），不是存储 ID；
要拼路径请用 `agentkit.runtime.user_context` 的 `storage_user_id` / `get_effective_user_id`。

规则（写错一条就会串号）：

- 缺 / 空的 role 才落到 `default_role`；**非空但未知**的 role 原样保留——
  M9 的路由层只认 admin / user，未知角色交给执行层 fail-closed（M10 护栏），
  不在这里被悄悄改写成 default。
- `is_internal` 必须 `is True`。`"false"` / `1` / `"yes"` 都不算——
  客户端塞的字符串真值不能把一轮请求抬成内部调用。
- `authz_attributes` 不是 Mapping 直接 `TypeError`，不要静默 `{}`：
  "属性丢了" 和 "属性空" 在策略层是两件事。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ROLE = "user"

#: M9 只认这两个角色（§2.4）。细粒度 RBAC 见 §12。
ADMIN_ROLE = "admin"
KNOWN_ROLES = frozenset({ADMIN_ROLE, DEFAULT_ROLE})


class AuthzAttributesError(TypeError):
    """authz_attributes 不是 Mapping。fail-closed，不静默降级成空字典。"""


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str | None = None
    role: str | None = None
    oauth_provider: str | None = None
    oauth_id: str | None = None
    channel_user_id: str | None = None
    is_internal: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)

    def context_fields(self) -> dict[str, Any]:
        """拷进子代理 runtime.context 的字段（M8 `task` 工具用，§2.10）。

        `is_internal` **即使是 False 也要拷**：子代理的归属必须和 lead 完全一致，
        "缺省不拷"会让子代理在另一个桶里跑。`channel_user_id` 同理（9D 群聊身份不断档）。
        """
        return {
            "user_id": self.user_id,
            "user_role": self.role,
            "is_internal": self.is_internal,
            "channel_user_id": self.channel_user_id,
            "authz_attributes": dict(self.attributes),
            "oauth_provider": self.oauth_provider,
            "oauth_id": self.oauth_id,
        }


def normalize_authz_attributes(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AuthzAttributesError(
            f"authz_attributes must be a mapping, got {type(value).__name__}"
        )
    return dict(value)


def build_principal_from_context(
    context: Mapping[str, Any] | None,
    *,
    default_role: str = DEFAULT_ROLE,
) -> Principal:
    """从 runtime.context 建 Principal。**唯一**的构造入口。"""
    source: Mapping[str, Any] = context if isinstance(context, Mapping) else {}

    role = source.get("user_role")
    if role is None or role == "":
        role = default_role

    user_id = source.get("user_id")
    channel_user_id = source.get("channel_user_id")

    return Principal(
        user_id=user_id if isinstance(user_id, str) and user_id else None,
        role=role,
        oauth_provider=_optional_str(source.get("oauth_provider")),
        oauth_id=_optional_str(source.get("oauth_id")),
        channel_user_id=channel_user_id if isinstance(channel_user_id, str) and channel_user_id else None,
        is_internal=source.get("is_internal") is True,  # 必须 is True，别的真值不算
        attributes=normalize_authz_attributes(source.get("authz_attributes")),
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
