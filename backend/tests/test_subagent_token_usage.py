"""M8 §15：子代理的 token 归到**父 run** 的账上（D10）。

三条通道各测一遍，端到端再测一次：

1. 运行中：``SubagentTokenCollector`` 把累计快照写进 holder（前端卡片实时更新）；
2. 终态：``task`` 工具汇总成 ``ToolMessage.subagent_token_usage``（刷新后仍可显示，另见
   ``test_subagent_status_contract.py``）；
3. 父账本：父循环调 ``record_external_llm_usage_records``，落在 ``subagent_tokens`` 这一栏
   ——**不混进 lead 的消耗**。

端到端那一条还兼职钉住 §7.2 的"重复计账"：父 run 的累加器必须**不被**子代理继承，
否则同一个子代理调用会既进 lead 栏（继承的回调）又进 subagent 栏（显式上报）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.callbacks import CallbackManager
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables.config import var_child_runnable_config

from agentkit.persistence.events.memory import MemoryRunEventStore
from agentkit.runtime.events.journal import RunJournal
from agentkit.runtime.runs.usage import UsageCollector
from agentkit.runtime.runs.usage_metrics import UNKNOWN_MODEL
from agentkit.subagents.results import SubagentResult, SubagentStatus
from agentkit.subagents.token_collector import SubagentTokenCollector, attach_token_collector
from agentkit.tools.builtin.task_tool import _find_usage_recorder, make_task_tool
from fakes.task_tool_harness import FakeDispatcher, FakeRuntime, make_app_config, make_tool

SUBAGENT_REPLY = "SUBAGENT-REPLY-XYZ"


# ---------------------------------------------------------------- 夹具


def _holder(task_id: str = "exec-1") -> SubagentResult:
    return SubagentResult(task_id=task_id, trace_id="trace-1", status=SubagentStatus.PENDING)


def _collector(holder: SubagentResult | None = None, *, name: str = "bash", model: str | None = "deepseek-reasoner"):
    return SubagentTokenCollector(holder or _holder(), subagent_name=name, model_name=model)


def _response(
    *,
    input_tokens: int = 3,
    output_tokens: int = 5,
    model: str | None = "deepseek-reasoner",
    cache_read: int = 0,
    with_usage: bool = True,
) -> LLMResult:
    metadata: dict | None = None
    if with_usage:
        metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if cache_read:
            metadata["input_token_details"] = {"cache_read": cache_read}
    message = AIMessage(
        content="child reply",
        usage_metadata=metadata,
        response_metadata={"model_name": model} if model else {},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def _child_record(
    *,
    input_tokens: int = 4,
    output_tokens: int = 6,
    model: str = "deepseek-reasoner",
    execution_id: str = "exec-1",
    llm_run_id: str = "llm-1",
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cache_read_tokens": 0,
        "model": model,
        "caller": "subagent:bash",
        "execution_id": execution_id,
        "llm_run_id": llm_run_id,
    }


def _lead_collector() -> UsageCollector:
    return UsageCollector(SimpleNamespace(), "r1")


def _journal(usage: UsageCollector | None = None) -> RunJournal:
    record = SimpleNamespace(
        thread_id="t1", run_id="r1", user_id="u1", abort_event=threading.Event()
    )
    return RunJournal(MemoryRunEventStore(), record, usage_sink=usage)


class _PlainHandler:
    """继承来的普通回调：一个都不许丢。"""


# ---------------------------------------------------------------- 1. 采集器


def test_each_response_publishes_a_cumulative_snapshot() -> None:
    """§15 第 1 点：快照是**累计**的——holder 的写入口是替换语义，追加会让同一次调用记两遍。"""
    holder = _holder()
    collector = _collector(holder)

    asyncio.run(collector.on_llm_end(_response(), run_id=uuid4()))
    assert [record["total_tokens"] for record in holder.token_usage_records] == [8]

    asyncio.run(collector.on_llm_end(_response(), run_id=uuid4()))
    assert [record["total_tokens"] for record in holder.token_usage_records] == [8, 8]
    assert [record["total_tokens"] for record in collector.records()] == [8, 8]


def test_the_same_llm_run_id_is_counted_once() -> None:
    """流式提供商会先回一次空 usage，再回放一次带 usage 的：同一个 run_id 只算一次。"""
    holder = _holder()
    collector = _collector(holder)
    run_id = uuid4()

    asyncio.run(collector.on_llm_end(_response(), run_id=run_id))
    asyncio.run(collector.on_llm_end(_response(), run_id=run_id))

    assert len(holder.token_usage_records) == 1


def test_a_response_without_usage_is_not_recorded_as_zero() -> None:
    """§15：``total_tokens <= 0`` 直接跳过。0 和"未知"是不同的信息。"""
    holder = _holder()
    collector = _collector(holder)

    asyncio.run(collector.on_llm_end(_response(with_usage=False), run_id=uuid4()))
    asyncio.run(collector.on_llm_end(_response(input_tokens=0, output_tokens=0), run_id=uuid4()))

    assert holder.token_usage_records == []
    assert collector.records() == []


def test_the_record_carries_caller_model_and_cache_read() -> None:
    holder = _holder()
    collector = _collector(holder, name="bash")
    run_id = uuid4()

    asyncio.run(collector.on_llm_end(_response(input_tokens=10, output_tokens=4, cache_read=7), run_id=run_id))

    record = holder.token_usage_records[0]
    assert record["caller"] == "subagent:bash", "§15：父 journal 按这个分桶"
    assert record["model"] == "deepseek-reasoner"
    assert (record["input_tokens"], record["output_tokens"]) == (10, 4)
    assert record["total_tokens"] == 14, "口径 = 输入 + 输出（与 provider 的 total 一致）"
    assert record["cache_read_tokens"] == 7
    assert record["execution_id"] == holder.task_id, "父账本的去重键"
    assert record["llm_run_id"] == str(run_id)


def test_the_dispatch_model_is_the_fallback_for_the_record() -> None:
    """provider 不报模型名时用 dispatch 解析好的那个（§4.3），否则模型分桶会全是 unknown。"""
    holder = _holder()
    asyncio.run(
        _collector(holder, name="bash", model="qwen-max").on_llm_end(
            _response(model=None), run_id=uuid4()
        )
    )
    assert holder.token_usage_records[0]["model"] == "qwen-max"

    other = _holder("exec-2")
    asyncio.run(
        _collector(other, name="bash", model=None).on_llm_end(_response(model=None), run_id=uuid4())
    )
    assert other.token_usage_records[0]["model"] == UNKNOWN_MODEL


def test_the_collector_is_not_loop_bound() -> None:
    """§7.2 的对照：它**就该**在隔离循环上跑，打了标记反而会被摘掉、用量再也收不到。

    ``UsageCollector`` 正相反：它是父循环的累加器，必须被摘掉，否则子代理的调用会被记两遍。
    """
    assert getattr(_collector(), "myflow_loop_bound", False) is False
    assert _journal().myflow_loop_bound is True
    assert _lead_collector().myflow_loop_bound is True


def test_a_terminal_holder_keeps_the_snapshot_it_had() -> None:
    """终态之后 holder 不再改快照（§8）：迟到的回调既不改数字，也不许抛。"""
    holder = _holder()
    collector = _collector(holder)
    asyncio.run(collector.on_llm_end(_response(), run_id=uuid4()))
    holder.try_set_terminal(SubagentStatus.COMPLETED, result="done")

    asyncio.run(collector.on_llm_end(_response(), run_id=uuid4()))

    assert len(holder.token_usage_records) == 1, "父账本交付的是这份快照，终态后就定稿了"


# ---------------------------------------------------------------- 挂到子 run 上


def test_attach_appends_without_dropping_inherited_handlers() -> None:
    """``ensure_config`` 是替换语义：只传 collector 会把 §3.1 依赖的流处理器一起丢掉。"""
    inherited_handler = _PlainHandler()
    collector = _collector()
    config = {"configurable": {"thread_id": "t1"}}

    merged = attach_token_collector(config, collector, inherited={"callbacks": [inherited_handler]})

    assert merged["callbacks"] == [inherited_handler, collector]
    assert merged["configurable"] == {"thread_id": "t1"}
    assert "callbacks" not in config, "不许就地改调用方的 config"


def test_attach_copies_a_callback_manager_without_touching_the_parent() -> None:
    inherited_handler = _PlainHandler()
    manager = CallbackManager(
        handlers=[inherited_handler], inheritable_handlers=[inherited_handler]
    )
    collector = _collector()

    merged = attach_token_collector({"configurable": {}}, collector, inherited={"callbacks": manager})

    assert merged["callbacks"] is not manager
    assert merged["callbacks"].handlers == [inherited_handler, collector]
    assert merged["callbacks"].inheritable_handlers == [inherited_handler, collector]
    assert manager.handlers == [inherited_handler], "父 run 的那份管理器不许被改到"


def test_attach_makes_the_collector_inheritable() -> None:
    """图节点里的模型调用是**子 run**：不在 ``inheritable_handlers`` 里就一次都收不到。"""
    collector = _collector()
    manager = CallbackManager(handlers=[_PlainHandler()])

    merged = attach_token_collector({}, collector, inherited={"callbacks": manager})

    assert merged["callbacks"].handlers[-1] is collector
    assert merged["callbacks"].inheritable_handlers == [collector]


def test_attach_wraps_a_bare_handler_and_reads_the_context_var() -> None:
    """``callbacks`` 还能是一个裸 handler（§7.2 的拷贝规则支持这种形状）。"""
    inherited_handler = _PlainHandler()
    token = var_child_runnable_config.set(
        {"configurable": {"thread_id": "t1"}, "callbacks": inherited_handler}
    )
    try:
        merged = attach_token_collector({"recursion_limit": 5}, _collector())
    finally:
        var_child_runnable_config.reset(token)

    assert merged["callbacks"][0] is inherited_handler
    assert isinstance(merged["callbacks"][1], SubagentTokenCollector)
    assert merged["recursion_limit"] == 5


# ---------------------------------------------------------------- 3. 父账本


def test_external_records_land_in_the_subagent_bucket() -> None:
    """§15：算在这次 run 的账上，但**单独一栏**。"""
    usage = _lead_collector()
    asyncio.run(usage.on_llm_end(_response(), run_id=uuid4()))  # lead 的一次调用：3/5

    usage.record_external_llm_usage_records([_child_record(input_tokens=4, output_tokens=6)])

    totals = usage.totals()
    assert totals.lead_agent_tokens == 8, "lead 那一栏不许被子代理的消耗污染"
    assert totals.subagent_tokens == 10
    assert (totals.total_input_tokens, totals.total_output_tokens) == (7, 11)
    assert totals.llm_call_count == 2
    assert totals.token_usage_by_model["deepseek-reasoner"] == {
        "input": 7,
        "output": 11,
        "cache_read": 0,
    }


def test_the_attribution_invariant_holds(caplog) -> None:
    """``RunTotals`` 的对账：lead + subagent + middleware 必须等于总数（不然它会记 ERROR）。"""
    usage = _lead_collector()
    usage.record_external_llm_usage_records([_child_record()])

    with caplog.at_level(logging.ERROR, logger="agentkit.runtime.runs.totals"):
        totals = usage.totals()

    attributed = totals.lead_agent_tokens + totals.subagent_tokens + totals.middleware_tokens
    assert attributed == totals.total_input_tokens + totals.total_output_tokens
    assert not caplog.records


def test_a_record_delivered_twice_is_counted_once() -> None:
    """§7.2 的"重复计账"第二道保险：``(execution_id, llm_run_id)`` 幂等。"""
    usage = _lead_collector()
    record = _child_record()

    usage.record_external_llm_usage_records([record])
    usage.record_external_llm_usage_records([dict(record)])

    assert usage.totals().subagent_tokens == 10


def test_the_caller_decides_the_column() -> None:
    """§15 的分栏：父账本按 ``caller`` 落到 M3 的三个维度（lead / subagent / middleware）。"""
    usage = _lead_collector()

    usage.record_external_llm_usage_records(
        [
            _child_record(input_tokens=4, output_tokens=6, execution_id="e1", llm_run_id="l1"),
            {
                **_child_record(input_tokens=1, output_tokens=1, execution_id="e2", llm_run_id="l2"),
                "caller": "middleware:token_usage",
            },
        ]
    )

    totals = usage.totals()
    assert (totals.subagent_tokens, totals.middleware_tokens) == (10, 2)
    assert (totals.total_input_tokens, totals.total_output_tokens) == (5, 7)


def test_an_unreadable_caller_is_still_counted_as_a_delegation() -> None:
    """认不出来的 caller 不许把用量丢掉：宁可进"委派"栏，也别让总数对不上。"""
    usage = _lead_collector()

    usage.record_external_llm_usage_records([{"input_tokens": 2, "output_tokens": 3, "caller": 7}])

    totals = usage.totals()
    assert totals.subagent_tokens == 5
    assert totals.middleware_tokens == 0


def test_records_without_an_identity_are_still_counted() -> None:
    """没有去重键（老记录 / 手写调用）时宁可多算，也不要静默丢掉整批用量。"""
    usage = _lead_collector()
    usage.record_external_llm_usage_records([{"input_tokens": 1, "output_tokens": 2}])
    usage.record_external_llm_usage_records([{"input_tokens": 1, "output_tokens": 2}])
    assert usage.totals().subagent_tokens == 6


def test_zero_and_malformed_records_are_skipped() -> None:
    usage = _lead_collector()

    usage.record_external_llm_usage_records(
        [
            None,
            "nonsense",
            {"input_tokens": 0, "output_tokens": 0},
            {"input_tokens": -5, "output_tokens": True},
            {"input_tokens": 6, "output_tokens": 4},
        ]
    )

    totals = usage.totals()
    assert totals.subagent_tokens == 10
    assert totals.llm_call_count == 1


# ---------------------------------------------------------------- 转发与接缝


def test_the_journal_forwards_to_the_usage_sink() -> None:
    """§15 第 3 点：``task`` 工具在父循环里调的就是这个方法（自己一个 token 都不记）。"""
    usage = _lead_collector()
    journal = _journal(usage)

    journal.record_external_llm_usage_records([_child_record()])

    assert usage.totals().subagent_tokens == 10


def test_a_journal_without_a_sink_is_harmless(caplog) -> None:
    journal = _journal()

    with caplog.at_level(logging.DEBUG, logger="agentkit.runtime.events.journal"):
        journal.record_external_llm_usage_records([_child_record()])

    assert "no usage sink attached" in caplog.text


def test_the_task_tool_reports_into_the_worker_callback_order() -> None:
    """worker 组装的是 ``[..., usage, journal]``：解析到的必须是累加器本身（§9 按方法名找）。"""
    usage = _lead_collector()
    journal = _journal(usage)
    dispatcher = FakeDispatcher(usage=(_child_record(),))
    tool = make_task_tool(
        dispatcher=dispatcher,
        app_config=make_app_config(allow_host_bash=True),
        poll_interval=0.01,
    )
    runtime = FakeRuntime(config={"callbacks": [usage, journal]}, context={"run_id": "r1"})
    runtime.tools = [make_tool("read_file")]

    assert _find_usage_recorder(runtime) is usage

    asyncio.run(
        tool.coroutine(
            runtime=runtime, prompt="go", subagent_type="general-purpose", tool_call_id="call_1"
        )
    )

    assert usage.totals().subagent_tokens == 10


def test_the_task_tool_still_reports_when_only_a_journal_is_wired() -> None:
    """嵌入 / 单测的调用方手里只有 journal——§15 的入口留在它身上，就是为这一条路。"""
    usage = _lead_collector()
    journal = _journal(usage)
    tool = make_task_tool(
        dispatcher=FakeDispatcher(usage=(_child_record(),)),
        app_config=make_app_config(allow_host_bash=True),
        poll_interval=0.01,
    )
    runtime = FakeRuntime(config={"callbacks": [journal]}, context={"run_id": "r1"})
    runtime.tools = [make_tool("read_file")]

    assert _find_usage_recorder(runtime) is journal

    asyncio.run(
        tool.coroutine(
            runtime=runtime, prompt="go", subagent_type="general-purpose", tool_call_id="call_1"
        )
    )

    assert usage.totals().subagent_tokens == 10


# ---------------------------------------------------------------- 端到端


def test_subagent_tokens_land_on_the_parent_run_but_not_in_the_lead_column(monkeypatch) -> None:
    """§15 的验收：真父图 + 真隔离循环 + 真累加器，两个数字都要对。

    子代理一次模型调用（3/5），lead 两次（发起委派 + 最终答复）。如果父 run 的累加器被子代理
    继承（§7.2 的"重复计账"），lead 栏会变成 24——所以这条同时钉住了 ``UsageCollector`` 的
    ``myflow_loop_bound`` 标记。
    """
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from agentkit.agents.thread_state import ThreadState
    from agentkit.config import AppConfig
    from agentkit.subagents import executor as subagents
    from fakes.chat_model import FakeChatModel, ScriptedChatModel

    app_config = AppConfig.model_validate(
        {"models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]}
    )
    monkeypatch.setattr(
        subagents, "create_chat_model", lambda **kwargs: FakeChatModel(reply=SUBAGENT_REPLY)
    )
    monkeypatch.setattr(subagents, "build_subagent_runtime_middlewares", lambda *a, **kw: [])

    task_tool = make_task_tool(
        dispatcher=subagents.SubagentExecutor(app_config=app_config),
        app_config=app_config,
        poll_interval=0.01,
    )
    parent = create_agent(
        model=ScriptedChatModel(
            replies=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "id": "call_1",
                            "args": {"prompt": "go", "subagent_type": "general-purpose"},
                        }
                    ],
                ),
                "LEAD-ANSWER",
            ]
        ),
        tools=[task_tool],
        middleware=[],
        state_schema=ThreadState,
        checkpointer=InMemorySaver(),
    )
    usage = _lead_collector()
    journal = _journal(usage)

    async def scenario() -> None:
        async for _item in parent.astream(
            {"messages": [HumanMessage("go")]},
            config={"configurable": {"thread_id": "t1"}, "callbacks": [usage, journal]},
            stream_mode=["values"],
        ):
            pass

    try:
        asyncio.run(scenario())
    finally:
        subagents.shutdown_isolated_subagent_loop()

    totals = usage.totals()
    assert totals.subagent_tokens == 8, "子代理的一次模型调用（3 + 5）"
    assert totals.lead_agent_tokens == 16, "lead 两次调用——子代理的消耗不许混进来"
    assert (totals.total_input_tokens, totals.total_output_tokens) == (9, 15)
    assert totals.llm_call_count == 3, "lead 两次 + 子代理一次（继承的话会是 4）"
    assert totals.token_usage_by_model["deepseek-reasoner"]["input"] == 3, "§4.3 解析出的子代理模型"
    assert totals.token_usage_by_model[UNKNOWN_MODEL]["output"] == 10, "两次 lead 调用（假模型不报名字）"
