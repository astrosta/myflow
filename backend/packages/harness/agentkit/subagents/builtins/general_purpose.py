"""`general-purpose`：唯一一个继承 lead 工具集的类型（M8 §5.2）。

它的价值是"并行 + 上下文隔离"：子代理翻文件、试命令的中间垃圾留在它自己的上下文里，
只把结论交回 lead。

两条禁令写进**系统提示词**，不只是 `disallowed_tools`：模型看不见 `task` 工具，
但仍然会"打算"再派一个帮手、或者向用户提问——那两句在回答里同样会毁掉这一轮。
"""

from __future__ import annotations

from agentkit.subagents.config import (
    BUILTIN_SUBAGENT_TIMEOUT_SECONDS,
    INHERIT_MODEL,
    SubagentConfig,
)
from agentkit.subagents.routing import ROUTING_BENEFITS, ROUTING_COST, ROUTING_VETOES

NAME = "general-purpose"

#: §5.2 的 150 轮。`subagents.max_turns` 非 null 时由注册表覆盖（§5.1）。
MAX_TURNS = 150

#: §5.2：禁止嵌套委派（M8 不做）与向用户提问（子代理没有这一轮对话的用户）。
DISALLOWED_TOOLS = ["task", "ask_clarification"]

DESCRIPTION = (
    "拥有独立上下文的通用子代理，只把最终结论交回 lead。"
    f"适合委派收益明确的独立、自包含子任务（{ROUTING_BENEFITS}）——"
    "例如同时调查三个互不相关的问题，或中间产物会淹没上下文的整理工作。"
    f"不要委派 {ROUTING_VETOES}。委派本身也有成本（{ROUTING_COST}），"
    "一两步就能自己做完的事情直接做更划算。"
)

SYSTEM_PROMPT = """\
你是一个通用子代理。lead 给了你一个自包含任务，你在自己的上下文中执行：
lead 只看得到你的最终回答，看不到你的中间步骤。

硬性规则：
- 你没有 `task` 工具，不能继续委派，也不能再创建其他助手：自己完成任务，
  绝不要说“我会转交给别人”。
- 你不能向用户提问（这里没有 `ask_clarification`）。任务有歧义时，采用最合理的理解，
  完成工作，并在回答中说明你的假设。
- 只做交给你的任务，不要开始无关工作。

回答方式：
- 报告结论，而不是过程：发现、决定、准确的文件路径，以及真正起作用的命令或来源。
- 保持简洁、可直接被 lead 使用：不要逐步骤叙述，也不要把任务原样复述给 lead。
- 你创建或修改的文件会留在与 lead 共享的工作区中，请写明你改动过的路径。
"""

GENERAL_PURPOSE_SUBAGENT = SubagentConfig(
    name=NAME,
    description=DESCRIPTION,
    system_prompt=SYSTEM_PROMPT,
    tools=None,  # None = 继承 lead **dispatch 那一刻**持有的工具，再扣 disallowed_tools
    disallowed_tools=list(DISALLOWED_TOOLS),
    skills=None,  # None = 全部启用技能
    model=INHERIT_MODEL,
    max_turns=MAX_TURNS,
    timeout_seconds=BUILTIN_SUBAGENT_TIMEOUT_SECONDS,  # 段落值 subagents.timeout_seconds 覆盖它
)

__all__ = ["DESCRIPTION", "DISALLOWED_TOOLS", "GENERAL_PURPOSE_SUBAGENT", "MAX_TURNS", "NAME", "SYSTEM_PROMPT"]
