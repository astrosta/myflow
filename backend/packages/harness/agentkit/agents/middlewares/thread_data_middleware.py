"""Publish the per-thread workspace, uploads, and outputs paths."""

from __future__ import annotations

from typing import NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from agentkit.agents.thread_state import ThreadDataState
from agentkit.config.paths import Paths, get_paths
from agentkit.sandbox.runtime import runtime_user_bucket


class ThreadDataMiddlewareState(AgentState):
    thread_data: NotRequired[ThreadDataState | None]


def resolve_thread_id(runtime: Runtime) -> str:
    """Context first, then LangGraph configurable. Missing both is an error."""
    context = getattr(runtime, "context", None) or {}
    thread_id = context.get("thread_id") if isinstance(context, dict) else None
    if not thread_id:
        from langgraph.config import get_config

        try:
            config = get_config()
        except RuntimeError:
            config = {}
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
        thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    if not thread_id:
        raise ValueError("Thread ID is required in runtime context or config.configurable")
    return str(thread_id)


class ThreadDataMiddleware(AgentMiddleware[ThreadDataMiddlewareState]):
    """lazy_init only computes paths. Eager init calls ensure_thread_dirs."""

    state_schema = ThreadDataMiddlewareState

    def __init__(self, base_dir: str | None = None, lazy_init: bool = True) -> None:
        super().__init__()
        self._paths = Paths(base_dir) if base_dir is not None else get_paths()
        self._lazy_init = lazy_init

    def _thread_paths(self, thread_id: str, user_id: str) -> dict[str, str]:
        return {
            "workspace_path": str(self._paths.sandbox_work_dir(thread_id, user_id=user_id).resolve()),
            "uploads_path": str(self._paths.sandbox_uploads_dir(thread_id, user_id=user_id).resolve()),
            "outputs_path": str(self._paths.sandbox_outputs_dir(thread_id, user_id=user_id).resolve()),
        }

    def before_agent(self, state: ThreadDataMiddlewareState, runtime: Runtime) -> dict:
        del state
        thread_id = resolve_thread_id(runtime)
        user_id = runtime_user_bucket(runtime)
        if not self._lazy_init:
            self._paths.ensure_thread_dirs(thread_id, user_id=user_id)
        return {"thread_data": self._thread_paths(thread_id, user_id)}

    async def abefore_agent(self, state: ThreadDataMiddlewareState, runtime: Runtime) -> dict:
        return self.before_agent(state, runtime)
