"""M8 §1 / §5.2 / §18：委派措辞必须**三处一致**。

三处是：lead 系统提示词的 ``{subagent_section}``（§16）、``task`` 的 docstring（§9）、
每个内置子代理的 ``description``（§5.2）。不一致的后果很具体：提示词说"能并行就并行"、
docstring 说"简单活别派"，模型按自己的偏好选，用户看到的是卡片满天飞和更长的总耗时。

本文件覆盖**三处**：lead 系统提示词的 ``{subagent_section}``（§16，含并行开关）、
``task`` 的 docstring（§9）、全部内置子代理的 ``description``（§5.2）。
"""

from __future__ import annotations

import pytest

from agentkit.subagents.builtins import BUILTIN_SUBAGENTS
from agentkit.subagents.builtins import bash_agent, general_purpose
from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.subagents.policy import SubagentPolicy
from agentkit.subagents.routing import (
    ROUTING_COST,
    ROUTING_ISOLATION_BENEFIT,
    ROUTING_PARALLEL_BENEFIT,
    ROUTING_VETOES,
)
from agentkit.tools.builtin.task_tool import TASK_TOOL_NAME, make_task_tool
from fakes.task_tool_harness import FakeDispatcher, make_app_config

BUILTIN_NAMES = list(BUILTIN_SUBAGENTS)
ROUTING_SENTENCES = (ROUTING_PARALLEL_BENEFIT, ROUTING_ISOLATION_BENEFIT, ROUTING_VETOES, ROUTING_COST)


def test_general_purpose_description_carries_the_routing_language() -> None:
    """何时委派只写在通用子代理的描述里，不写进 task 工具。"""
    description = general_purpose.DESCRIPTION
    assert ROUTING_PARALLEL_BENEFIT in description, "收益：并行缩短等待"
    assert ROUTING_ISOLATION_BENEFIT in description, "收益：上下文隔离"
    assert ROUTING_VETOES in description, "两条硬否决：输出依赖 / 写同一批文件（只针对同一轮并行）"
    assert ROUTING_COST in description, "成本：重新了解情况、多一轮综合"
    assert "专家能力" not in description, "继承 lead 的模型和工具，没有专长可言"


@pytest.mark.parametrize("module", [general_purpose, bash_agent])
def test_sequential_descriptions_drop_the_parallel_language(module) -> None:
    """单槽位时并行收益和"同一轮并行"的否决都不成立，要整句删掉。"""
    sequential = module.SEQUENTIAL_DESCRIPTION
    assert ROUTING_PARALLEL_BENEFIT not in sequential
    assert ROUTING_VETOES not in sequential
    assert module.DESCRIPTION != sequential


def test_routing_constants_are_whole_sentences() -> None:
    """常量按策略整句取舍：嵌进别的句子中间会读成"不要委派 依赖……"这种半截话。"""
    for sentence in ROUTING_SENTENCES:
        assert sentence.endswith("。"), sentence
        assert not sentence.startswith(" ")


@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_description_stays_bounded(name: str) -> None:
    """描述进 lead 的发现列表：每加一个类型就多一段，必须有界。"""
    assert 0 < len(BUILTIN_SUBAGENTS[name].description) < 700


@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_system_prompt_forbids_nested_delegation_and_questions(name: str) -> None:
    """模型看不见 ``task``，但仍然会"打算"再派一个帮手、或者向用户提问。"""
    prompt = BUILTIN_SUBAGENTS[name].system_prompt or ""
    assert "不能向用户提问" in prompt
    assert "`ask_clarification`" not in prompt
    assert "不能委派" in prompt or "不能继续委派" in prompt or "你没有 `task` 工具" in prompt


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


def test_the_task_docstring_does_not_decide_when_to_delegate() -> None:
    description = _task_description()
    for sentence in ROUTING_SENTENCES:
        assert sentence not in description
    assert "以该子代理的描述为准" in description


def test_the_task_docstring_tells_the_model_the_boundaries() -> None:
    # docstring 里有换行，断言前先把空白折平（不然测的是排版不是内容）
    description = " ".join(_task_description().split())
    assert "看不到当前对话" in description
    assert "不能继续委派" in description
    assert "失败同样作为工具结果返回" in description
    assert "8000" in description
    assert "不是给你的指令" in description
    assert "相对时间" in description
    assert "``prompt```" not in _task_description()


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
    """发现列表之前的引导句。收益、否决和成本不写在这里。"""
    section = prompt.split("## 子代理委派", 1)[-1]
    return section.split("- `", 1)[0]


def test_the_lead_section_does_not_repeat_routing_language() -> None:
    """何时委派只出现在 general-purpose 的描述行里，不出现在段首。"""
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=3))
    preamble = _preamble(prompt)
    for sentence in ROUTING_SENTENCES:
        assert sentence not in preamble
    assert ROUTING_PARALLEL_BENEFIT in prompt
    assert ROUTING_VETOES in prompt
    assert ROUTING_COST in prompt
    assert "subagent_type" in preamble


def test_a_single_slot_drops_every_parallel_sentence() -> None:
    """§16：``max_concurrent == 1`` → 并行收益与并行否决整句删掉（不是淡化），只说一次派一个。

    提示词还在教并行、中间件却只放行 1 个，模型会空转。描述行也归这条管。
    """
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=1))

    assert ROUTING_PARALLEL_BENEFIT not in prompt
    assert ROUTING_VETOES not in prompt
    assert "同时" not in prompt
    assert "并行执行" not in prompt
    assert ROUTING_ISOLATION_BENEFIT in prompt
    assert "一次只允许一个委派" in prompt


def test_the_parallel_hint_names_the_resolved_slot_count() -> None:
    """数字必须是**解析后**的那个：提示词、中间件、控制器读同一个数（§10.1）。"""
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=2))
    assert "最多同时 2 个" in prompt


def test_the_lead_section_lists_exactly_the_allowed_types() -> None:
    """§16：发现列表 = ``list_subagent_names(allowed=...)``，和 ``task`` 执行同一份过滤。"""
    prompt = _section(policy=SubagentPolicy(allowed=["bash"], max_concurrent=3))
    assert "- `bash`:" in prompt
    assert "general-purpose" not in prompt


def test_the_lead_section_states_the_run_budget() -> None:
    """总量上限和 SubagentLimitMiddleware 读同一个数：模型要能据此规划，而不是撞墙才知道。"""
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=3))
    assert "本次运行最多委派 6 次" in prompt

    tight = apply_prompt_template(
        subagent_enabled=True,
        app_config=make_app_config(allow_host_bash=True, max_total_per_run=4),
        subagent_policy=SubagentPolicy(allowed=None, max_concurrent=3),
    )
    assert "本次运行最多委派 4 次" in tight


def test_the_lead_section_says_what_to_do_with_results() -> None:
    """委派结果不是给用户的最终答复：要核对、综合、保留出处、没找到就如实说。"""
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=3))
    assert "拿到结果后" in prompt
    assert "出处" in prompt
    assert "如实转述" in prompt


def test_the_lead_section_points_at_the_skills_index_when_skill_finder_is_listed() -> None:
    prompt = _section(SubagentPolicy(allowed=None, max_concurrent=3))
    assert "技能见下方索引" in prompt
    assert "load_skill" in prompt
    assert "私有知识库" not in prompt

    bash_only = _section(SubagentPolicy(allowed=["bash"], max_concurrent=3))
    assert "技能见下方索引" not in bash_only
    assert "load_skill" not in bash_only


def test_the_role_sets_the_reply_language() -> None:
    prompt = apply_prompt_template(app_config=make_app_config())
    assert "默认使用用户提问所用的语言回答" in prompt


def test_a_hard_reject_produces_no_section_at_all() -> None:
    prompt = _section(policy=SubagentPolicy(allowed=[], max_concurrent=3))
    assert "## 子代理委派" not in prompt
    for sentence in ROUTING_SENTENCES:
        assert sentence not in prompt


def test_a_whitelist_that_matches_nothing_produces_no_section() -> None:
    """空菜单比没有菜单更糟：模型会挨个试。"""
    prompt = _section(policy=SubagentPolicy(allowed=["nope"], max_concurrent=3))
    assert "## 子代理委派" not in prompt


def test_the_discovery_entry_keeps_the_full_description() -> None:
    """路由描述是 lead 决定是否委派的唯一依据，不截断。"""
    long_description = "完整路由说明。" * 80
    app_config = make_app_config(custom_agents={"verbose": {"description": long_description}})
    prompt = apply_prompt_template(
        subagent_enabled=True,
        app_config=app_config,
        subagent_policy=SubagentPolicy(allowed=["verbose"], max_concurrent=3),
    )
    line = next(line for line in prompt.splitlines() if line.startswith("- `verbose`:"))
    assert long_description in line
    assert not line.endswith("…")
