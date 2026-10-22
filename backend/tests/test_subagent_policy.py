"""M8 §16：调用方策略（``allowed_subagents`` / ``max_concurrent``）要在 run 开始时快照。

三件事必须同时成立：

* 快照是**值**：run 中途改 YAML 不影响本次 run（否则前后两次委派用两套策略）；
* 快照同时喂给**提示词的发现**和 **``task`` 的执行**——只过滤一边，要么看得见调不动，
  要么看不见却能调；
* ``[]``（硬拒绝）要真的把段和工具都拿掉，不是渲染成空段。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentkit.agents.leader_agent.agent import assemble_agent
from agentkit.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware
from agentkit.config import AppConfig
from agentkit.subagents.policy import (
    ALLOWED_SUBAGENTS_CONTEXT_KEY,
    SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY,
    SubagentPolicy,
    allowed_subagents_from_context,
    max_concurrent_from_context,
    snapshot_subagent_policy,
)
from agentkit.subagents.registry import list_subagent_names
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.chat_model import FakeChatModel
from fakes.task_tool_harness import FakeDispatcher, FakeRuntime, make_tool

UNSET = object()


def _config(
    *,
    allowed_subagents: object = UNSET,
    subagent_max_concurrent: int | None = None,
    max_running: int | None = None,
    custom_agents: dict | None = None,
) -> AppConfig:
    """只填需要的段落：``agents.*`` 是调用方策略，``subagents.*`` 是子代理侧配置。"""
    agents: dict = {}
    if allowed_subagents is not UNSET:
        agents["allowed_subagents"] = allowed_subagents
    if subagent_max_concurrent is not None:
        agents["subagent_max_concurrent"] = subagent_max_concurrent
    raw: dict = {
        "models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}],
        "sandbox": {
            "use": "agentkit.sandbox.local:LocalSandboxProvider",
            "allow_host_bash": True,
        },
        "agents": agents,
        "subagents": {} if custom_agents is None else {"custom_agents": custom_agents},
    }
    if max_running is not None:
        raw["subagent_runtime"] = {"max_running": max_running}
    return AppConfig.model_validate(raw)


# ---------------------------------------------------------------- 快照本身


def test_the_default_policy_allows_everything() -> None:
    policy = snapshot_subagent_policy(_config())
    assert policy.allowed is None and policy.enabled and policy.parallel
    assert policy.max_concurrent == 3  # = subagent_runtime.max_running 的默认值


def test_an_empty_whitelist_is_a_hard_reject() -> None:
    policy = snapshot_subagent_policy(_config(allowed_subagents=[]))
    assert policy.allowed == []
    assert policy.enabled is False


def test_a_whitelist_is_copied_into_the_snapshot() -> None:
    app_config = _config(allowed_subagents=["bash"])
    policy = snapshot_subagent_policy(app_config)

    app_config.agents.allowed_subagents.append("general-purpose")  # 运行中有人改了配置

    assert policy.allowed == ["bash"], "快照是值：改配置不许影响已经发出去的那一份"


def test_the_declared_concurrency_is_clamped_by_the_frozen_capacity() -> None:
    assert snapshot_subagent_policy(_config(subagent_max_concurrent=2)).max_concurrent == 2
    declared = _config(subagent_max_concurrent=8)
    assert snapshot_subagent_policy(declared, max_running=2).max_concurrent == 2
    # 64 是硬天花板：只有槽位够多时它才成为瓶颈（否则瓶颈是 ``max_running``）
    assert snapshot_subagent_policy(
        _config(subagent_max_concurrent=100), max_running=64
    ).max_concurrent == 64
    assert snapshot_subagent_policy(_config(subagent_max_concurrent=100)).max_concurrent == 3
    assert snapshot_subagent_policy(_config(), max_running=1).max_concurrent == 1


def test_the_snapshot_round_trips_through_the_runtime_context() -> None:
    policy = SubagentPolicy(allowed=["bash"], max_concurrent=2)
    context = policy.to_context()

    assert context == {
        ALLOWED_SUBAGENTS_CONTEXT_KEY: ["bash"],
        SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY: 2,
    }
    assert allowed_subagents_from_context(context) == ["bash"]
    assert max_concurrent_from_context(context, 9) == 2


def test_a_missing_snapshot_means_everything_is_allowed() -> None:
    assert allowed_subagents_from_context({}) is None
    assert allowed_subagents_from_context(None) is None
    assert max_concurrent_from_context({}, 3) == 3


@pytest.mark.parametrize("malformed", ["all", ["bash", 7], {"name": "bash"}])
def test_a_malformed_snapshot_fails_closed(malformed: object) -> None:
    """策略读不出来时宁可不让派，也不要按"全部允许"放行。"""
    assert allowed_subagents_from_context({ALLOWED_SUBAGENTS_CONTEXT_KEY: malformed}) == []


# ---------------------------------------------------------------- 发现 = 执行


def test_discovery_lists_exactly_what_the_task_tool_accepts() -> None:
    """§16：提示词的发现列表和 ``task`` 的执行必须用同一份快照。"""
    app_config = _config(
        allowed_subagents=["bash"],
        custom_agents={"researcher": {"description": "x"}},
    )
    policy = snapshot_subagent_policy(app_config)
    names = list_subagent_names(app_config=app_config, allowed=policy.allowed)

    assert names == ["bash"]

    tool = make_task_tool(
        dispatcher=FakeDispatcher(),
        app_config=app_config,
        poll_interval=0.01,
    )
    runtime = FakeRuntime(context={"run_id": "run-1", **policy.to_context()})
    runtime.tools = [make_tool("read_file")]

    listed = asyncio.run(
        tool.coroutine(runtime=runtime, prompt="p", subagent_type="bash", tool_call_id="call_1")
    )
    del listed  # 白名单里的类型能进执行器（dispatcher 是替身，不真跑图）

    rejected = asyncio.run(
        tool.coroutine(
            runtime=runtime, prompt="p", subagent_type="researcher", tool_call_id="call_2"
        )
    )
    message = rejected.update["messages"][0]
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "bash" in message.content, "拒绝时要列出可用类型，否则模型只能瞎猜"


def test_a_missing_snapshot_keeps_the_previous_behaviour() -> None:
    """没有快照（老调用方 / 直接调 factory）→ 全部允许，不是"硬拒绝"。"""
    app_config = _config()
    tool = make_task_tool(
        dispatcher=FakeDispatcher(),
        app_config=app_config,
        poll_interval=0.01,
    )
    runtime = FakeRuntime(context={"run_id": "run-1"})
    runtime.tools = [make_tool("read_file")]

    command = asyncio.run(
        tool.coroutine(
            runtime=runtime, prompt="p", subagent_type="general-purpose", tool_call_id="call_1"
        )
    )
    # 没有快照时 allowed_subagents 是 None（全部允许）→ 走到调度而不是被拒绝
    assert command.update["delegations"][0]["subagent_type"] == "general-purpose"


# ---------------------------------------------------------------- 接线


def _capture_assembly(monkeypatch) -> dict:
    captured: dict = {}
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_chat_model",
        lambda *args, **kwargs: FakeChatModel(reply="ok"),
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.get_available_tools",
        lambda *args, **kwargs: [make_tool("read_file")],
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_agent",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(**kwargs),
    )
    return captured


def test_the_assembly_stamps_the_snapshot_into_the_run_context(monkeypatch) -> None:
    from agentkit.subagents.runtime import SubagentRuntime

    captured = _capture_assembly(monkeypatch)
    app_config = _config(allowed_subagents=["bash"], subagent_max_concurrent=1)
    runtime = SubagentRuntime.from_app_config(app_config)
    config = {"configurable": {"subagent_enabled": True}, "context": {"thread_id": "t1"}}

    assemble_agent(config, app_config=app_config, subagent_runtime=runtime)

    assert config["context"]["thread_id"] == "t1", "不能把原来的 context 覆盖掉"
    assert config["context"][ALLOWED_SUBAGENTS_CONTEXT_KEY] == ["bash"]
    assert config["context"][SUBAGENT_MAX_CONCURRENT_CONTEXT_KEY] == 1

    limits = [m for m in captured["middleware"] if isinstance(m, SubagentLimitMiddleware)]
    assert [limit.max_concurrent for limit in limits] == [1], "中间件读同一份快照里的数"


def test_a_hard_reject_removes_both_the_tool_and_the_section(monkeypatch) -> None:
    """§16：``[]`` = 硬拒绝——提示词里不出现委派段，``task`` 也不装进工具表。"""
    captured = _capture_assembly(monkeypatch)

    assemble_agent(
        {"configurable": {"subagent_enabled": True}},
        app_config=_config(allowed_subagents=[]),
    )

    assert [tool.name for tool in captured["tools"]] == ["read_file"], "task 不许出现"
    assert "## 子代理委派" not in (captured["system_prompt"] or "")
    # 硬拒绝由「提示词 + 工具表」落地：中间件的 ``max_concurrent`` 被 clamp 到 >=1
    # （§10.3 的 constructor），表达不了"一个都不许"，所以它不参与硬拒绝。
    limits = [m for m in captured["middleware"] if isinstance(m, SubagentLimitMiddleware)]
    assert all(limit.max_concurrent >= 1 for limit in limits)


def test_a_whitelist_shrinks_the_tool_table_and_the_prompt(monkeypatch) -> None:
    captured = _capture_assembly(monkeypatch)

    assemble_agent(
        {"configurable": {"subagent_enabled": True}},
        app_config=_config(allowed_subagents=["bash"]),
    )

    assert [tool.name for tool in captured["tools"]] == ["read_file", "task"]
    prompt = captured["system_prompt"] or ""
    assert "## 子代理委派" in prompt
    assert "`bash`" in prompt
    assert "`general-purpose`" not in prompt, "白名单外的类型不许出现在发现列表里"


def test_the_snapshot_survives_a_config_change_mid_run(monkeypatch) -> None:
    """§16 的核心不变量：``task`` 内部绝不重读可变的 agent 配置。"""
    captured = _capture_assembly(monkeypatch)
    app_config = _config(
        allowed_subagents=["bash"],
        custom_agents={"researcher": {"description": "x"}},
    )
    config = {"configurable": {"subagent_enabled": True}, "context": {}}

    assemble_agent(config, app_config=app_config)
    app_config.agents.allowed_subagents.append("researcher")  # 运行中途有人改了 YAML

    assert config["context"][ALLOWED_SUBAGENTS_CONTEXT_KEY] == ["bash"]
    assert "`researcher`" not in (captured["system_prompt"] or "")
