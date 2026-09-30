from agentkit.config import AppConfig, get_app_config, set_app_config
from agentkit.trace_context import ensure_trace_id, generate_trace_id
from agentkit.utils import make_safe_user_id, validate_thread_id, validate_user_id

__all__ = [
    "AgentClient",
    "AppConfig",
    "StreamEvent",
    "ensure_trace_id",
    "generate_trace_id",
    "get_app_config",
    "main",
    "make_safe_user_id",
    "set_app_config",
    "translate_stream",
    "validate_thread_id",
    "validate_user_id",
]

_LAZY_EXPORTS = {
    "AgentClient": ("agentkit.client", "AgentClient"),
    "StreamEvent": ("agentkit.client", "StreamEvent"),
    "main": ("agentkit.cli", "main"),
    "translate_stream": ("agentkit.client", "translate_stream"),
}


def __getattr__(name: str):
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attr = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value
