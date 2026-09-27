from contextvars import ContextVar
from typing import Final
import uuid

TRACE_METADATA_KEY = "myflow-trace_id"
_current_trace_id: Final[ContextVar[str | None]] = ContextVar("myflow_current_trace_id", default=None)

def generate_trace_id() -> str:
    return uuid.uuid4().hex

def ensure_trace_id() -> str:
    trace_id = _current_trace_id.get()
    if trace_id is None:
        trace_id = generate_trace_id()
        _current_trace_id.set(trace_id)
    return trace_id


