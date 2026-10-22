"""``subagents:`` 段落（M8 §4.1 / §4.2）——全部字段**热重载**。

两个独立上限**不要合成一个**（§4.2）：

| 上限 | 配置 | 约束的是 | 默认 | schema |
|---|---|---|---|---|
| 进程并发 | ``subagent_runtime.max_running`` | **同时**在跑几个 | 3 | 1–64，启动冻结 |
| 单轮并行 | 解析后的 ``max_concurrent`` | 同一条 AIMessage 里保留几条 task | = ``max_running`` | ``effective_subagent_concurrency`` |
| 单 run 总量 | ``subagents.max_total_per_run`` | 一次父 run **累计**派了几个 | 6 | 1–50，可热重载 |

只有并发上限是不够的：一个长 lead run 可以在每个规划检查点都发起一批合法大小的
委派，跑一整天。总量上限堵的就是这个（按当前 run 的委派台账计，历史 run 不消耗）。

注册表吐出的是 ``agentkit.subagents.config.SubagentConfig``（dataclass），
**不是**这里的 pydantic 模型（§4.3）：校验框架不进执行器。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentkit.subagents.config import (
    BUILTIN_SUBAGENT_TIMEOUT_SECONDS,
    DEFAULT_SUBAGENT_DISALLOWED_TOOLS,
    DEFAULT_SUBAGENT_MAX_TURNS,
    DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    INHERIT_MODEL,
)

#: ``subagents.timeout_seconds`` 默认值 = 内置子代理的墙钟（§4.1 / §5.1）。
#: 注意与 dataclass 的 900 区分：900 是 custom_agents 的自带默认值。
DEFAULT_SUBAGENTS_TIMEOUT_SECONDS = BUILTIN_SUBAGENT_TIMEOUT_SECONDS
DEFAULT_SUBAGENTS_MAX_TOTAL_PER_RUN = 6
MIN_SUBAGENTS_MAX_TOTAL_PER_RUN = 1
MAX_SUBAGENTS_MAX_TOTAL_PER_RUN = 50

#: 单轮并行的硬上限；``subagent_runtime.max_running`` 也在同一区间内校验。
SUBAGENT_CONCURRENCY_CEILING = 64

#: token 预算默认跟 summarization.enabled 耦合（§12.5）：
#: 开压缩留出摘要余量，关压缩可以吃满模型窗口。用户显式写了 max_tokens 永远优先。
DEFAULT_TOKEN_BUDGET_WITH_SUMMARIZATION = 1_000_000
DEFAULT_TOKEN_BUDGET_WITHOUT_SUMMARIZATION = 2_000_000
DEFAULT_TOKEN_BUDGET_WARN_THRESHOLD = 0.7


def drop_null_config_entries(data: Any) -> Any:
    """present-but-null 当作缺失（M0 §5.7）。

    用户把一整段注释掉时 PyYAML 会解析成 ``None``；当成"缺失"让默认值生效，
    比抛 ``Input should be a valid dictionary`` 有用得多。
    """
    if isinstance(data, dict):
        return {key: value for key, value in data.items() if value is not None}
    return data


class SubagentConfigModel(BaseModel):
    """``subagents.*`` / ``subagent_runtime`` 所有 YAML 模型的基类：extra=forbid + null 当缺失。"""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_null_config_sections(cls, data: Any) -> Any:
        return drop_null_config_entries(data)


class SubagentTokenBudgetConfig(SubagentConfigModel):
    """子代理的 token 预算（软告警 + 硬上限）。硬停**不抛异常**，走 ``stop_reason``（§13）。"""

    enabled: bool = Field(default=True, description="关闭后子代理没有 token 轴护栏，只剩轮次与循环检测。")
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "显式值永远优先；留空（null）时跟 summarization.enabled 耦合："
            f"开压缩 {DEFAULT_TOKEN_BUDGET_WITH_SUMMARIZATION:,}，关压缩 {DEFAULT_TOKEN_BUDGET_WITHOUT_SUMMARIZATION:,}。"
        ),
    )
    warn_threshold: float = Field(
        default=DEFAULT_TOKEN_BUDGET_WARN_THRESHOLD,
        gt=0,
        le=1,
        description="占 max_tokens 的比例，超过只发提醒，不硬停。",
    )


class CustomSubagentConfig(SubagentConfigModel):
    """``subagents.custom_agents.<name>``：用户定义的新类型（§4.1）。"""

    description: str = Field(
        ...,
        min_length=1,
        description="给 lead 看的一句话：这个帮手擅长什么、什么时候该派它。会进提示词的发现列表。",
    )
    system_prompt: str | None = Field(
        default=None,
        description="子代理自己的角色提示。null = 只用框架的静态角色提示与日期。",
    )
    tools: list[str] | None = Field(
        default=None,
        description="null = 继承 lead **dispatch 那一刻**持有的全部工具（再扣 disallowed_tools）。",
    )
    disallowed_tools: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SUBAGENT_DISALLOWED_TOOLS),
        description=(
            "永不暴露给这个子代理的工具名。默认禁 task（M8 不做嵌套委派）；"
            "写 [] 才是「什么都不禁」。"
        ),
    )
    skills: list[str] | None = Field(
        default=None,
        description="null = 全部启用技能；[] = 禁用技能（不是「继承 lead 的技能集」）。",
    )
    model: str = Field(
        default=INHERIT_MODEL,
        description=f"模型名，或 {INHERIT_MODEL!r}（跟 lead 同名，凭据仍来自服务端配置）。",
    )
    max_turns: int = Field(default=DEFAULT_SUBAGENT_MAX_TURNS, ge=1, description="轮次轴：recursion_limit。")
    timeout_seconds: int = Field(
        default=DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        ge=1,
        description="墙钟上限（秒）。到点由隔离循环 wait_for 收尾，对外 status=timed_out。",
    )


class SubagentOverrideConfig(SubagentConfigModel):
    """``subagents.agents.<name>``：按名字覆盖 builtin / custom（§5.1）。

    只覆盖这几个维度，**不能**改 description / system_prompt / tools——
    那等于重新定义一个类型，应该写进 ``custom_agents``。
    """

    timeout_seconds: int | None = Field(default=None, ge=1, description="null = 不改。")
    max_turns: int | None = Field(default=None, ge=1, description="null = 不改。")
    model: str | None = Field(
        default=None,
        description=f"模型名或 {INHERIT_MODEL!r}；null = 不改。",
    )
    token_budget: SubagentTokenBudgetConfig | None = Field(
        default=None,
        description="这个类型的 token 预算覆盖；null = 用 subagents.token_budget。",
    )


class SubagentsConfig(SubagentConfigModel):
    """``subagents:`` 整段。全部热重载——进程级容量在 ``subagent_runtime``（startup-only）。"""

    timeout_seconds: int = Field(
        default=DEFAULT_SUBAGENTS_TIMEOUT_SECONDS,
        ge=1,
        description="内置子代理默认墙钟上限（秒）；单个类型可用 agents.<name> 覆盖。",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="全局轮次覆盖；null = 用每个类型自己的值（general-purpose=150，bash=60）。",
    )
    max_total_per_run: int = Field(
        default=DEFAULT_SUBAGENTS_MAX_TOTAL_PER_RUN,
        ge=MIN_SUBAGENTS_MAX_TOTAL_PER_RUN,
        le=MAX_SUBAGENTS_MAX_TOTAL_PER_RUN,
        description="一次父 run 累计委派上限。按当前 run 的台账计，同 thread 历史 run 不消耗新预算。",
    )
    token_budget: SubagentTokenBudgetConfig = Field(default_factory=SubagentTokenBudgetConfig)
    agents: dict[str, SubagentOverrideConfig] = Field(
        default_factory=dict,
        description="按名字覆盖 builtin / custom 的超时、轮次、模型、token 预算。",
    )
    custom_agents: dict[str, CustomSubagentConfig] = Field(
        default_factory=dict,
        description="用户定义的新类型；与内置同名时**覆盖**内置并打 warning（§5.1）。",
    )

    @field_validator("agents", "custom_agents", mode="before")
    @classmethod
    def _drop_null_agent_entries(cls, value: Any) -> Any:
        """``general-purpose:`` 下面整段被注释掉时值是 None——当成"没写这个类型"。"""
        return drop_null_config_entries(value)


def effective_subagent_concurrency(requested: int | None, *, max_running: int) -> int:
    """解析单轮并行数（§4.2）。

    提示词、``SubagentLimitMiddleware``、进程控制器必须读**同一个数**：提示词说
    "你可以并发 8 个"而控制器只有 3 个槽，模型会一直撞墙。所以三者都调这一个函数，
    而不是各自读 YAML（中间件构造函数尤其不要再读一遍配置）。

    取 ``min(请求值, 启动时冻结的 max_running, 64)``；``requested=None`` 表示"用满
    容量"。函数对非正数/超界输入是**防御性**的：不抛异常，夹到合法区间。
    """
    upper = min(SUBAGENT_CONCURRENCY_CEILING, max(1, max_running))
    value = max_running if requested is None else int(requested)
    return max(1, min(upper, value))


def resolve_token_budget_max_tokens(
    token_budget: SubagentTokenBudgetConfig,
    *,
    summarization_enabled: bool,
) -> int:
    """token 预算上限：用户显式值永远优先，否则跟压缩开关耦合（§4.1 / §12.5）。"""
    if token_budget.max_tokens is not None:
        return token_budget.max_tokens
    if summarization_enabled:
        return DEFAULT_TOKEN_BUDGET_WITH_SUMMARIZATION
    return DEFAULT_TOKEN_BUDGET_WITHOUT_SUMMARIZATION
