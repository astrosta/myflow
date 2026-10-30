"""Compatibility re-export for the OpenViking memory config.

The real definitions live in agentkit.config.openviking_memory to avoid a
package-initialization cycle between MemoryConfig and the memory package.
"""

from agentkit.config.openviking_memory import (
    DEFAULT_MEMORY_TYPES,
    CaptureConfig,
    CaptureStreamConfig,
    OpenVikingMemoryConfig,
    PersonalizationConfig,
    ProfileSuggesterConfig,
    RetentionConfig,
    SessionPolicyConfig,
    idle_only_auto_commit_policy,
)

__all__ = [
    "CaptureConfig",
    "CaptureStreamConfig",
    "DEFAULT_MEMORY_TYPES",
    "OpenVikingMemoryConfig",
    "PersonalizationConfig",
    "ProfileSuggesterConfig",
    "RetentionConfig",
    "SessionPolicyConfig",
    "idle_only_auto_commit_policy",
]
