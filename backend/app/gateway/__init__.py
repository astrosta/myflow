__all__ = [
    "AssistantNotFound",
    "CallerKind",
    "CheckpointNotFound",
    "ClientDisconnected",
    "IdempotencyKeyReused",
    "ModelNotAllowed",
    "RunCaller",
    "RunCancelRequest",
    "RunCreateRequest",
    "RunLaunch",
    "RunNotCancellable",
    "RunNotFound",
    "RunNotLocal",
    "RunPermissionDenied",
    "RunRequestInvalid",
    "RunServices",
    "StartRunError",
    "ThreadBusy",
    "ThreadNotFound",
    "caller_from_request",
    "get_run_services",
    "sse_response",
    "start_run",
    "wait_for_run_completion",
]

_EXPORTS = {
    "RunServices": "backend.app.gateway.deps",
    "caller_from_request": "backend.app.gateway.deps",
    "get_run_services": "backend.app.gateway.deps",
    "CallerKind": "backend.app.gateway.run_models",
    "RunCaller": "backend.app.gateway.run_models",
    "RunCancelRequest": "backend.app.gateway.run_models",
    "RunCreateRequest": "backend.app.gateway.run_models",
    "RunLaunch": "backend.app.gateway.run_models",
    "AssistantNotFound": "backend.app.gateway.services",
    "CheckpointNotFound": "backend.app.gateway.services",
    "ClientDisconnected": "backend.app.gateway.services",
    "IdempotencyKeyReused": "backend.app.gateway.services",
    "ModelNotAllowed": "backend.app.gateway.services",
    "RunNotCancellable": "backend.app.gateway.services",
    "RunNotFound": "backend.app.gateway.services",
    "RunNotLocal": "backend.app.gateway.services",
    "RunPermissionDenied": "backend.app.gateway.services",
    "RunRequestInvalid": "backend.app.gateway.services",
    "StartRunError": "backend.app.gateway.services",
    "ThreadBusy": "backend.app.gateway.services",
    "ThreadNotFound": "backend.app.gateway.services",
    "sse_response": "backend.app.gateway.services",
    "start_run": "backend.app.gateway.services",
    "wait_for_run_completion": "backend.app.gateway.services",
}


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
