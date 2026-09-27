"""Sandbox error family. The tool layer translates these into model-visible strings."""


class SandboxError(Exception):
    """Base class for failures the tool layer turns into ``Error: ...`` strings."""


class SandboxRuntimeError(SandboxError):
    """Tool runtime, thread id, or sandbox lifecycle is unavailable."""


class SandboxNotFoundError(SandboxError):
    """``acquire`` succeeded but the instance could not be looked up."""

    def __init__(self, message: str, *, sandbox_id: str | None = None) -> None:
        super().__init__(message)
        self.sandbox_id = sandbox_id


class SandboxTimeoutError(SandboxError):
    """A command exceeded its wall-clock timeout and its process tree was killed."""
