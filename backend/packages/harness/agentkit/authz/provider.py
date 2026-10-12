"""AuthzProvider：接口在，M9 的内置策略是全放行（§2.10 / §12）。

细粒度 RBAC 与工具级 deny 是 M10。这里只把**形状**定下来，
让 M10 换实现时不用回头改调用点（沙箱 acquire/reuse、子代理、路由）。

fail-closed 的方向写在调用方：只有 `AuthzDecision.allow` 才继续，
`deny`、异常、provider 缺失都当拒绝处理。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from agentkit.authz.principal import Principal


class AuthzDecision(StrEnum):
    allow = "allow"
    deny = "deny"


class AuthzDeniedError(PermissionError):
    """provider 拒绝，或 provider 本身出错（fail-closed）。"""

    def __init__(self, action: str, *, reason: str) -> None:
        super().__init__(f"authorization denied for {action}: {reason}")
        self.action = action
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AuthzRequest:
    action: str
    resource: Mapping[str, Any] | None = None


@runtime_checkable
class AuthzProvider(Protocol):
    def authorize(self, principal: Principal, request: AuthzRequest) -> AuthzDecision: ...


class AllowAllAuthzProvider:
    """M9 的内置实现：接口在，策略全放行。工具级 deny 是 M10。"""

    def authorize(self, principal: Principal, request: AuthzRequest) -> AuthzDecision:
        del principal, request
        return AuthzDecision.allow


_provider: AuthzProvider = AllowAllAuthzProvider()


def get_authz_provider() -> AuthzProvider:
    return _provider


def set_authz_provider(provider: AuthzProvider | None) -> None:
    """M10（或测试）注入细粒度实现。传 None 恢复 AllowAll。"""
    global _provider
    _provider = provider if provider is not None else AllowAllAuthzProvider()


def check_authorization(
    principal: Principal,
    action: str,
    *,
    resource: Mapping[str, Any] | None = None,
) -> AuthzDecision:
    """问一次策略。provider 抛异常 = 拒绝，不是放行。"""
    request = AuthzRequest(action=action, resource=resource)
    try:
        decision = get_authz_provider().authorize(principal, request)
    except Exception as exc:  # noqa: BLE001 —— fail-closed：策略坏了不等于放行
        raise AuthzDeniedError(action, reason=f"provider error: {exc!r}") from exc
    if decision is not AuthzDecision.allow:
        raise AuthzDeniedError(action, reason=str(decision))
    return decision
