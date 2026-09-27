"""The only bridge from tools to a sandbox. M9 authorization mounts here."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from langchain.tools import ToolRuntime

from agentkit.sandbox.exceptions import SandboxNotFoundError, SandboxRuntimeError
from agentkit.sandbox.sandbox import Sandbox
from agentkit.sandbox.sandbox_provider import get_sandbox_provider
from agentkit.sandbox.security import safe_app_config

_DEFAULT_USER_ID = "default"
_AUTHORIZATION_CHECKED: ContextVar[bool] = ContextVar("sandbox_authorization_checked", default=False)


def reset_authorization_checked() -> None:
    _AUTHORIZATION_CHECKED.set(False)


def authorize_sandbox_execution(*, context: dict[str, Any], app_config: Any) -> None:
    """M9 fills this in. The call site stays on both the reuse and acquire paths."""
    del context, app_config


def resolve_runtime_user_id(runtime: ToolRuntime) -> str:
    context = runtime.context or {}
    if isinstance(context, dict) and (user_id := context.get("user_id")):
        return str(user_id)
    return _DEFAULT_USER_ID


def _resolve_runtime_thread_id(runtime: ToolRuntime) -> str | None:
    context = runtime.context or {}
    if isinstance(context, dict) and (thread_id := context.get("thread_id")):
        return str(thread_id)
    config = runtime.config or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _check_authorization(runtime: ToolRuntime) -> None:
    if _AUTHORIZATION_CHECKED.get():
        return
    context = runtime.context if isinstance(runtime.context, dict) else {}
    authorize_sandbox_execution(context=context, app_config=safe_app_config())
    _AUTHORIZATION_CHECKED.set(True)


def _rollback_failed_acquire(provider: Any, sandbox_id: str) -> None:
    provider.release(sandbox_id)


def _remember(runtime: ToolRuntime, sandbox_id: str) -> None:
    if runtime.state is not None:
        runtime.state["sandbox"] = {"sandbox_id": sandbox_id}
    if isinstance(runtime.context, dict):
        runtime.context["sandbox_id"] = sandbox_id


def ensure_sandbox_initialized(runtime: ToolRuntime | None = None) -> Sandbox:
    if runtime is None or runtime.state is None:
        raise SandboxRuntimeError("Tool runtime not available")

    _check_authorization(runtime)

    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state and (sandbox_id := sandbox_state.get("sandbox_id")):
        sandbox = get_sandbox_provider().get(sandbox_id)
        if sandbox is not None:
            if isinstance(runtime.context, dict):
                runtime.context["sandbox_id"] = sandbox_id
            return sandbox

    thread_id = _resolve_runtime_thread_id(runtime)
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = provider.acquire(thread_id, user_id=resolve_runtime_user_id(runtime))
    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        _rollback_failed_acquire(provider, sandbox_id)
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    _remember(runtime, sandbox_id)
    return sandbox


async def ensure_sandbox_initialized_async(runtime: ToolRuntime | None = None) -> Sandbox:
    """Mirror of the sync path. The only behavioral difference is ``acquire_async``."""
    if runtime is None or runtime.state is None:
        raise SandboxRuntimeError("Tool runtime not available")

    _check_authorization(runtime)

    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state and (sandbox_id := sandbox_state.get("sandbox_id")):
        sandbox = get_sandbox_provider().get(sandbox_id)
        if sandbox is not None:
            if isinstance(runtime.context, dict):
                runtime.context["sandbox_id"] = sandbox_id
            return sandbox

    thread_id = _resolve_runtime_thread_id(runtime)
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = await provider.acquire_async(thread_id, user_id=resolve_runtime_user_id(runtime))
    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        _rollback_failed_acquire(provider, sandbox_id)
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    _remember(runtime, sandbox_id)
    return sandbox


async def run_sync_tool_after_async_init(func, runtime: ToolRuntime, *args: Any) -> Any:
    """Async graphs initialize the sandbox first, then run the sync tool off-loop."""
    await ensure_sandbox_initialized_async(runtime)
    return await asyncio.to_thread(func, runtime, *args)
