"""子代理执行器与隔离循环（M8 §7）。

M8 的实现难度集中在**一条边界**上：子代理跑在独立的事件循环里，而它需要继承父 run 的
checkpoint 血缘、流回调和身份，同时又**不能**继承父 run 的账本、checkpointer 和消息通道。

三条去程边界：

1. **ContextVar 拷贝，但要摘掉绑定父循环的回调**（§7.2）。``RunJournal`` 持有父循环上的
   任务和一个 SQL 连接池，跨循环会重复计账、甚至 ``Future attached to a different loop``。
   但也不能图省事传一个空白 ``Context``：继承的 checkpoint namespace 和框架的流处理
   回调是"子代理的帧发得出去"的前提。
2. **子配置不许重新指定 checkpoint 坐标**（§7.3）：``thread_id`` / ``checkpoint_ns`` /
   ``checkpoint_id`` / ``checkpoint_map``。**显式提供（哪怕值完全一样）会开启一条新的
   根血缘**，子代理就失去非根的子图 namespace，它的帧会被路由进父 run 的 ``messages`` 流。
3. **子代理图用 ``checkpointer=False`` 编译**（§7.4）：它一次性、永不 resume，继承父
   checkpointer 只会把中间状态写进父 thread。

外加一条回程边界（§7.5）：最终用量必须用提交时捕获的**父循环** ``call_soon_threadsafe``
交回去——账本的累加器是无锁的读-改-写字段，从隔离循环写它会静默丢 token。

跑过这一节之后，判定标准只剩一句话：**子代理运行期间，用户的会话视图不被它的局部状态
覆盖；父 run 取消之后，注册表条目归零。**（第二条的沙箱租约部分在 §11/D7。）
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import suppress
from contextvars import Context, copy_context
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackManager
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.errors import GraphRecursionError

from agentkit.agents.leader_agent.runtime_keys import CHECKPOINT_COORDINATE_KEYS
from agentkit.agents.middlewares.assembly import build_subagent_runtime_middlewares
from agentkit.agents.thread_state import ThreadState
from agentkit.config import AppConfig
from agentkit.knowledge.citations import collect_known_citations, resolve_citations
from agentkit.models import create_chat_model
from agentkit.observability import attach_callback_handler, start_subagent_trace
from agentkit.sandbox.lease import release_sandbox_lease, subagent_lease_owner_id
from agentkit.sandbox.security import safe_app_config
from agentkit.subagents.capacity import AdmissionDenied, SubagentExecutionCapacity
from agentkit.subagents.registry import filter_tools
from agentkit.subagents.results import SubagentResult, SubagentStatus, attach_future, cleanup
from agentkit.subagents.step_events import (
    capture_new_retrieval_events,
    capture_new_step_messages,
)
from agentkit.subagents.token_collector import SubagentTokenCollector, attach_token_collector
from agentkit.utils.messages import message_content_to_text

if TYPE_CHECKING:  # 只在类型检查期 import：执行器不认识具体的调度器实现
    from agentkit.tools.builtin.task_tool import SubagentDispatch

logger = logging.getLogger(__name__)

#: 需要摘掉的回调处理器的标记（M8 起统一 ``myflow_`` 前缀；对照设计文档时注意这处改过名）。
LOOP_BOUND_MARKER = "myflow_loop_bound"

#: M6 的 LLM 错误降级标记（§13.2）。**只有标记是权威的**，散文不算。
ERROR_FALLBACK_KEY = "myflow_error_fallback"

#: 没有自有 system_prompt 的类型（``custom_agents`` 没写）拿到这句静态角色提示。
DEFAULT_SUBAGENT_SYSTEM_PROMPT = (
    "你是一个子代理。lead 给了你一个自包含任务，你在自己的上下文中执行："
    "lead 只看得到你的最终回答。你不能向用户提问，也不能继续委派。"
    "自己完成任务，报告结论而不是过程。"
)

def _subagent_tools(dispatch: "SubagentDispatch") -> list[Any]:
    """按类型声明求交；`delegated_tools` 只给点名要它的类型。

    `tools=None` 是"继承 lead 当时的工具"（general-purpose）。lead 已经不再持有检索
    工具（2026-11-02 检索子代理设计），所以这类子代理**不能**顺手拿到 delegated 工具，
    否则"检索只走专用子代理"就形同虚设。类型显式列出工具名时，才从 delegated 里补。
    """

    config = dispatch.config
    tools = filter_tools(dispatch.tools, config.tools, config.disallowed_tools)
    if config.tools is None:
        return tools
    delegated = filter_tools(
        dispatch.delegated_tools, config.tools, config.disallowed_tools
    )
    existing = {tool.name for tool in tools}
    return [*tools, *(tool for tool in delegated if tool.name not in existing)]


_LOOP_START_TIMEOUT_SECONDS = 5.0
_LOOP_STOP_TIMEOUT_SECONDS = 1.0
_ISOLATED_THREAD_NAME = "subagent-isolated-loop"


# ---------------------------------------------------------------- §7.1 隔离循环


_isolated_loop: asyncio.AbstractEventLoop | None = None
_isolated_thread: threading.Thread | None = None
_isolated_lock = threading.Lock()


def _run_loop(loop: asyncio.AbstractEventLoop, started: threading.Event) -> None:
    asyncio.set_event_loop(loop)
    loop.call_soon(started.set)
    try:
        loop.run_forever()
    finally:
        started.clear()


def get_isolated_subagent_loop() -> asyncio.AbstractEventLoop:
    """懒启动的进程级隔离循环（daemon 线程）。

    子代理是一个完整 agent 图，会跑几十上百轮。放在父 run 的循环上意味着它的每一次
    ``await`` 都和父 run 的流式推送、心跳、SSE 消费抢同一个循环；更麻烦的是父 run 结束时
    循环会被拆掉，而子代理的清理逻辑还需要跑。
    """
    global _isolated_loop, _isolated_thread
    with _isolated_lock:
        if _isolated_loop is not None and not _isolated_loop.is_closed():
            return _isolated_loop
        loop = asyncio.new_event_loop()
        started = threading.Event()
        thread = threading.Thread(
            target=_run_loop,
            args=(loop, started),
            name=_ISOLATED_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        if not started.wait(_LOOP_START_TIMEOUT_SECONDS):
            logger.error("the isolated subagent loop did not start in %ss", _LOOP_START_TIMEOUT_SECONDS)
            raise RuntimeError("the isolated subagent loop did not start")
        _isolated_loop = loop
        _isolated_thread = thread
        return loop


def submit_to_isolated_loop(
    coro: Any,
    loop: asyncio.AbstractEventLoop,
    *,
    context: Context | None = None,
) -> Future:
    """把协程提交到**已经拿到**的隔离循环上。

    顺序是硬要求（§7.1）：先 ``get_isolated_subagent_loop()``，再创建协程。写成
    ``run_coroutine_threadsafe(factory(), loop)`` 时 Python 先求值 ``factory()``，
    loop 启动失败就会留下一个从未 await 的协程（``RuntimeWarning`` + 对象泄漏）。
    提交失败时这里对**已创建、未启动**的协程调 ``close()``。

    ``context`` 的生效方式：``call_soon_threadsafe`` 捕获**调用这一刻**的 context，
    回调里创建 Task 时再复制它——所以 ``context.run(...)`` 包一层就够了（§7.2）。
    """
    try:
        if context is not None:
            return context.run(asyncio.run_coroutine_threadsafe, coro, loop)
        return asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:
        coro.close()
        raise


def run_on_isolated_subagent_loop(coro: Any, *, context: Context | None = None) -> Future:
    """懒启动版本：给清理任务用。

    父循环的 ``create_task`` 会随 ``asyncio.run()`` 一起被取消，在那里排队等于没排（§7.5）。
    """
    return submit_to_isolated_loop(coro, get_isolated_subagent_loop(), context=context)


def shutdown_isolated_subagent_loop() -> None:
    """atexit：``stop`` → join(1s) → **确认停了**才 close。

    在仍 running 的 loop 上 close() 会留下半死的执行器和一堆
    "Event loop is closed" 噪音；宁可打一条 warning 让它跟着进程一起走。
    """
    global _isolated_loop, _isolated_thread
    with _isolated_lock:
        loop, thread = _isolated_loop, _isolated_thread
        _isolated_loop = None
        _isolated_thread = None
    if loop is None:
        return
    if not loop.is_closed():
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread.is_alive():
        thread.join(_LOOP_STOP_TIMEOUT_SECONDS)
    if thread is not None and thread.is_alive():
        logger.warning("the isolated subagent loop is still running; refusing to close it")
        return
    if not loop.is_running() and not loop.is_closed():
        loop.close()


atexit.register(shutdown_isolated_subagent_loop)


# ---------------------------------------------------------------- §7.2 ContextVar 拷贝


def copy_isolated_subagent_context() -> Context:
    """拷贝当前环境，但摘掉**绑定父循环**的回调处理器。

    典型的绑定者是 M4 的 ``RunJournal``（构造时给自己打 ``myflow_loop_bound = True``）：
    它持有父循环上的任务和 SQL 连接池，跟着子代理跨循环会重复计账并炸
    ``Future attached to a different loop``。用**显式标记**而不是类名黑名单：以后换实现
    不用改这里。

    注意**不能**返回空白 ``Context``——继承的 checkpoint namespace、身份、追踪上下文和
    框架自己的流处理回调都在里面，丢了它们子代理的帧就发不出去（§3.1 那个契约的前提）。
    """
    context = copy_context()
    inherited = context.get(var_child_runnable_config)
    if inherited is None or "callbacks" not in inherited:
        return context
    callbacks = inherited.get("callbacks")
    if isinstance(callbacks, BaseCallbackManager):
        isolated = callbacks.copy()
        isolated.handlers = [
            handler for handler in callbacks.handlers if not getattr(handler, LOOP_BOUND_MARKER, False)
        ]
        isolated.inheritable_handlers = [
            handler
            for handler in callbacks.inheritable_handlers
            if not getattr(handler, LOOP_BOUND_MARKER, False)
        ]
    elif isinstance(callbacks, (list, tuple)):
        isolated = [handler for handler in callbacks if not getattr(handler, LOOP_BOUND_MARKER, False)]
    elif getattr(callbacks, LOOP_BOUND_MARKER, False):
        isolated = None
    else:
        isolated = callbacks
    cfg = dict(inherited)
    if isolated:
        cfg["callbacks"] = isolated
    else:
        cfg.pop("callbacks", None)
    context.run(var_child_runnable_config.set, cfg)
    return context


# ---------------------------------------------------------------- §7.3 子配置


def recursion_limit_for(agent: Any, max_turns: int) -> int:
    """``max_turns`` 次模型调用所需的 LangGraph ``recursion_limit``。

    ``recursion_limit`` 数的是图的超步，中间件的每个钩子都是一个节点：``before_agent`` /
    ``after_agent`` 每次 run 各跑一次，``before_model`` / ``after_model``、``model``、``tools``
    每轮各跑一次。把 ``max_turns`` 原样当 ``recursion_limit``，真实中间件链下子代理连一次
    工具调用都做不完；按节点换算，子代理链加减中间件时预算也不会悄悄缩水。
    """
    once = 0
    # model + tools。最后一轮不跑 tools，空出来的那一步正好是 LangGraph 要求的余量。
    per_turn = 2
    for name in getattr(agent, "nodes", None) or ():
        if name.endswith((".before_agent", ".after_agent")):
            once += 1
        elif name.endswith((".before_model", ".after_model")):
            per_turn += 1
    return once + per_turn * max(1, int(max_turns))


def build_child_config(
    parent_config: Mapping[str, Any] | None, *, recursion_limit: int
) -> dict[str, Any]:
    """子代理的 ``RunnableConfig``：省略 checkpoint 坐标键，其余原样继承。

    ✗ ``{"configurable": {"thread_id": parent_thread_id}}`` —— 哪怕值和父 run 完全一样，
    也会开启一条新的**根血缘**，子代理的 AI / tool 帧随后会被路由进父 run 的
    ``messages`` 流（用户会看到自己的对话历史被子代理的局部状态覆盖）。

    隐藏的 LangGraph 运行时键（``__pregel_checkpoint_ns`` 等）**保留**：血缘就是靠它们传的。
    业务组件要 ``thread_id`` 请从 ``runtime.context`` 拿（§7.3）。
    """
    configurable: dict[str, Any] = {}
    if isinstance(parent_config, Mapping):
        raw = parent_config.get("configurable")
        if isinstance(raw, Mapping):
            configurable = {
                key: value for key, value in raw.items() if key not in CHECKPOINT_COORDINATE_KEYS
            }
    return {"configurable": configurable, "recursion_limit": recursion_limit}


# ---------------------------------------------------------------- §7.5 回程：用量


def _deliver_final_usage_report(
    records: list[dict],
    *,
    report_loop: asyncio.AbstractEventLoop | None,
    recorder: Any | None,
) -> None:
    """把最终用量交回**父循环**（§7.5）。

    账本的累加器是无锁的读-改-写字段：从隔离循环或 ``to_thread`` 里写它会**静默**丢更新，
    所以这里只做一件事——``call_soon_threadsafe``。父循环已经拆掉（同步 ``asyncio.run``）
    时故意丢弃并打 info：run 已经把 completion data 持久化了，没人会再读这些计数器。
    """
    if recorder is None:
        logger.debug("no usage recorder; dropping %d subagent usage records", len(records))
        return
    if report_loop is None or report_loop.is_closed():
        logger.info("drop subagent usage: parent loop closed (n=%d)", len(records))
        return
    try:
        report_loop.call_soon_threadsafe(recorder.record_external_llm_usage_records, records)
    except RuntimeError:
        logger.info("drop subagent usage: parent loop refused the callback (n=%d)", len(records))


# ---------------------------------------------------------------- 执行器


@dataclass
class _RunState:
    """一次执行的本地状态。只在隔离循环上读写，不需要锁。"""

    last_state: Mapping[str, Any] | None = None
    middlewares: list[Any] = field(default_factory=list)
    processed_count: int = 0
    seen_messages: set[str] = field(default_factory=set)
    #: 检索证据自己的游标：它和步骤共用新增尾部，但读原始 ToolMessage，不截断。
    retrieval_processed_count: int = 0


class SubagentExecutor:
    """§7 的执行器，同时实现 §9 的 ``SubagentDispatcher``。

    ``start()`` 必须**同步**返回：工具提交完就去轮询 holder，等在这里就一条
    ``task_running`` 都发不出去（§9.1 第 8 步）。
    """

    def __init__(
        self,
        *,
        app_config: AppConfig | None = None,
        lazy_init: bool = True,
        capacity: SubagentExecutionCapacity | None = None,
    ) -> None:
        self._app_config = app_config
        self._lazy_init = lazy_init
        #: §10.2 的准入控制器。由 §10.1 的 ``SubagentRuntime`` 注入**同一个**实例——
        #: 每次 new 一个就是 N 套各 3 槽的控制器，上限形同虚设。None = 不过准入（测试/嵌入）。
        self._capacity = capacity

    # ---- §9 的接缝 ---------------------------------------------------------

    def start(self, dispatch: SubagentDispatch) -> None:
        context = copy_isolated_subagent_context()
        report_loop = _running_loop()
        loop = get_isolated_subagent_loop()  # ① 先拿 loop（失败就没有孤儿协程）
        coro = self._aexecute(dispatch, report_loop=report_loop)  # ② 再创建协程
        future = submit_to_isolated_loop(coro, loop, context=context)
        attach_future(dispatch.holder.task_id, future)

    # ---- 隔离循环上的执行 ---------------------------------------------------

    async def _aexecute(
        self,
        dispatch: SubagentDispatch,
        *,
        report_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """隔离循环上的入口：**先过准入，再跑模型**（§10.2）。"""
        if self._capacity is None:
            await self._aexecute_admitted(dispatch, report_loop=report_loop)
            return
        try:
            async with self._capacity.slot():
                await self._aexecute_admitted(dispatch, report_loop=report_loop)
        except AdmissionDenied as denied:
            # 队列满 / 排队超时 / reject：发生在跑模型之前，所以没有 token 消耗，
            # 也没有"成功委派"语义——但台账仍要记一条 failed，免得模型原地重试把队列打满。
            logger.info("subagent admission denied (%s): %s", denied.reason, denied)
            dispatch.holder.try_set_terminal(
                SubagentStatus.FAILED,
                error=(
                    f"子代理槽位已满（{denied.reason}）：不要立即重试同一任务，先自己完成，或稍后再委派"
                ),
                admission_failure=True,
            )
            await self._finish(dispatch, report_loop=report_loop)

    async def _aexecute_admitted(
        self,
        dispatch: SubagentDispatch,
        *,
        report_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """拿到槽位之后的执行。**绝不把异常留给调用方**：失败也要终态化 holder（§13）。"""
        run = _RunState()
        dispatch.holder.mark_running()
        try:
            run.middlewares = self._build_middlewares(dispatch)
            await asyncio.wait_for(
                self._astream(run, dispatch), timeout=dispatch.config.timeout_seconds
            )
        except TimeoutError:
            # §17：子代理墙钟到点。已经拿到的部分结果照样带回去。
            self._terminalize(dispatch, run, SubagentStatus.TIMED_OUT)
        except GraphRecursionError:
            # §13.1：这一条必须 catch 在通用 except **之前**，否则 lead 看不到 turn_capped
            self._terminalize(dispatch, run, None, stop_reason="turn_capped")
        except asyncio.CancelledError:
            self._terminalize(dispatch, run, SubagentStatus.CANCELLED)
            raise
        except Exception as exc:
            logger.exception("subagent execution failed (execution_id=%s)", dispatch.holder.task_id)
            dispatch.holder.try_set_terminal(
                SubagentStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            # 收尾钉在隔离循环上：轮询器可能早就退出了（轮询预算耗尽 / 父 run 取消）。
            await self._finish(dispatch, report_loop=report_loop)

    async def _astream(self, run: _RunState, dispatch: SubagentDispatch) -> None:
        agent = self._build_agent(dispatch, run.middlewares)
        state = self._build_initial_state(dispatch)
        run.last_state = state
        parent_config = var_child_runnable_config.get()
        config = build_child_config(
            parent_config,
            recursion_limit=recursion_limit_for(agent, dispatch.config.max_turns),
        )
        # §15：子代理自己的用量采集器挂在这条子 run 的 callbacks 上（**追加**，不覆盖继承来的
        # 处理器——§3.1 的帧发布依赖它们）。它把累计快照写进 holder，父循环的 task 工具再交付。
        config = attach_token_collector(
            config, self._make_token_collector(dispatch), inherited=parent_config
        )
        subagent_trace = start_subagent_trace(
            trace_id=dispatch.holder.trace_id or None,
            parent_span_id=getattr(dispatch, "trace_parent_span_id", None),
            name=dispatch.config.name,
            execution_id=dispatch.holder.task_id,
            tool_call_id=dispatch.holder.external_task_id,
            user_id=dispatch.user_id,
            thread_id=dispatch.thread_id,
            model_name=dispatch.model_name,
        )
        if subagent_trace.handler is not None:
            try:
                config = attach_callback_handler(config, subagent_trace.handler)
            except Exception:  # noqa: BLE001 - tracing must not fail the subagent
                logger.warning("Failed to attach Langfuse handler to subagent", exc_info=True)
        try:
            async for chunk in agent.astream(
                state,
                config=config,
                context=self._child_context(dispatch),
                stream_mode="values",
            ):
                run.last_state = chunk
                # §3.2 第 6 步：**先发布最新证据**，再看有没有人喊停。反过来的话，取消刚好卡在
                # stream 边界时，lead 拿不到已经发生的工具输出。
                self._capture_steps(run, dispatch.holder, chunk)
                if dispatch.holder.cancel_event.is_set():
                    self._terminalize(dispatch, run, SubagentStatus.CANCELLED)
                    subagent_trace.finish(status="cancelled")
                    return
            self._terminalize(dispatch, run, None)
            if dispatch.holder.status is SubagentStatus.COMPLETED:
                subagent_trace.finish(status="success")
            else:
                subagent_trace.finish(
                    status="error", error=dispatch.holder.error or "subagent failed"
                )
        except asyncio.CancelledError:
            subagent_trace.finish(status="cancelled")
            raise
        except Exception as exc:
            subagent_trace.finish(
                status="error", error=f"{type(exc).__name__}: {exc}"
            )
            raise

    async def _finish(
        self, dispatch: SubagentDispatch, *, report_loop: asyncio.AbstractEventLoop | None
    ) -> None:
        """排空在途沙箱调用 → 释放租约 → 用量交回父循环 → 摘掉注册表条目（§7.5 / §11.3–4）。

        闭包只持有"解析好的记录器 + 几个 id + 捕获的父循环"：清理任务可能活到整个轮询预算
        结束，通过 runtime 它会把父 run 的账本和事件存储钉住那么久。
        """
        holder = dispatch.holder
        # §11.3 + §11.4：排空在途沙箱调用 → 幂等释放租约，两步合成一个入口。
        # 顺序是有语义的（反了会让在途调用打到已经关掉的沙箱上），所以不许在这里拆开写；
        # 中间件的正常路径会先摘一次持有，这里不是最后持有者时就什么都不做。
        with suppress(Exception):
            await release_sandbox_lease(subagent_lease_owner_id(holder.task_id))
        records = [dict(record) for record in holder.token_usage_records]
        if records and holder.claim_usage_report():
            _deliver_final_usage_report(
                records, report_loop=report_loop, recorder=dispatch.usage_recorder
            )
        cleanup(holder.task_id)

    # ---- 组装 --------------------------------------------------------------

    def _resolve_app_config(self) -> AppConfig:
        config = self._app_config or safe_app_config()
        if config is None:
            raise RuntimeError("application config is unavailable")
        return config

    def _make_token_collector(self, dispatch: SubagentDispatch) -> SubagentTokenCollector:
        """§15：一次委派一个实例。**不打** ``myflow_loop_bound``——它就该在隔离循环上跑（§7.2）。"""
        return SubagentTokenCollector(
            dispatch.holder,
            subagent_name=dispatch.config.name,
            model_name=dispatch.model_name,
        )

    def _build_middlewares(self, dispatch: SubagentDispatch) -> list[Any]:
        """§12.1 的子代理链：没有上传、没有标题、没有 ``task``、没有澄清；日期照常注入。

        ``skip_memory_flush=True``、护栏 trio 与 SystemMessage 合并是 D9 的事；
        每份 task 都 new 一套实例，所以并行子代理的护栏状态不会串号（§12.5）。
        """
        return list(
            build_subagent_runtime_middlewares(
                self._resolve_app_config(),
                model_name=dispatch.model_name,
                lazy_init=self._lazy_init,
            )
        )

    def _build_agent(self, dispatch: SubagentDispatch, middlewares: list[Any]) -> Any:
        """§7.4：``checkpointer=False`` + ``system_prompt=None``。

        ``system_prompt=None`` 是故意的：initial state 已经有一条框架 SystemMessage，
        再让 ``create_agent`` 插一条，严格后端会 400。
        """
        model = create_chat_model(
            name=dispatch.model_name,
            app_config=self._resolve_app_config(),
            attach_tracing=False,
        )
        tools = _subagent_tools(dispatch)
        return create_agent(
            model=model,
            tools=tools,
            middleware=middlewares,
            system_prompt=None,
            state_schema=ThreadState,
            checkpointer=False,
        )

    def _build_initial_state(self, dispatch: SubagentDispatch) -> dict[str, Any]:
        """§12.4：每次 task 都是一份**新鲜** ThreadState（``promoted`` 从空开始）。"""
        system_prompt = dispatch.config.system_prompt or DEFAULT_SUBAGENT_SYSTEM_PROMPT
        state: dict[str, Any] = {
            "messages": [SystemMessage(content=system_prompt), HumanMessage(content=dispatch.prompt)]
        }
        if dispatch.uploaded_files is not None:
            # §9.1 第 4 步的快照；None 表示缺失/畸形 → 不放进去（fail closed）
            state["uploaded_files"] = deepcopy(dispatch.uploaded_files)
        return state

    @staticmethod
    def _child_context(dispatch: SubagentDispatch) -> dict[str, Any]:
        """业务组件（沙箱、归属统计、中间件）都从这里拿身份，**不从 RunnableConfig**（§7.3）。"""
        holder = dispatch.holder
        # §11.1：租约 id 派生自 execution_id（不是 provider 的 tool_call_id）。沙箱中间件与
        # bash 工具都从这个 context 取它，三处必须是**同一个**字符串。
        lease_owner = subagent_lease_owner_id(holder.task_id)
        return {
            "thread_id": dispatch.thread_id,
            "run_id": dispatch.run_id,
            "user_id": dispatch.user_id,
            "subagent_name": dispatch.config.name,
            "subagent_execution_id": holder.task_id,
            "subagent_tool_call_id": holder.external_task_id,
            "sandbox_lease_owner_id": lease_owner,
            "sandbox_command_scope_id": lease_owner,
        }

    # ---- 终态化 ------------------------------------------------------------

    def _terminalize(
        self,
        dispatch: SubagentDispatch,
        run: _RunState,
        status: SubagentStatus | None,
        *,
        stop_reason: str | None = None,
    ) -> None:
        """把执行推向终态。护栏命中**不改状态**，只额外带一个 ``stop_reason``（§13.1）。"""
        holder = dispatch.holder
        result = extract_final_result(run.last_state)
        citations = extract_citations(run.last_state, result)
        reason = stop_reason or self._consume_stop_reason(dispatch, run)

        error: str | None = None
        if status is None:  # 图正常跑完（或轮次到顶）：有结论才算完成
            status = SubagentStatus.COMPLETED if result else SubagentStatus.FAILED
        if _has_error_fallback(run.last_state):
            # §13.2：图干净地结束 ≠ 子代理成功。provider 异常被中间件兜成了一条消息。
            status = SubagentStatus.FAILED
            error = "模型调用失败，中间件已将其降级为一条消息"
        if status is SubagentStatus.COMPLETED and not result:
            status = SubagentStatus.FAILED
        if status is SubagentStatus.FAILED and error is None:
            error = "子代理在轮次预算内没有产出结论"
        holder.try_set_terminal(
            status, result=result, error=error, stop_reason=reason, citations=citations
        )

    @staticmethod
    def _consume_stop_reason(dispatch: SubagentDispatch, run: _RunState) -> str | None:
        """§12.5 的 duck typing：执行器不 import ``TokenBudget`` / ``LoopDetection``。

        以后加一条护栏轴，这里一行都不用改——只要那个中间件有 ``consume_stop_reason``。
        """
        for middleware in run.middlewares:
            consume = getattr(middleware, "consume_stop_reason", None)
            if not callable(consume):
                continue
            reason = consume(dispatch.run_id)
            if reason:
                return str(reason)
        return None

    def _capture_steps(
        self, run: _RunState, holder: SubagentResult, state: Mapping[str, Any] | None
    ) -> None:
        """§14.1：把新出现的 AI / Tool 消息捕获进 ``holder.steps``。

        规则（新增的尾部、压缩收缩后的游标重置、id/内容去重）全在纯函数
        ``step_events.capture_new_step_messages`` 里——这里有图的循环，那里没有，
        所以单元测试不需要 Gateway。父循环的 ``task`` 工具每 0.25 秒读一次
        ``holder.pending_steps`` 并发一条 ``task_running``。
        """
        messages = state.get("messages") if isinstance(state, Mapping) else None
        if not isinstance(messages, list):
            return
        captured: list[dict[str, Any]] = []
        run.processed_count = capture_new_step_messages(
            messages, captured, run.seen_messages, run.processed_count
        )
        if captured:
            holder.append_steps(captured)

        # 检索证据走另一条游标：同一段新增尾部，但读**原始** ToolMessage.content。
        # 步骤投影仍然是 8192 字符（前端卡片）；这里不截断（评测要看完整正文）。
        retrievals, run.retrieval_processed_count = capture_new_retrieval_events(
            messages, run.retrieval_processed_count
        )
        if retrievals:
            holder.append_retrievals(retrievals)


# ---------------------------------------------------------------- 纯函数


def extract_final_result(state: Mapping[str, Any] | None) -> str | None:
    """最后一条**有文本**的 AI 消息，没有就是 None（调用方按"没可用结果"走 failed，§13.1）。"""
    messages = state.get("messages") if isinstance(state, Mapping) else None
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if getattr(message, "type", None) != "ai":
            continue
        text = message_content_to_text(getattr(message, "content", ""))
        if text.strip():
            return text
    return None


def extract_citations(
    state: Mapping[str, Any] | None, result_text: str | None
) -> list[dict[str, Any]]:
    """Resolve citation markers in the final answer against this subagent's tool results.

    Only sources that were actually retrieved in this run and actually named by a
    marker in the final answer survive. A marker invented by the model resolves
    to nothing rather than becoming a fake source in the lead's context.
    """

    if not result_text:
        return []
    messages = state.get("messages") if isinstance(state, Mapping) else None
    if not isinstance(messages, list):
        return []
    known = collect_known_citations(messages)
    return resolve_citations(result_text, known)


def _has_error_fallback(state: Mapping[str, Any] | None) -> bool:
    """最后一条 assistant 消息上有没有 M6 的错误降级标记（§13.2）。"""
    messages = state.get("messages") if isinstance(state, Mapping) else None
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if getattr(message, "type", None) != "ai":
            continue
        kwargs = getattr(message, "additional_kwargs", None)
        return bool(isinstance(kwargs, Mapping) and kwargs.get(ERROR_FALLBACK_KEY))
    return False


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """当前（父）循环。``start()`` 是在工具的协程里被调的，所以这里拿得到。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
