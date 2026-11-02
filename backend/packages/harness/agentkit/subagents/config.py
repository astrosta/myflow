"""注册表条目：执行器只认这个 dataclass（M8 §4.3）。

和 ``agentkit.config.subagents_config`` 里的 YAML pydantic 模型**刻意分开**：
校验框架不泄漏进执行器，子代理在隔离循环上构造配置时不必过一遍 pydantic。
所以本模块**只依赖 stdlib**，``AppConfig`` 只在类型检查期 import。

内置类型（general-purpose / bash）与 custom_agents 最终都解析成这里的
``SubagentConfig``——注册表负责拼接与覆盖（§5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 运行时不 import：本模块是纯 stdlib 叶子，避免 config ↔ subagents 循环
    from agentkit.config import AppConfig

#: 默认禁掉的工具。M8 不做嵌套委派（§20），内置与自定义一视同仁：
#: 想放开必须显式写 disallowed_tools（``[]`` 或去掉 task 的列表）。
DEFAULT_SUBAGENT_DISALLOWED_TOOLS: tuple[str, ...] = ("task",)

DEFAULT_SUBAGENT_MAX_TURNS = 50
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 900

#: 两个**内置**子代理的墙钟默认值 = ``subagents.timeout_seconds`` 的默认值（§4.1）。
#: 内置不带自己的墙钟：注册表用段落值填它（§5.1）。这里定义一次，配置层引用它，
#: 免得两处 1800 各自漂移。
BUILTIN_SUBAGENT_TIMEOUT_SECONDS = 1800

#: ``model: inherit`` = 跟 lead 同名模型
INHERIT_MODEL = "inherit"

#: 委派工具名。放在这个**叶子模块**里是有原因的：limit 中间件（agents 层）与 ``task`` 工具
#: （tools 层）都要用它，谁都不该为了一个字符串把对方的整条 import 链拖进来（会成环）。
TASK_TOOL_NAME = "task"


@dataclass
class SubagentConfig:
    """一个子代理类型的解析结果（BUILTIN → custom_agents → agents.<name> 之后）。

    字段语义（§4.3）：
      * ``tools=None``              继承 lead **当时**持有的全部工具，再扣 ``disallowed_tools``
      * ``skills=None``             暂未生效（子代理链没有技能中间件）；``[]`` 同样不改变可见技能
      * ``disallowed_tools=None``   不过滤（默认值是 ``["task"]``）
      * ``model="inherit"``         只继承名字，凭据仍来自服务端配置（M7 §6）
      * ``max_turns``               模型调用次数上限；执行器按图的节点换算 ``recursion_limit``
    """

    name: str
    description: str
    #: ``max_concurrent == 1`` 时委派段改用这一版（去掉并行字样）；None = 两种模式同一份。
    sequential_description: str | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = field(
        default_factory=lambda: list(DEFAULT_SUBAGENT_DISALLOWED_TOOLS)
    )
    skills: list[str] | None = None
    model: str = INHERIT_MODEL
    max_turns: int = DEFAULT_SUBAGENT_MAX_TURNS
    timeout_seconds: int = DEFAULT_SUBAGENT_TIMEOUT_SECONDS


def resolve_subagent_model_name(
    config: SubagentConfig,
    parent_model: str | None,
    *,
    app_config: AppConfig,
) -> str:
    """解析子代理实际使用的模型名（§4.3）。

    ``inherit`` 只继承**名字**：走和 lead 相同的 ``create_chat_model(name=...)``，
    请求级密钥不继承（M7 §6）。父模型名缺失（例如嵌入调用没带）时退到
    ``models[0]``——和 M1 "不指定模型用第一个" 的规则一致。
    """
    if config.model != INHERIT_MODEL:
        return config.model
    if parent_model is not None:
        return parent_model
    if not app_config.models:
        raise ValueError("No chat models are configured.")
    return app_config.models[0].name
