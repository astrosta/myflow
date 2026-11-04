"""The only bridge from tools to a sandbox. M9 authorization mounts here."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from langchain.tools import ToolRuntime

from agentkit.authz import (
    DEFAULT_ROLE,
    AuthzDeniedError,
    build_principal_from_context,
    check_authorization,
)
from agentkit.runtime.user_context import (
    DEFAULT_USER_ID,
    resolve_runtime_user_id as resolve_request_user_id,
)
from agentkit.sandbox.exceptions import (
    SandboxAuthorizationError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from agentkit.sandbox.lease import lease_owner_from_context, shield_sandbox_work
from agentkit.sandbox.sandbox import Sandbox
from agentkit.sandbox.sandbox_provider import (
    SandboxProvider,
    get_sandbox_provider,
    reset_sandbox_provider,
)
from agentkit.sandbox.security import safe_app_config

#: 沙箱动作名。M10 的策略按它分支，M9 的 AllowAll 不看。
SANDBOX_EXECUTE_ACTION = "sandbox.execute"

_AUTHORIZATION_CHECKED: ContextVar[bool] = ContextVar("sandbox_authorization_checked", default=False)


def reset_authorization_checked() -> None:
    _AUTHORIZATION_CHECKED.set(False)


def authorize_sandbox_execution(*, context: dict[str, Any], app_config: Any) -> None:
    """沙箱 acquire 与 reuse **两条路径**共用的一次授权检查（M9 §2.10）。

    M9 的 provider 是 AllowAll：接口在，策略全放行。M10 换成工具级 deny 时
    这里一行都不用改。provider 抛异常 = 拒绝（fail-closed），不是放行。
    """
    del app_config
    principal = build_principal_from_context(context, default_role=DEFAULT_ROLE)
    resource = {
        key: value
        for key, value in (("thread_id", context.get("thread_id")), ("run_id", context.get("run_id")))
        if isinstance(value, str)
    }
    try:
        check_authorization(principal, SANDBOX_EXECUTE_ACTION, resource=resource or None)
    except AuthzDeniedError as exc:
        raise SandboxAuthorizationError(str(exc)) from exc


def runtime_user_bucket(runtime: ToolRuntime) -> str:
    """这一轮工具调用属于哪个存储桶（M9 §2.1）。链在 `agentkit.runtime.user_context`。

    工具运行时是"无 HTTP 路径"，所以允许落到 `DEFAULT_USER_ID` 桶：
    `config["context"]["user_id"]`（服务端注入）→ 请求 ContextVar → default。
    客户端塞的 `user_id` 到不了这里（`start_run` 已剥掉，M9 §2.1）。
    """
    context = runtime.context if isinstance(runtime.context, dict) else None
    return resolve_request_user_id(context, allow_default=True) or DEFAULT_USER_ID


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
    sandbox_id = provider.acquire(thread_id, user_id=runtime_user_bucket(runtime))
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
    sandbox_id = await provider.acquire_async(thread_id, user_id=runtime_user_bucket(runtime))
    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        _rollback_failed_acquire(provider, sandbox_id)
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    _remember(runtime, sandbox_id)
    return sandbox


def start_sandbox_provider(*, app_config: Any = None) -> SandboxProvider:
    """Build (or reuse) the process provider and run its startup checks.

    The lifespan calls this once, after the config is loaded and before the first
    request, so an unreachable remote cluster or a bad mount source fails startup
    instead of the first user run. 只做健康/配置检查：启动不创建任何沙箱，第一个
    沙箱由 ``acquire`` 在真正有线程要跑的时候才建。
    """
    if app_config is None:
        provider = get_sandbox_provider()
    else:
        provider = get_sandbox_provider(app_config=app_config)
    provider.startup_check()
    return provider


async def shutdown_sandbox_provider() -> None:
    """Drop the process provider off-loop: ``shutdown()`` kills every live sandbox.

    ``reset_sandbox_provider`` calls ``provider.shutdown()``, which for a remote provider
    pauses/kills containers over the network—the same blocking IO that justifies
    ``release_async``. A synchronous reset here would stall the loop that is still
    draining the run manager, so the shutdown path awaits this instead.
    """
    await asyncio.to_thread(reset_sandbox_provider)


async def run_sync_tool_after_async_init(func, runtime: ToolRuntime, *args: Any) -> Any:
    """Async graphs initialize the sandbox first, then run the sync tool off-loop.

    §11.3：子代理名下这次调用要**登记**在租约台账里再 await。``to_thread`` 取消不掉——
    worker 被砍之后线程还在跑，执行器得先 ``drain_sandbox_work`` 排空它，才能释放租约。
    """
    await ensure_sandbox_initialized_async(runtime)
    owner_id = lease_owner_from_context(runtime.context)
    if owner_id is None:
        return await asyncio.to_thread(func, runtime, *args)
    return await shield_sandbox_work(owner_id, asyncio.to_thread(func, runtime, *args))
