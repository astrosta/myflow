from backend.app.gateway.auth.store.base import UserStore
from backend.app.gateway.auth.store.memory import MemoryUserStore
from backend.app.gateway.auth.store.sql import SqlUserStore

__all__ = ["MemoryUserStore", "SqlUserStore", "UserStore", "make_user_store"]


def make_user_store(session_factory) -> UserStore:
    """调用方永远拿到 ABC。None 选中进程内实现（database.backend=memory）。"""
    if session_factory is None:
        return MemoryUserStore()
    return SqlUserStore(session_factory)
