"""两个内置子代理（M8 §5.2）。**字典顺序 = 发现列表顺序**。

内置是 dataclass 模板：注册表拿它当 base，再让 ``subagents.custom_agents``（同名覆盖）
与 ``subagents.agents.<name>``（超时 / 轮次 / 模型 / token_budget）覆盖（§5.1）。

模板**永远不要就地改**：``_apply_overrides`` 用 ``dataclasses.replace`` 复制，
两条列表字段也会重新建，谁拿到谁能随便改。
"""

from agentkit.subagents.builtins.bash_agent import BASH_SUBAGENT
from agentkit.subagents.builtins.general_purpose import GENERAL_PURPOSE_SUBAGENT
from agentkit.subagents.config import SubagentConfig

BUILTIN_SUBAGENTS: dict[str, SubagentConfig] = {
    GENERAL_PURPOSE_SUBAGENT.name: GENERAL_PURPOSE_SUBAGENT,
    BASH_SUBAGENT.name: BASH_SUBAGENT,
}

__all__ = ["BUILTIN_SUBAGENTS"]
