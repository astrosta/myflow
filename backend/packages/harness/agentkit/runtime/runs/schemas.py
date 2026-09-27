"""
RunStatus、ThreadOperationKind、CancelOutcome
"""
from enum import StrEnum

class RunStatus(StrEnum):
    pending = "pending"
    running = "running"
    success = "success"
    interrupted = "interrupted"
    error = "error"
    timeout = "timeout"

class ThreadOperationKind(StrEnum):
    run = "run"
    checkpoint_write = "checkpoint_write"
    artifact_write = "artifact_write"
    branch = "branch"
    delete = "delete"

class DisconnectMode(StrEnum):
    """Behaviour when the SSE consumer disconnects."""
    cancel = "cancel"
    continue_ = "continue"

class RunStartOutcome(StrEnum):
    started = "started"
    cancelled = "cancelled"


class RunStartupError(RuntimeError):
    """store 没法确认启动结果。worker 按 error 收尾。"""

class InvalidRunTransition(RuntimeError):
    """活跃状态下的非法迁移：调用顺序错了，是 bug。"""

class CancelOutcome(StrEnum):
    cancelled = "cancelled"
    not_active_locally = "not_active_locally"
    not_cancellable = "not_cancellable"
    unknown = "unknown"
    requested = "requested"          # M9
    taken_over = "taken_over"        # M9