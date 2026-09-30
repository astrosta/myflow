"""
依赖注入： bridge/run_manager/config
"""
from dataclasses import dataclass

from fastapi import Request

from agentkit.runtime.context import RunContext
from agentkit.runtime.runs import RunManager
from agentkit.runtime.stream_bridge.base import StreamBridge
from backend.app.gateway.run_models import CallerKind, RunCaller


def caller_from_request(request: Request) -> RunCaller:
    return RunCaller(kind=CallerKind.HTTP, user_id=None) #M9 从AuthMiddleware里读

@dataclass(frozen=True, slots=True)
class RunServices:
    """lifespan 建一次，挂在app.state.run_services."""
    run_manager: RunManager
    bridge: StreamBridge
    run_context: RunContext  #进程级runcontext

def get_run_services(request: Request) -> RunServices:
    return request.app.state.run_services

