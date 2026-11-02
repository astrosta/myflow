"""内置子代理。**字典顺序 = 发现列表顺序**。

内置是 dataclass 模板：注册表拿它当 base，再让 ``subagents.custom_agents``（同名覆盖）
与 ``subagents.agents.<name>``（超时 / 轮次 / 模型 / token_budget）覆盖（§5.1）。

模板**永远不要就地改**：``_apply_overrides`` 用 ``dataclasses.replace`` 复制，
两条列表字段也会重新建，谁拿到谁能随便改。

``general-purpose`` 排第一是历史原因；三个检索子代理（2026-11-02）排在 ``bash`` 之前，
让 lead 的发现列表先看到它们。
"""

from agentkit.subagents.builtins.bash_agent import BASH_SUBAGENT
from agentkit.subagents.builtins.general_purpose import GENERAL_PURPOSE_SUBAGENT
from agentkit.subagents.builtins.knowledge_researcher import KNOWLEDGE_RESEARCHER_SUBAGENT
from agentkit.subagents.builtins.memory_researcher import MEMORY_RESEARCHER_SUBAGENT
from agentkit.subagents.builtins.skill_finder import SKILL_FINDER_SUBAGENT
from agentkit.subagents.config import SubagentConfig

BUILTIN_SUBAGENTS: dict[str, SubagentConfig] = {
    GENERAL_PURPOSE_SUBAGENT.name: GENERAL_PURPOSE_SUBAGENT,
    KNOWLEDGE_RESEARCHER_SUBAGENT.name: KNOWLEDGE_RESEARCHER_SUBAGENT,
    MEMORY_RESEARCHER_SUBAGENT.name: MEMORY_RESEARCHER_SUBAGENT,
    SKILL_FINDER_SUBAGENT.name: SKILL_FINDER_SUBAGENT,
    BASH_SUBAGENT.name: BASH_SUBAGENT,
}

__all__ = ["BUILTIN_SUBAGENTS"]
