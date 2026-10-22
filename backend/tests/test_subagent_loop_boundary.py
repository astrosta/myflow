"""M8 §7.1 / §7.2 / §7.5：隔离循环、ContextVar 边界与反向的用量交付。

子代理跑在一个**进程级的持久事件循环**上。它要继承父 run 的 checkpoint 血缘、流回调和
身份，但**不能**继承父 run 的账本（``RunJournal`` 持有父循环上的任务和 SQL 连接池）。

三个方向都要钉：摘多了，子代理的帧发不出去；摘少了，账目重复、甚至
``Future attached to a different loop``；用量从隔离循环写回父账本则会**静默**丢 token。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextvars import Context, ContextVar
from types import SimpleNamespace

import pytest
from langchain_core.callbacks import BaseCallbackManager, CallbackManager
from langchain_core.runnables.config import var_child_runnable_config

from agentkit.subagents import executor as ex
from agentkit.subagents.results import SubagentResult, SubagentStatus
from fakes.task_tool_harness import FakeUsageRecorder

MARKER: ContextVar[str] = ContextVar("boundary_marker", default="unset")

#: 子代理的答复原文：它出现在父账本里就说明"计账"漏了（§7.2）。
SUBAGENT_REPLY = "SUBAGENT-REPLY-XYZ"


class _PlainHandler:
    """普通回调：必须活下来。"""


class _LoopBoundHandler:
    """绑定父循环的回调（M4 的 RunJournal 就是这个形状）。"""

    myflow_loop_bound = True


@pytest.fixture(autouse=True)
def _fresh_loop():
    yield
    ex.shutdown_isolated_subagent_loop()


@pytest.fixture
def inherited_config():
    """在测试函数的 context 里放一份父 run 的 RunnableConfig，用完复位。"""

    def _set(callbacks):
        return var_child_runnable_config.set({"configurable": {"thread_id": "thread-1"}, "callbacks": callbacks})

    tokens: list = []
    yield _set, tokens
    for token in reversed(tokens):
        var_child_runnable_config.reset(token)


# ---------------------------------------------------------------- §7.2 拷贝规则


def test_bound_handlers_are_stripped_from_a_plain_list(inherited_config) -> None:
    set_config, tokens = inherited_config
    keep = _PlainHandler()
    tokens.append(set_config([keep, _LoopBoundHandler()]))

    cfg = ex.copy_isolated_subagent_context().get(var_child_runnable_config)

    assert cfg["callbacks"] == [keep]
    assert cfg["configurable"] == {"thread_id": "thread-1"}


def test_bound_handlers_are_stripped_from_a_callback_manager(inherited_config) -> None:
    set_config, tokens = inherited_config
    manager = CallbackManager(handlers=[_PlainHandler(), _LoopBoundHandler()])
    manager.inheritable_handlers = [_LoopBoundHandler(), _PlainHandler()]
    tokens.append(set_config(manager))

    cfg = ex.copy_isolated_subagent_context().get(var_child_runnable_config)
    isolated = cfg["callbacks"]

    assert isinstance(isolated, BaseCallbackManager)
    assert [type(handler) for handler in isolated.handlers] == [_PlainHandler]
    assert [type(handler) for handler in isolated.inheritable_handlers] == [_PlainHandler]
    assert manager.handlers and len(manager.handlers) == 2, "拷贝不能改到父 run 的那份"


def test_callbacks_are_dropped_when_every_handler_is_bound(inherited_config) -> None:
    set_config, tokens = inherited_config
    tokens.append(set_config([_LoopBoundHandler()]))

    cfg = ex.copy_isolated_subagent_context().get(var_child_runnable_config)

    assert "callbacks" not in cfg
    assert cfg["configurable"] == {"thread_id": "thread-1"}, "只摘回调，configurable 要留着"


def test_a_single_non_bound_handler_is_kept_as_is(inherited_config) -> None:
    set_config, tokens = inherited_config
    keep = _PlainHandler()
    tokens.append(set_config(keep))

    cfg = ex.copy_isolated_subagent_context().get(var_child_runnable_config)

    assert cfg["callbacks"] is keep


def test_a_single_bound_handler_is_dropped(inherited_config) -> None:
    set_config, tokens = inherited_config
    tokens.append(set_config(_LoopBoundHandler()))

    cfg = ex.copy_isolated_subagent_context().get(var_child_runnable_config)

    assert "callbacks" not in cfg


def test_no_inherited_config_leaves_other_contextvars_alone() -> None:
    token = MARKER.set("kept")
    try:
        context = ex.copy_isolated_subagent_context()
        assert context.get(MARKER) == "kept"
        assert context.get(var_child_runnable_config) is None
    finally:
        MARKER.reset(token)


# ---------------------------------------------------------------- §7.5 反向边界


def test_usage_is_delivered_back_to_the_parent_loop() -> None:
    """§7.5：账本累加器是无锁读-改-写，只能由**父循环**写——所以是 call_soon_threadsafe。"""
    recorder = FakeUsageRecorder()
    records = [{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}]

    async def scenario() -> None:
        parent_loop = asyncio.get_running_loop()
        ex._deliver_final_usage_report(records, report_loop=parent_loop, recorder=recorder)
        await asyncio.sleep(0)  # 让回调在父循环上跑掉

    asyncio.run(scenario())

    assert recorder.calls == [records]


def test_usage_is_dropped_when_the_parent_loop_is_already_closed(caplog: pytest.LogCaptureFixture) -> None:
    """父循环已经拆掉（同步 asyncio.run）时故意丢弃：run 已经把 completion data 存下来了。"""
    recorder = FakeUsageRecorder()
    closed = asyncio.new_event_loop()
    closed.close()

    with caplog.at_level(logging.INFO, logger="agentkit.subagents.executor"):
        ex._deliver_final_usage_report(
            [{"total_tokens": 5}], report_loop=closed, recorder=recorder
        )

    assert recorder.calls == []
    assert "parent loop closed" in caplog.text


def test_usage_is_dropped_without_a_recorder(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="agentkit.subagents.executor"):
        ex._deliver_final_usage_report([{"total_tokens": 5}], report_loop=None, recorder=None)
    assert "no usage recorder" in caplog.text


def test_the_usage_report_is_claimed_only_once() -> None:
    """§7.5：``task`` 工具和隔离循环的兜底交付只能有一个真正上报。"""
    holder = SubagentResult(task_id="e1", trace_id="", status=SubagentStatus.RUNNING)

    assert holder.claim_usage_report() is True
    assert holder.claim_usage_report() is False


# ---------------------------------------------------------------- §7.2 无重复计账


def test_the_real_journal_is_marked_as_loop_bound() -> None:
    """加载期断言：RunJournal 必须带这个标记，否则下面那条端到端测试保护的是空气。"""
    from agentkit.persistence.events.memory import MemoryRunEventStore
    from agentkit.runtime.events.journal import RunJournal

    journal = RunJournal(
        MemoryRunEventStore(),
        SimpleNamespace(thread_id="t1", run_id="r1", user_id="u1"),
    )
    assert journal.myflow_loop_bound is True


def test_the_parent_journal_only_records_the_lead(monkeypatch) -> None:
    """§7.2「无重复计账」的端到端版：父 run 的 RunJournal 不许看见子代理的模型调用。

    没打标记时它会看见——子代理的 AI / Tool 消息随后被记进**父 run** 的消息账本，
    刷新页面后用户的主会话里就会冒出子代理的中间消息（§3.1 的第一个"绝不"）。
    """
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from agentkit.agents.thread_state import ThreadState
    from agentkit.config import AppConfig
    from agentkit.persistence.events.memory import MemoryRunEventStore
    from agentkit.runtime.events.journal import RunJournal
    from agentkit.subagents import executor as subagents
    from agentkit.tools.builtin.task_tool import make_task_tool
    from fakes.chat_model import FakeChatModel, ScriptedChatModel

    app_config = AppConfig.model_validate(
        {"models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]}
    )
    monkeypatch.setattr(subagents, "create_chat_model", lambda **kwargs: FakeChatModel(reply=SUBAGENT_REPLY))
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
                        {"name": "task", "id": "call_1", "args": {"prompt": "go", "subagent_type": "general-purpose"}}
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
    store = MemoryRunEventStore()
    journal = RunJournal(
        store,
        SimpleNamespace(
            thread_id="t1", run_id="r1", user_id="u1", abort_event=threading.Event()
        ),
    )

    async def scenario() -> list[dict]:
        async for _item in parent.astream(
            {"messages": [HumanMessage("go")]},
            config={"configurable": {"thread_id": "t1"}, "callbacks": [journal]},
            stream_mode=["values"],
        ):
            pass
        return await store.list_events("t1", "r1")

    rows = asyncio.run(scenario())
    ai_rows = [row for row in rows if row["event_type"] == "llm.ai.response"]

    assert len(ai_rows) == 2, "lead 的两次模型调用（发起委派 + 最终答复）"
    assert all(SUBAGENT_REPLY not in json.dumps(row["content"], ensure_ascii=False) for row in ai_rows)

# ---------------------------------------------------------------- §7.1 提交与执行


async def _read_inherited() -> tuple[str, str | None]:
    await asyncio.sleep(0)  # 让 Task 真的在隔离循环上跑起来
    cfg = var_child_runnable_config.get() or {}
    return MARKER.get(), (cfg.get("configurable") or {}).get("thread_id")


async def _thread_name() -> str:
    await asyncio.sleep(0)
    return threading.current_thread().name


def test_the_copied_context_reaches_the_isolated_loop(inherited_config) -> None:
    _, tokens = inherited_config
    tokens.append(var_child_runnable_config.set({"configurable": {"thread_id": "thread-1"}}))
    token = MARKER.set("parent-value")
    try:
        context = ex.copy_isolated_subagent_context()
        loop = ex.get_isolated_subagent_loop()
        future = ex.submit_to_isolated_loop(_read_inherited(), loop, context=context)
        assert future.result(timeout=5) == ("parent-value", "thread-1")
    finally:
        MARKER.reset(token)


def test_a_blank_context_would_lose_the_inherited_config() -> None:
    """负例：空白 Context 看上去"更干净"，其实把 namespace 和流回调一起丢了。"""
    token = var_child_runnable_config.set({"configurable": {"thread_id": "thread-1"}})
    marker = MARKER.set("parent-value")
    try:
        loop = ex.get_isolated_subagent_loop()
        future = ex.submit_to_isolated_loop(_read_inherited(), loop, context=Context())
        assert future.result(timeout=5) == ("unset", None)
    finally:
        MARKER.reset(marker)
        var_child_runnable_config.reset(token)


def test_the_isolated_loop_is_a_separate_thread_and_gets_reused() -> None:
    loop = ex.get_isolated_subagent_loop()
    assert ex.get_isolated_subagent_loop() is loop

    first = ex.submit_to_isolated_loop(_thread_name(), loop).result(timeout=5)
    second = ex.submit_to_isolated_loop(_thread_name(), loop).result(timeout=5)

    assert first == second == "subagent-isolated-loop"
    assert first != threading.current_thread().name


def test_a_failed_submission_closes_the_orphan_coroutine() -> None:
    """§7.1：loop 在 lookup 和 schedule 之间关掉时，不许留下从未 await 的协程。"""
    closed = asyncio.new_event_loop()
    closed.close()
    coro = _thread_name()

    with pytest.raises(RuntimeError):
        ex.submit_to_isolated_loop(coro, closed)

    assert coro.cr_frame is None, "未启动的协程必须被 close()，否则是 RuntimeWarning + 对象泄漏"


def test_shutdown_stops_the_thread_and_a_later_call_starts_a_new_loop() -> None:
    first = ex.get_isolated_subagent_loop()
    thread = ex._isolated_thread
    assert thread is not None

    ex.shutdown_isolated_subagent_loop()

    assert not thread.is_alive(), "atexit 要真的把线程停下来"
    assert first.is_closed()

    second = ex.get_isolated_subagent_loop()
    assert second is not first and not second.is_closed()


def test_shutdown_without_a_loop_is_a_no_op() -> None:
    ex.shutdown_isolated_subagent_loop()
    ex.shutdown_isolated_subagent_loop()
