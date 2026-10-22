"""``bash``：沙箱 shell 子代理（M8 §5.2）。

它存在的理由只有一个：**长、吵、只关心结论**的命令活（整套测试、构建、日志排查、
全仓搜索）。中间输出留在它自己的上下文里，lead 只拿到结论。

所以描述里必须写明"常规 git/build/test/deploy 不够成为委派理由"——否则模型会把
每一条命令都包一层子代理，用户看到的是卡片满天飞、总耗时反而更长。

工具是**沙箱工具集的子集**，且按"有则留，无则跳过"求交（§5.2 / §5.3）：宿主 bash
没开时这个类型连发现列表都不出现（注册表负责，§5.2）。
"""

from __future__ import annotations

from agentkit.subagents.config import (
    BUILTIN_SUBAGENT_TIMEOUT_SECONDS,
    INHERIT_MODEL,
    SubagentConfig,
)
from agentkit.subagents.routing import ROUTING_BENEFITS, ROUTING_COST, ROUTING_VETOES

NAME = "bash"

#: §5.2 的 60 轮。``subagents.max_turns`` 非 null 时由注册表覆盖（§5.1）。
MAX_TURNS = 60

#: 沙箱工具子集。缺哪个就少哪个（executor 用 filter_tools 与 lead 的实际工具求交）。
TOOLS = ["bash", "ls", "read_file", "write_file", "str_replace"]

DISALLOWED_TOOLS = ["task", "ask_clarification"]

DESCRIPTION = (
    "拥有独立上下文的沙箱命令子代理，适合执行时间长、输出嘈杂的命令工作："
    "完整测试套件、构建、日志排查、全仓搜索——你不希望这些输出进入自己的上下文，"
    f"只关心最终结论（{ROUTING_BENEFITS}）。常规的 git / 构建 / 测试 / 部署命令"
    f"不足以成为委派理由：自己执行比 {ROUTING_COST} 更便宜。不要委派 {ROUTING_VETOES}。"
)

SYSTEM_PROMPT = """\
你是一个沙箱子代理。lead 给了你一个偏命令行的任务，你在自己的上下文中执行：
lead 只看得到你的最终回答，看不到完整命令输出。

硬性规则：
- 你没有 `task` 工具，也不能向用户提问（这里没有 `ask_clarification`）：
  自己执行任务；如果任务有歧义，采用最合理的理解，并说明你的假设。
- 只做任务范围内的动作。优先使用只读命令，只写入任务要求的文件：
  lead 与你共享这个工作区，可能正在同时使用它。
- 长输出正是你存在的理由：把输出留在自己的上下文里，只返回结论。

回答方式：
- 先给结果（通过/失败、数量、真正重要的错误文本），再给执行过的准确命令和改动路径。
- 引用关键的失败行或数字，不要整段粘贴日志。
"""

BASH_SUBAGENT = SubagentConfig(
    name=NAME,
    description=DESCRIPTION,
    system_prompt=SYSTEM_PROMPT,
    tools=list(TOOLS),
    disallowed_tools=list(DISALLOWED_TOOLS),
    skills=None,  # None = 全部启用技能
    model=INHERIT_MODEL,
    max_turns=MAX_TURNS,
    timeout_seconds=BUILTIN_SUBAGENT_TIMEOUT_SECONDS,  # 段落值 subagents.timeout_seconds 覆盖它
)

__all__ = ["BASH_SUBAGENT", "DESCRIPTION", "DISALLOWED_TOOLS", "MAX_TURNS", "NAME", "SYSTEM_PROMPT", "TOOLS"]
