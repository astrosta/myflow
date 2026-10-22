"""M8 §1 / §5.2 / §18：委派措辞必须**三处一致**。

三处是：lead 系统提示词的 ``{subagent_section}``（§16）、``task`` 的 docstring（§9）、
每个内置子代理的 ``description``（§5.2）。不一致的后果很具体：提示词说"能并行就并行"、
docstring 说"简单活别派"，模型按自己的偏好选，用户看到的是卡片满天飞和更长的总耗时。

本文件覆盖**三处**：lead 系统提示词的 ``{subagent_section}``（§16，含并行开关）、
``task`` 的 docstring（§9）、两个内置子代理的 ``description``（§5.2）。
"""

from __future__ import annotations

import pytest

from agentkit.subagents.builtins import BUILTIN_SUBAGENTS
from agentkit.subagents.builtins import bash_agent, general_purpose
from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.subagents.policy import SubagentPolicy
from agentkit.subagents.routing import (
    ROUTING_BENEFITS,
    ROUTING_BENEFITS_SEQUENTIAL,
    ROUTING_COST,
    ROUTING_VETOES,
)
from agentkit.tools.builtin.task_tool import TASK_TOOL_NAME, make_task_tool
from fakes.task_tool_harness import FakeDispatcher, make_app_config

BUILTIN_NAMES = ["general-purpose", "bash"]


@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_description_carries_the_shared_routing_language(name: str) -> None:
    description = BUILTIN_SUBAGENTS[name].description
    assert ROUTING_BENEFITS in description, "收益三条：并行 / 专家能力 / 上下文隔离"
    assert ROUTING_VETOES in description, "两条硬否决：输出依赖 / 写同一个文件"
    assert ROUTING_COST in description, "成本：启动、重复发现、综合那一轮"


@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_description_stays_bounded(name: str) -> None:
    """描述进 lead 的发现列表：每加一个类型就多一段，必须有界。"""
    assert 0 < len(BUILTIN_SUBAGENTS[name].description) < 700


@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_system_prompt_forbids_nested_delegation_and_questions(name: str) -> None:
    """模型看不见 ``task``，但仍然会"打算"再派一个帮手、或者向用户提问。"""
    prompt = BUILTIN_SUBAGENTS[name].system_prompt or ""
    assert "你没有 `task` 工具" in prompt
    assert "这里没有 `ask_clarification`" in prompt


@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_builtins_also_block_them_at_the_tool_layer(name: str) -> None:
    disallowed = set(BUILTIN_SUBAGENTS[name].disallowed_tools or [])
    assert {"task", "ask_clarification"} <= disallowed


def test_bash_description_rejects_routine_commands() -> None:
    """§5.2：常规 git/build/test/deploy **不够**成为委派理由，描述里必须写明。"""
    description = bash_agent.DESCRIPTION
    lowered = description.lower()
    for word in ("git", "构建", "测试", "部署"):
        assert word in lowered
    assert "不足以成为委派理由" in description


def test_general_purpose_inherits_tools_while_bash_declares_a_subset() -> None:
    assert general_purpose.GENERAL_PURPOSE_SUBAGENT.tools is None
    assert general_purpose.GENERAL_PURPOSE_SUBAGENT.skills is None
    assert bash_agent.BASH_SUBAGENT.tools == ["bash", "ls", "read_file", "write_file", "str_replace"]


def test_both_builtins_inherit_the_lead_model() -> None:
    for config in BUILTIN_SUBAGENTS.values():
        assert config.model == "inherit"


# ------------------------------------------------------- task 工具的 docstring（§9）


def _task_description() -> str:
    tool = make_task_tool(dispatcher=FakeDispatcher(), app_config=make_app_config())
    return tool.description or ""


def test_the_task_docstring_carries_the_same_routing_language() -> None:
    description = _task_description()
    assert ROUTING_BENEFITS in description
    assert ROUTING_VETOES in description
    assert ROUTING_COST in description


def test_the_task_docstring_tells_the_model_the_boundaries() -> None:
    # docstring 里有换行，断言前先把空白折平（不然测的是排版不是内容）
    description = " ".join(_task_description().split())
    assert "看不到当前对话" in description
    assert "不能继续委派" in description
    assert "失败也会作为工具结果返回" in description


def test_the_task_tool_takes_exactly_three_parameters() -> None:
    """§9：参数刻意精简；§20：**不收** ``acceptance_criteria``（那条通道会让 lead 以为有验收）。"""
    tool = make_task_tool(dispatcher=FakeDispatcher(), app_config=make_app_config())
    fields = set(tool.args_schema.model_fields)
    assert fields == {"prompt", "subagent_type", "description", "runtime", "tool_call_id"}
    assert "acceptance_criteria" not in fields
    assert tool.name == TASK_TOOL_NAME == "task"


# ------------------------------------------------------- lead 提示词的委派段（§16）


def _section(policy: SubagentPolicy) -> str:
    """``allow_host_bash=True``：``bash`` 内置要本地 provider 放行才**存在**（§5.1 的可用性判定）。"""
    return apply_prompt_template(
        subagent_enabled=True,
        app_config=make_app_config(allow_host_bash=True),
        subagent_policy=policy,
    )


def _preamble(prompt: str) -> str:
    """发现列表**之前**的那几句（收益 / 否决 / 成本）——§16 管的就是这几句。

    发现列表本身是各类型自己的 ``description``（§5.2 要求它带全套措辞），不归 §16 管：
    单槽位时它仍会照原文出现，这是已知的措辞重叠，不是本段能删的。
    """
    section = prompt.split("## 子代理委派", 1)[-1]
    return section.split("可用类型", 1)[0]


def test_the_lead_section_carries_the_same_routing_language() -> None:
    """§1：三处同一套措辞；lead 段是模型最容易照做的那一处，缺了它前两处等于白写。"""
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=3))
    assert ROUTING_BENEFITS in _preamble(prompt)
    assert ROUTING_VETOES in prompt
    assert ROUTING_COST in prompt
    assert "Delegate work that you can complete in a single step" not in prompt


def test_a_single_slot_drops_the_parallel_benefit_and_the_parallel_hint() -> None:
    """§16：``max_concurrent == 1`` → 删掉并行那条收益（不是淡化）。

    提示词还在教并行 3 个、中间件只放行 1 个，模型会空转；这条测的就是那两句必须消失。
    """
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=1))

    assert ROUTING_BENEFITS not in _preamble(prompt), "并行那条收益必须整条删掉"
    assert ROUTING_BENEFITS_SEQUENTIAL in _preamble(prompt), "剩两条：专家能力 / 上下文隔离"
    assert "并行执行" not in prompt
    assert "一次只允许一个委派" in prompt, "删了并行指引要补上「一次只能派一个」"


def test_the_parallel_hint_names_the_resolved_slot_count() -> None:
    """数字必须是**解析后**的那个：提示词、中间件、控制器读同一个数（§10.1）。"""
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=2))
    assert "最多同时 2 个" in prompt


def test_the_lead_section_lists_exactly_the_allowed_types() -> None:
    """§16：发现列表 = ``list_subagent_names(allowed=...)``，和 ``task`` 执行同一份过滤。"""
    prompt = _section(policy=SubagentPolicy(allowed=["bash"], max_concurrent=3))
    assert "- `bash`:" in prompt
    assert "general-purpose" not in prompt


def test_a_hard_reject_produces_no_section_at_all() -> None:
    prompt = _section(policy=SubagentPolicy(allowed=[], max_concurrent=3))
    assert "## 子代理委派" not in prompt
    assert ROUTING_COST not in prompt


def test_a_whitelist_that_matches_nothing_produces_no_section() -> None:
    """空菜单比没有菜单更糟：模型会挨个试。"""
    prompt = _section(policy=SubagentPolicy(allowed=["nope"], max_concurrent=3))
    assert "## 子代理委派" not in prompt


def test_the_discovery_entry_description_is_truncated() -> None:
    """提示词必须有界（和 M7 的技能索引同一条纪律）。"""
    from agentkit.agents.leader_agent.prompt import DISCOVERY_DESCRIPTION_MAX_CHARS

    long_description = "x" * (DISCOVERY_DESCRIPTION_MAX_CHARS * 3)
    app_config = make_app_config(custom_agents={"verbose": {"description": long_description}})
    prompt = apply_prompt_template(
        subagent_enabled=True,
        app_config=app_config,
        subagent_policy=SubagentPolicy(allowed=["verbose"], max_concurrent=3),
    )
    line = next(line for line in prompt.splitlines() if line.startswith("- `verbose`:"))
    assert line.endswith("…")
    assert len(line) < DISCOVERY_DESCRIPTION_MAX_CHARS + 20
