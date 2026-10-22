"""PII protection configuration (M10 memory privacy boundary)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PIIConfig(BaseModel):
    """Text PII redaction is enabled by default for user input.

    The run-scoped vault is never persisted. Every tool call receives restored
    original arguments at the trusted tool boundary; unresolved handles fail closed
    in the tool error path.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    redact_ai_output: bool = True


__all__ = ["PIIConfig"]
