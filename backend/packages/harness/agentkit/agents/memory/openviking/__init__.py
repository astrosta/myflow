"""OpenViking-backed memory adapter.

This package is additive: the legacy canonical memory module remains active
until the P5 cutover. The public surface does not import the OpenViking SDK at
module import time, so core behavior is testable with fakes.
"""

from agentkit.agents.memory.openviking.config import (
    DEFAULT_MEMORY_TYPES,
    OpenVikingMemoryConfig,
    idle_only_auto_commit_policy,
)
from agentkit.agents.memory.openviking.contracts import (
    CapturedMessage,
    CommitResult,
    MemoryHit,
    MemoryStatus,
    ProfileView,
    SessionRef,
    SignalKind,
    ToolEvidence,
    TurnDecision,
)

__all__ = [
    "CapturedMessage",
    "CommitResult",
    "DEFAULT_MEMORY_TYPES",
    "MemoryHit",
    "MemoryStatus",
    "OpenVikingMemoryConfig",
    "ProfileView",
    "SessionRef",
    "SignalKind",
    "ToolEvidence",
    "TurnDecision",
    "idle_only_auto_commit_policy",
]
