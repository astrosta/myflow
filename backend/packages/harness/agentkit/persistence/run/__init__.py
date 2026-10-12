"""run 行的仓储：ABC 在 base，内存实现在 memory，SQL 实现在 sql，ORM 行在 model。

和 `persistence/thread_meta/` 一个形状——M4 定下的约定是"仓储住在 persistence，
runtime 只拿它当接口用"。`RunManager` 只依赖 `base.RunStore`。
"""

from agentkit.persistence.run.base import RunStore
from agentkit.persistence.run.memory import MemoryRunStore
from agentkit.persistence.run.model import RunRow
from agentkit.persistence.run.sql import SqlRunStore

__all__ = ["MemoryRunStore", "RunRow", "RunStore", "SqlRunStore", "make_run_store"]


def make_run_store(session_factory) -> RunStore:
    """None selects the in-process store. Callers keep the RunStore ABC."""
    if session_factory is None:
        return MemoryRunStore()
    return SqlRunStore(session_factory)
