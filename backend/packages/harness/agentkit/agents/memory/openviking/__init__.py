"""OpenViking-backed memory adapter.

The package is selected by `memory.openviking.enabled`; the legacy canonical
module remains the fallback when the switch is off. The public surface does not
import the OpenViking SDK at module import time, so core behavior is testable
with fakes.
"""

from agentkit.agents.memory.openviking.config import (
    DEFAULT_MEMORY_TYPES,
    DEFAULT_SESSION_MEMORY_TYPES,
    OpenVikingMemoryConfig,
    SessionSkillsConfig,
    idle_only_auto_commit_policy,
)
from agentkit.agents.memory.openviking.facade import MemoryFacade
from agentkit.agents.memory.openviking.runtime import (
    OpenVikingMemoryRuntime,
    configure_openviking_memory,
    get_openviking_memory_runtime,
)
from agentkit.agents.memory.openviking.contracts import (
    CapturedMessage,
    CommitResult,
    MemoryHit,
    MemoryStatus,
    ProfileView,
    SessionRef,
    SignalContext,
    SignalKind,
    ToolEvidence,
    TurnDecision,
)
from agentkit.agents.memory.openviking.signals import CommitSignalDetector

__all__ = [
    "CapturedMessage",
    "CommitResult",
    "CommitSignalDetector",
    "DEFAULT_MEMORY_TYPES",
    "DEFAULT_SESSION_MEMORY_TYPES",
    "MemoryFacade",
    "MemoryHit",
    "MemoryStatus",
    "OpenVikingMemoryConfig",
    "OpenVikingMemoryRuntime",
    "ProfileView",
    "SessionRef",
    "SessionSkillsConfig",
    "SignalContext",
    "SignalKind",
    "ToolEvidence",
    "TurnDecision",
    "configure_openviking_memory",
    "get_openviking_memory_runtime",
    "idle_only_auto_commit_policy",
]
