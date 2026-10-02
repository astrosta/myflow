"""
依赖注入： bridge/run_manager/config
"""
from fastapi import Request

from agentkit.runtime.run_services import RunServices
from backend.app.gateway.run_models import CallerKind, RunCaller

__all__ = ["RunServices", "caller_from_request", "get_run_services"]


def caller_from_request(request: Request) -> RunCaller:
    return RunCaller(kind=CallerKind.HTTP, user_id=None) #M9 从AuthMiddleware里读

def get_run_services(request: Request) -> RunServices:
    return request.app.state.run_services

