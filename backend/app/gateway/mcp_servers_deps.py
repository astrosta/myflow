"""Dependency providers for the /api/mcp-servers router.

Composition lives outside route modules: the application composes one service at
startup, while route tests inject a narrow fake through app.state. Missing service
is a coded 503, not a 500 or an empty list.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request

from agentkit.config import get_app_config
from backend.app.gateway.auth.errors import Unauthenticated
from backend.app.gateway.deps import caller_from_request, require_authenticated
from backend.app.gateway.errors import McpServersDisabled
from backend.app.gateway.run_models import RunCaller

MCP_SERVERS_READ = "mcp_servers:read"
MCP_SERVERS_WRITE = "mcp_servers:write"


def get_mcp_server_service(request: Request) -> Any:
    """The process-owned service at app.state.mcp_server_service.

    That single documented key is what Task 8 publishes and what route tests
    inject; there is deliberately no second alias to drift from it.
    """
    service = getattr(request.app.state, "mcp_server_service", None)
    if service is not None:
        return service
    if not get_app_config().mcp_servers.enabled:
        raise McpServersDisabled("MCP server management is disabled")
    raise McpServersDisabled(
        "MCP server management is enabled but the service is not initialized"
    )


def require_mcp_servers_scope(scope: str) -> Callable[[Request], RunCaller]:
    """Any signed-in user may manage their own servers; PATs keep their scopes."""

    def _dep(request: Request) -> RunCaller:
        from backend.app.gateway.services import RunPermissionDenied

        require_authenticated(request)
        caller = caller_from_request(request)
        if not caller.user_id:
            raise Unauthenticated("authentication required")
        if not caller.allows(scope):
            raise RunPermissionDenied(f"missing scope: {scope}")
        return caller

    return _dep


def require_mcp_reader(request: Request) -> RunCaller:
    return require_mcp_servers_scope(MCP_SERVERS_READ)(request)


def require_mcp_writer(request: Request) -> RunCaller:
    return require_mcp_servers_scope(MCP_SERVERS_WRITE)(request)


__all__ = [
    "MCP_SERVERS_READ",
    "MCP_SERVERS_WRITE",
    "get_mcp_server_service",
    "require_mcp_reader",
    "require_mcp_servers_scope",
    "require_mcp_writer",
]
