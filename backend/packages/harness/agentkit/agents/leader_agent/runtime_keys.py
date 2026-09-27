# 装配键, 来源 body.context, body.config.context, body.config.configurable
CLIENT_ASSEMBLY_KEYS = frozenset([
    "model_name",
    "thinking_enabled",
    "reasoning_effort",
    "is_plan_mode",
    "subagent_enabled",
    "agent_name",
])
CLIENT_ASSEMBLY_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "model_name": (str, type(None)),
    "thinking_enabled": bool,
    "reasoning_effort": (str, type(None)),
    "is_plan_mode": bool,
    "subagent_enabled": bool,
    "agent_name": (str, type(None)),
}

# checkpoint坐标
CHECKPOINT_COORDINATE_KEYS = frozenset([
    "thread_id", 
    "checkpoint_ns",
    "checkpoint_id", 
    "checkpoint_map",
])

# 服务端拥有
SERVER_OWNED_KEYS = frozenset({
    "non_interactive",
    "user_id",
    "run_id",
    "trace_id",
    "is_internal",
})

# 客户端身份
CLIENT_IDENTITY_KEYS = frozenset({
    "user_id",
    "owner_id",
    "auth_user",
    "auth_user_id",
})

# 内部身份
CONTEXT_ONLY_KEYS = frozenset({"channel_user_id"})

# 内部开关
INTERNAL_SWITCHES = frozenset({"non_interactive"})

# 装配输入
ASSEMBLY_INPUT_KEYS = CLIENT_ASSEMBLY_KEYS | INTERNAL_SWITCHES