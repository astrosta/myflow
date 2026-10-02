"""Current-user ContextVar. Persistence reads it; HTTP middleware writes it."""

from __future__ import annotations

from contextvars import ContextVar, Token


class MissingUserContextError(RuntimeError):
    """AUTO was requested but no current user is set."""


class _AutoSentinel:
    def __repr__(self) -> str:
        return "AUTO"


AUTO = _AutoSentinel()
_current_user: ContextVar[str | None] = ContextVar("agentkit_current_user", default=None)


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
