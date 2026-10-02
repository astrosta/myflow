from agentkit.utils.ids import make_safe_user_id, new_run_id, validate_thread_id, validate_user_id

__all__ = [
    "aemit_custom_event",
    "emit_custom_event",
    "make_safe_user_id",
    "new_run_id",
    "validate_thread_id",
    "validate_user_id",
]

_EVENT_EXPORTS = {"aemit_custom_event", "emit_custom_event"}


def __getattr__(name: str):
    if name not in _EVENT_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module("agentkit.utils.custom_events"), name)
    globals()[name] = value
    return value
