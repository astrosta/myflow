"""Re-exports for ``config.sandbox.use`` consumers and tool wiring."""

from agentkit.sandbox.exceptions import (
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxTimeoutError,
)
from agentkit.sandbox.sandbox import GrepMatch, Sandbox
from agentkit.sandbox.sandbox_provider import (
    SandboxProvider,
    get_sandbox_provider,
    reset_sandbox_provider,
)
from agentkit.sandbox.security import is_host_bash_allowed

__all__ = [
    "GrepMatch",
    "Sandbox",
    "SandboxError",
    "SandboxNotFoundError",
    "SandboxProvider",
    "SandboxRuntimeError",
    "SandboxTimeoutError",
    "ensure_sandbox_initialized",
    "ensure_sandbox_initialized_async",
    "get_sandbox_provider",
    "is_host_bash_allowed",
    "reset_sandbox_provider",
]

_RUNTIME_EXPORTS = {
    "ensure_sandbox_initialized",
    "ensure_sandbox_initialized_async",
}


def __getattr__(name: str):
    if name not in _RUNTIME_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module("agentkit.sandbox.runtime"), name)
    globals()[name] = value
    return value
