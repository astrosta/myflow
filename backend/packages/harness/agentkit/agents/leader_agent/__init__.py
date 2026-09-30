from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.agents.leader_agent.runtime_keys import (
    ASSEMBLY_INPUT_KEYS,
    CHECKPOINT_COORDINATE_KEYS,
    CLIENT_ASSEMBLY_KEY_TYPES,
    CLIENT_ASSEMBLY_KEYS,
    CLIENT_IDENTITY_KEYS,
    CONTEXT_ONLY_KEYS,
    SERVER_OWNED_KEYS,
)

__all__ = [
    "ASSEMBLY_INPUT_KEYS",
    "AgentAssembly",
    "AgentCacheKey",
    "CHECKPOINT_COORDINATE_KEYS",
    "CLIENT_ASSEMBLY_KEY_TYPES",
    "CLIENT_ASSEMBLY_KEYS",
    "CLIENT_IDENTITY_KEYS",
    "CONTEXT_ONLY_KEYS",
    "SERVER_OWNED_KEYS",
    "agent_cache_key",
    "apply_prompt_template",
    "apply_tool_filter",
    "assemble_agent",
    "build_middlewares",
    "make_agent",
    "normalize_client_configurable",
    "unwrap_agent_graph",
]

_AGENT_EXPORTS = {
    "AgentAssembly",
    "AgentCacheKey",
    "agent_cache_key",
    "apply_tool_filter",
    "assemble_agent",
    "build_middlewares",
    "make_agent",
    "normalize_client_configurable",
    "unwrap_agent_graph",
}


def __getattr__(name: str):
    if name not in _AGENT_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module("agentkit.agents.leader_agent.agent"), name)
    globals()[name] = value
    return value
