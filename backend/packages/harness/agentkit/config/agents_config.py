from pydantic import BaseModel, ConfigDict, Field


class AgentsConfig(BaseModel):
    """M1 agents section. Everything here hot-reloads."""

    model_config = ConfigDict(extra="forbid")

    default_model: str | None = Field(
        default=None,
        description="Per-run default model name; must be one of models[].name",
    )
    allowed_subagents: list[str] | None = Field(
        default=None,
        description=(
            "M8 §16 的调用方策略：None = 全部允许，[] = 硬拒绝（提示词里不出现委派段、"
            "task 工具也不装），列表 = 白名单。run 开始时快照一次，运行中不再重读。"
        ),
    )
    subagent_max_concurrent: int | None = Field(
        default=None,
        ge=1,
        description=(
            "这个 lead agent 允许的单轮并行委派数。最终值取 "
            "min(这里, subagent_runtime.max_running, 64)——提示词、Limit 中间件、"
            "控制器必须读同一个数（§4.2）。"
        ),
    )
