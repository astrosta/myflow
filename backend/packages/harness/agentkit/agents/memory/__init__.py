"""Long-term memory: backend, services, manager, middleware, and tools."""

from agentkit.agents.memory.backend import MemoryBackend
from agentkit.agents.memory.manager import (
    MemoryManager,
    configure_memory_store,
    get_memory_manager,
    reset_memory_manager,
)
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.base import MemoryTypeService
from agentkit.agents.memory.services.profile import ProfileService
from agentkit.agents.memory.services.semantic import SemanticMemoryService
from agentkit.agents.memory.types import (
    MemoryConflict,
    MemoryConflictError,
    MemoryCorruptionError,
    MemoryExport,
    MemoryHit,
    MemoryInstruction,
    MemoryKind,
    MemoryKindNotSupported,
    MemoryKindSpec,
    MemoryManagerError,
    MemoryNotFoundError,
    MemoryProposal,
    MemoryQuery,
    MemoryReadError,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    MemoryStats,
    MemoryWriteError,
    ProfilePatch,
    ProfileSnapshot,
    SemanticPayload,
    UserProfile,
)

__all__ = [
    "MemoryBackend",
    "MemoryConflict",
    "MemoryConflictError",
    "MemoryCorruptionError",
    "MemoryExport",
    "MemoryHit",
    "MemoryInstruction",
    "MemoryKind",
    "MemoryKindNotSupported",
    "MemoryKindSpec",
    "MemoryManager",
    "MemoryManagerError",
    "MemoryNotFoundError",
    "MemoryProposal",
    "MemoryQuery",
    "MemoryReadError",
    "MemoryRecord",
    "MemoryResolver",
    "MemoryScope",
    "MemorySearchPage",
    "MemoryStats",
    "MemoryTypeService",
    "MemoryWriteError",
    "ProfilePatch",
    "ProfileService",
    "ProfileSnapshot",
    "SemanticMemoryService",
    "SemanticPayload",
    "UserProfile",
    "configure_memory_store",
    "get_memory_manager",
    "reset_memory_manager",
]
