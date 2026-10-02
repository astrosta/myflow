from agentkit.runtime.runs.store.base import RunStore
from agentkit.runtime.runs.store.memory import MemoryRunStore

__all__ = ["MemoryRunStore", "RunStore", "make_run_store"]


def make_run_store(session_factory) -> RunStore:
    """None selects the in-process store. Callers keep the RunStore ABC."""
    if session_factory is None:
        return MemoryRunStore()
    from agentkit.persistence.run.sql import SqlRunStore

    return SqlRunStore(session_factory)
