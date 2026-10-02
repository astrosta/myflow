from agentkit.persistence.thread_meta.base import ThreadMetaStore
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.persistence.thread_meta.sql import SqlThreadMetaStore

__all__ = [
    "MemoryThreadMetaStore",
    "SqlThreadMetaStore",
    "ThreadMetaStore",
    "make_thread_meta_store",
]


def make_thread_meta_store(session_factory) -> ThreadMetaStore:
    """Callers always receive the ABC. None selects the in-process store."""
    if session_factory is None:
        return MemoryThreadMetaStore()
    return SqlThreadMetaStore(session_factory)
