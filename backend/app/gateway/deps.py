"""Dependency providers. Routes take process singletons from here, not from app.state."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from agentkit.agents.memory.manager import MemoryManager, get_memory_manager
from agentkit.config import get_app_config
from agentkit.config.knowledge_config import KnowledgeBaseConfig
from agentkit.knowledge.errors import KnowledgeDisabled
from agentkit.knowledge.service import KnowledgeService
from agentkit.persistence.engine import get_session_factory
from agentkit.runtime.run_services import RunServices
from fastapi import Request

from backend.app.gateway.auth.errors import Unauthenticated
from backend.app.gateway.auth.result import AuthKind, AuthResult
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.store import make_user_store
from backend.app.gateway.auth_middleware import require_session
from backend.app.gateway.run_models import CallerKind, RunCaller

__all__ = [
    "RunServices",
    "caller_from_request",
    "get_auth_service",
    "get_checkpointer",
    "get_event_store",
    "get_knowledge_config",
    "get_knowledge_service",
    "get_knowledge_service_optional",
    "get_memory_manager_service",
    "get_request_id",
    "get_run_services",
    "get_run_store",
    "get_session_auth",
    "get_thread_store",
    "require_all_scopes",
    "require_authenticated",
    "require_scope",
]


def caller_from_request(request: Request) -> RunCaller:
    """M3 的匿名实现到这里换成读中间件（M9 §2.12）。**body 里的身份仍然不看。**

    `auth.scopes is None` 表示会话用户或内部调用方（不按 scope 裁）；
    PAT 带具体集合。内部调用是 `CallerKind.INTERNAL`，`user_id` 来自
    `X-MyFlow-Owner-User-Id`，而且那只头**只有** AuthMiddleware 读（§2.9）。
    """
    auth: AuthResult | None = getattr(request.state, "auth", None)
    if auth is None:
        return RunCaller(kind=CallerKind.HTTP, user_id=None)
    kind = CallerKind.INTERNAL if auth.kind is AuthKind.internal else CallerKind.HTTP
    return RunCaller(kind=kind, user_id=auth.user_id, scopes=auth.scopes, user_role=auth.role)


def get_auth_service(request: Request) -> AuthService:
    """进程级单例。`database.backend=memory` 时账号只活在进程内，构建一次就够。"""
    service = getattr(request.app.state, "auth_service", None)
    if isinstance(service, AuthService):
        return service
    service = AuthService(make_user_store(get_session_factory()))
    request.app.state.auth_service = service
    return service


def get_session_auth(request: Request) -> AuthResult:
    """只允许会话的路由用它（`/me`、`change-password`）。PAT → 403 `session_required`。"""
    return require_session(getattr(request.state, "auth", None))


def require_authenticated(request: Request) -> AuthResult:
    """认证门的**函数版**：装在依赖里，函数体第一行不再自己判断。"""
    auth: AuthResult | None = getattr(request.state, "auth", None)
    if auth is None or not auth.is_authenticated:
        raise Unauthenticated("authentication required")
    return auth


def get_memory_manager_service(request: Request) -> MemoryManager:
    """Resolve the process manager (lifespan-owned when running the real app)."""
    manager = getattr(request.app.state, "memory_manager", None)
    if not isinstance(manager, MemoryManager):
        manager = get_memory_manager()
    request.state.memory_manager = manager
    request.state.memory_maintenance = getattr(request.app.state, "memory_maintenance", None)
    return manager


def get_run_services(request: Request) -> RunServices:
    return request.app.state.run_services


def get_thread_store(request: Request):
    return get_run_services(request).run_context.thread_store


def get_run_store(request: Request):
    return get_run_services(request).run_store


def get_event_store(request: Request):
    return get_run_services(request).event_store


def get_checkpointer(request: Request):
    return get_run_services(request).run_context.checkpointer


def get_knowledge_service_optional(request: Request) -> KnowledgeService | None:
    # app.state is process-owned; tests may inject a narrow fake implementing the service API.
    service = getattr(request.app.state, "knowledge_service", None)
    return service if service is not None else None


def get_knowledge_service(request: Request) -> KnowledgeService:
    service = get_knowledge_service_optional(request)
    if service is None:
        raise KnowledgeDisabled("Knowledge base is disabled or not initialized")
    return service


def get_knowledge_config(request: Request) -> KnowledgeBaseConfig:
    config = getattr(request.app.state, "knowledge_config", None)
    if isinstance(config, KnowledgeBaseConfig):
        return config
    return get_app_config().knowledge_base


def get_request_id(request: Request) -> str:
    trace_id = getattr(request.state, "trace_id", None)
    if not trace_id:
        trace_id = uuid4().hex
        request.state.trace_id = trace_id
    return trace_id


def require_scope(scope: str) -> Callable[[Request], RunCaller]:
    """PAT missing this scope is 403. A session or internal caller has scopes=None and passes."""

    def _dep(request: Request) -> RunCaller:
        from backend.app.gateway.services import RunPermissionDenied

        caller = caller_from_request(request)
        if not caller.allows(scope):
            raise RunPermissionDenied(f"missing scope: {scope}")
        return caller

    return _dep


def require_all_scopes(*scopes: str) -> Callable[[Request], RunCaller]:
    """**全部**满足才放行（M9 §2.12：定时任务路由要同时有 `threads:write` 和 `runs:create`）。"""

    def _dep(request: Request) -> RunCaller:
        from backend.app.gateway.services import RunPermissionDenied

        caller = caller_from_request(request)
        missing = [scope for scope in scopes if not caller.allows(scope)]
        if missing:
            raise RunPermissionDenied(f"missing scope: {', '.join(missing)}")
        return caller

    return _dep
