def make_agent(config):
    """具体的薄函数，不是 lazy 属性 —— LangGraph Server 从模块字典里直接取。"""
    from agentkit.agents.leader_agent import make_agent as _impl

    return _impl(config)


__all__ = ["ThreadState", "make_agent"]


def __getattr__(name: str):
    if name != "ThreadState":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from agentkit.agents.thread_state import ThreadState

    globals()["ThreadState"] = ThreadState
    return ThreadState
