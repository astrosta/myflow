def make_agent(config):
    """具体的薄函数，不是 lazy 属性 —— LangGraph Server 从模块字典里直接取。"""
    from agentkit.agents.leader_agent.agent import make_agent as _impl
    return _impl(config)
