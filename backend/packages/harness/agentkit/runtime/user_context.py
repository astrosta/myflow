"""当前用户 ContextVar + **唯一一条**身份优先级链（M9 §2.1 / §2.2 / §2.3）。

三个概念必须分清，混起来就是 9A 最典型的 bug：

- **原始 identity**：认证系统给的非空字符串（邮箱、uuid、平台 id）。授权过滤、JWT `sub`、
  `users` 表主键用它。**不保证**能进文件路径。
- **存储 ID**：`make_safe_user_id(identity)`，只含 `[A-Za-z0-9_-]`。
  文件系统分桶、sandbox 路径、memory 路径、`threads_meta.user_id` / `runs.user_id` 全用它。
  它的权威来源是 **`users.id`**（不可变），不是邮箱——改邮箱不能让用户的对话"消失"。
- **AUTO**：仓储层的哨兵，"从 ContextVar 解析"。解析不出来就抛，**绝不**退化成全局查询。

写 ContextVar 的只有 HTTP 中间件与内部入口；persistence 与工具只读。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextvars import ContextVar, Token
from typing import Any, Protocol

from agentkit.utils.ids import make_safe_user_id

logger = logging.getLogger(__name__)

#: 无认证 / 嵌入 / 内部无 HTTP 路径的桶。M2 的 `Paths` 与 M3 的 `AgentClient` 也是这个名字。
DEFAULT_USER_ID = "default"


class MissingUserContextError(RuntimeError):
    """AUTO was requested but no current user is set."""


class _AutoSentinel:
    def __repr__(self) -> str:
        return "AUTO"


AUTO = _AutoSentinel()
_current_user: ContextVar[str | None] = ContextVar("agentkit_current_user", default=None)


class _HasImmutableId(Protocol):
    """`users` 行的最小形状。harness 不认识 UserRecord，只要一个不可变主键。"""

    id: str


def _record_id(user: Any) -> str | None:
    """仓储返回的是 dict，ORM/数据类是对象——两种形状都要认。

    只认一个形状的后果很具体：gateway 里到处是 `row["id"]`，
    而 harness 的另一半是 `user.id`，中间就会长出第二份"从哪拿 id"的逻辑。
    """
    if isinstance(user, Mapping):
        candidate = user.get("id")
    else:
        candidate = getattr(user, "id", None)
    return candidate if isinstance(candidate, str) and candidate else None


def peek_current_user() -> str | None:
    """Soft read for writers that may run without a request. Missing context is None."""
    return _current_user.get()


def set_current_user(user_id: str | None) -> Token[str | None]:
    """HTTP 中间件 / 内部入口写；persistence 只读。"""
    return _current_user.set(user_id)


def reset_current_user(token: Token[str | None]) -> None:
    """Pair of set_current_user. Pass the token it returned."""
    _current_user.reset(token)


def resolve_user_id(user_id: str | None | _AutoSentinel, *, method_name: str) -> str | None:
    """把 AUTO 解析成当前用户。解析不出来就抛，绝不静默返回 None。

    显式 str 按该用户过滤。显式 None 是全局查询，调用方自己负责。
    """
    if not isinstance(user_id, _AutoSentinel):
        return user_id
    resolved = _current_user.get()
    if resolved is None:
        raise MissingUserContextError(
            f"{method_name}() called without an explicit user_id and no user context is set; "
            "background writers must pass record.user_id explicitly"
        )
    return resolved


def resolve_runtime_user_id(
    runtime_context: Mapping[str, Any] | None = None,
    *,
    server_identity: str | None = None,
    allow_default: bool = False,
) -> str | None:
    """把"这一轮是谁"解析成**存储 ID**（M9 §2.1）。

    图构建、中间件、记忆、技能、自定义 agent、提示词装配**全部调这一个函数**，
    否则会出现"图构建和中间件落进不同用户桶"——它只在多用户环境下出现，
    单用户测试完全看不出来。

    优先级链：

    1. 服务端认证注入的身份（`runtime.context.user_id`，由 AuthMiddleware /
       `inject_server_owned` 写入）。客户端塞的同名键在 `start_run` 里已被剥掉。
    2. `server_identity`：将来接 Agent Server 时的 `server_info.user.identity`，
       先 `make_safe_user_id`。
    3. 请求 ContextVar（`get_current_user`）：覆盖"只在 HTTP 层设过、没进 config"的路径。
    4. `allow_default=True` 时落到 `DEFAULT_USER_ID`（仅内部 / 嵌入 / 无 HTTP 路径）。
       否则返回 None——调用方自己决定这是不是错误。
    """
    if isinstance(runtime_context, Mapping):
        injected = runtime_context.get("user_id")
        if isinstance(injected, str) and injected:
            return make_safe_user_id(injected)

    if isinstance(server_identity, str) and server_identity:
        return make_safe_user_id(server_identity)

    current = _current_user.get()
    if current:
        return make_safe_user_id(current)

    return DEFAULT_USER_ID if allow_default else None


def get_effective_user_id(runtime_context: Mapping[str, Any] | None = None) -> str:
    """文件系统桶的唯一回退入口（§2.3）：解析不出来就用 `DEFAULT_USER_ID`。

    仓储查询**不要**用这个函数——那里要的是 `resolve_user_id(AUTO)` 的 fail-closed 语义。
    """
    return resolve_runtime_user_id(runtime_context, allow_default=True) or DEFAULT_USER_ID


def storage_user_id(user: _HasImmutableId | Mapping[str, Any] | None, *, raw_identity: str | None) -> str:
    """**存储 ID 从哪来**（§2.2）：有 `users` 行就用它的不可变主键。

    没有 `users` 行（嵌入模式 / 认证关闭）才退化到 identity。退化路径必须打一次
    warning：那意味着这份数据没有可迁移的账号归属。

    反面：桶名派生自 identity（邮箱）时，用户改一次邮箱就会看到自己的对话全没了，
    而数据库里的行都还在——排查方向会先指向仓储而不是路径。
    """
    if (identifier := _record_id(user)) is not None:
        return make_safe_user_id(identifier)
    if raw_identity:
        logger.warning("Falling back to identity-derived storage bucket for %r", raw_identity)
        return make_safe_user_id(raw_identity)
    return DEFAULT_USER_ID
