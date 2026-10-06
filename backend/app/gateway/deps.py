"""Dependency providers. Routes take process singletons from here, not from app.state."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from fastapi import Request

from agentkit.runtime.run_services import RunServices
from backend.app.gateway.run_models import CallerKind, RunCaller

__all__ = [
    "RunServices",
    "caller_from_request",
    "get_checkpointer",
    "get_event_store",
    "get_request_id",
    "get_run_services",
    "get_run_store",
    "get_thread_store",
    "require_scope",
]


def caller_from_request(request: Request) -> RunCaller:
    """M3 has no auth: every HTTP caller is anonymous. M9 reads request.state.auth here."""
    return RunCaller(kind=CallerKind.HTTP, user_id=None)


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

