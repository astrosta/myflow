"""Dependency providers for the ContextForge tool-management router.

Kept outside routers so route modules never read `request.app` directly (the
gateway keeps all composition and lifecycle access in the app layer).
"""

from __future__ import annotations

from fastapi import Request

from agentkit.config import get_app_config
from agentkit.contextforge.admin import ContextForgeAdminClient
from agentkit.contextforge.runtime import get_contextforge_runtime
from backend.app.gateway.deps import caller_from_request, require_authenticated
from backend.app.gateway.errors import ContextForgeDisabled
from backend.app.gateway.run_models import RunCaller


def require_tools_scope(scope: str):
    """Any signed-in user may use the shared catalog; PATs keep their scopes.

    ContextForge credentials stay server-side. Session callers have
    scopes=None and pass; a PAT must carry tools:read / tools:write.
    """

    def _dep(request: Request) -> RunCaller:
        from backend.app.gateway.services import RunPermissionDenied

        require_authenticated(request)
        caller = caller_from_request(request)
        if not caller.allows(scope):
            raise RunPermissionDenied(f"missing scope: {scope}")
        return caller

    return _dep


def get_contextforge_admin_client(request: Request) -> ContextForgeAdminClient:
    runtime = get_contextforge_runtime()
    if runtime is not None:
        return runtime.admin
    config = get_app_config().contextforge
    if not config.enabled:
        raise ContextForgeDisabled("ContextForge tool management is disabled")
    client = getattr(request.app.state, "contextforge_admin_client", None)
    if client is not None:
        return client
    client = ContextForgeAdminClient(config)
    request.app.state.contextforge_admin_client = client
    return client


__all__ = ["get_contextforge_admin_client", "require_tools_scope"]
