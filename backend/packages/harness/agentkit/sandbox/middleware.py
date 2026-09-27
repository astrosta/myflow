"""Acquire on the way in, release on the way out. Tools never release the sandbox."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from agentkit.sandbox.runtime import reset_authorization_checked, resolve_runtime_user_id
from agentkit.sandbox.sandbox_provider import get_sandbox_provider


class SandboxMiddleware(AgentMiddleware):
    """Warm the sandbox before the agent and release it after the graph finishes.

    A single tool must not release: siblings in the same step still need the sandbox.
    ``ensure_sandbox_initialized`` remains the lazy fallback for runs that skipped warmup.
    """

    def before_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        reset_authorization_checked()
        return _acquire(state, runtime, async_acquire=False)

    async def abefore_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        reset_authorization_checked()
        return await _acquire_async(state, runtime)

    def after_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        _release(state, runtime)
        return None

    async def aafter_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        _release(state, runtime)
        return None


def _thread_id(state: Any, runtime: Runtime) -> str | None:
    context = getattr(runtime, "context", None) or {}
    if isinstance(context, dict) and context.get("thread_id"):
        return str(context["thread_id"])
    configurable = {}
    config = getattr(runtime, "config", None)
    if isinstance(config, dict):
        configurable = config.get("configurable", {}) or {}
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _user_id(runtime: Runtime) -> str:
    context = getattr(runtime, "context", None)
    if isinstance(context, dict) and context.get("user_id"):
        return str(context["user_id"])
    try:
        return resolve_runtime_user_id(runtime)  # type: ignore[arg-type]
    except Exception:
        return "default"


def _existing_id(state: Any) -> str | None:
    sandbox_state = state.get("sandbox") if isinstance(state, dict) else None
    if isinstance(sandbox_state, dict):
        return sandbox_state.get("sandbox_id")
    return None


def _acquire(state: Any, runtime: Runtime, *, async_acquire: bool) -> dict[str, Any] | None:
    del async_acquire
    existing = _existing_id(state)
    if existing and get_sandbox_provider().get(existing) is not None:
        _stamp(runtime, existing)
        return None
    thread_id = _thread_id(state, runtime)
    if thread_id is None:
        return None
    sandbox_id = get_sandbox_provider().acquire(thread_id, user_id=_user_id(runtime))
    _stamp(runtime, sandbox_id)
    return {"sandbox": {"sandbox_id": sandbox_id}}


async def _acquire_async(state: Any, runtime: Runtime) -> dict[str, Any] | None:
    existing = _existing_id(state)
    if existing and get_sandbox_provider().get(existing) is not None:
        _stamp(runtime, existing)
        return None
    thread_id = _thread_id(state, runtime)
    if thread_id is None:
        return None
    sandbox_id = await get_sandbox_provider().acquire_async(thread_id, user_id=_user_id(runtime))
    _stamp(runtime, sandbox_id)
    return {"sandbox": {"sandbox_id": sandbox_id}}


def _stamp(runtime: Runtime, sandbox_id: str) -> None:
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        context["sandbox_id"] = sandbox_id


def _release(state: Any, runtime: Runtime) -> None:
    context = getattr(runtime, "context", None) or {}
    sandbox_id = context.get("sandbox_id") if isinstance(context, dict) else None
    if not sandbox_id:
        sandbox_id = _existing_id(state)
    if sandbox_id:
        get_sandbox_provider().release(sandbox_id)
