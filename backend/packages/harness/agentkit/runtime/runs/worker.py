"""
run_agent
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterable, Callable, Mapping

from langgraph.types import Overwrite

from agentkit.runtime.context import RunContext
from agentkit.runtime.runs.manager import RunManager, RunRecord
from agentkit.runtime.runs.schemas import RunStartOutcome, RunStatus
from agentkit.runtime.stream_bridge.base import StreamBridge
from agentkit.runtime.stream_modes import to_langgraph_stream_modes
from agentkit.runtime.task import spawn_detached

logger = logging.getLogger(__name__)
STREAM_RETENTION_SECONDS = 60.0      # END 之后桥缓冲再留多久，给晚到的 /join 续传
AgentFactory = Callable[..., Any]

class RunAborted(Exception):
    def __init__(self, action: str) -> None:
        super().__init__(action)
        self.action = action


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    ctx: RunContext,
) -> None:
    """一条run从启动到end的全过程，不返回值，终态写进store，事件写进bridge"""
    run_id, thread_id = record.run_id, record.thread_id
    terminal, error, error_type = RunStatus.interrupted, None, None #try_start 拒绝、提前return时的终态
    rollback: RollbackPoint | None = None 
    usage = UsageCollector(run_manager, run_id)
    deadline: asyncio.Timeout | None = None
    timeout_s: float | None = None

    try:
        await run_manager.wait_for_prior_finalizing(thread_id, run_id, abort_event=record.abort_event)

        if await run_manager.try_start(run_id) is RunStartOutcome.cancelled:
            return

        await ctx.thread_store.set_status(thread_id, "running")
        rollback = await capture_rollback_point(ctx, thread_id)
        await bridge.publish(run_id, "metadata", {"run_id": run_id, "thread_id": thread_id})
        
        _install_runtime(ctx.run_config, record)

        assembly = _call_agent_factory(ctx.agent_factory, ctx.run_config, app_config=ctx.app_config,checkpointer=ctx.checkpointer)
        graph = unwrap_agent_graph(assembly)
        timeout_s = ctx.app_config.runs.timeout_seconds if ctx.app_config is not None else None
        deadline = asyncio.timeout(timeout_s) #开始计时
        async with deadline:
            await _stream_graph(bridge, record, ctx, graph, usage) #每一帧都查abort
        terminal, error = RunStatus.success, None

    except RunAborted:
        terminal, error = RunStatus.interrupted, None
    except asyncio.CancelledError:
        terminal, error = RunStatus.interrupted, None
        raise
    except TimeoutError as exc:
        if deadline is not None and deadline.expired():
            terminal, error_type = RunStatus.timeout, "timeout"
            error = f"Run execeed {timeout_s} seconds"
        else:
            terminal, error, error_type=RunStatus.error, _public_error(exec), type(exc).__name__
    except Exception as exc:
        terminal, error, error_type = RunStatus.error, _public_error(exc), type(exc).__name__
        logger.exception("Run %s failed", run_id)

    finally:
        record.finalizing = True
        try:
            # 收尾可能很多：回滚、写终态、token 汇总、事件账本落盘（M4）、标题投影（M6）。
            # 每一步都可能抛，_finalize 自己逐步兜底（见下）。
            await _finalize(
                bridge, run_manager, record, ctx,
                terminal=terminal, error=error, error_type=error_type, rollback=rollback, usage=usage,
            )
        finally:
            #无论收尾是否成功，流必须结束
            await bridge.publish_end(run_id)
            record.finalizing = False
            run_manager.schedule_cleanup(run_id)
            spawn_detached(bridge.cleanup(run_id, delay=STREAM_RETENTION_SECONDS))


def _call_agent_factory(factory: AgentFactory, run_config:dict, *, app_config, checkpointer) -> Any:
    params = inspect.signature(factory).parameters
    kwargs: dict[str, Any] = {}
    if "app_config" in params:
        kwargs["app_config"] = app_config
    if "checkpointer" in params:
        kwargs["checkpointer"] = checkpointer
    return factory(run_config, **kwargs)


_PUBLIC_ERROR_MAX_CHARS = 2000

def _public_error(exc: BaseException) -> str:
    """会进 runs.error、error 帧，并被 API 回显：只要类型名和截断后的消息，不带 traceback。"""
    message = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {message[:_PUBLIC_ERROR_MAX_CHARS]}"

def _install_runtime(run_config: dict, record: RunRecord) -> None:
    """将thread_id和run_id注入到ToolRuntime里面"""
    from langgraph.runtime import Runtime

    context = dict(run_config.get("context") or {})
    context.setdefault("thread_id", record.thread_id)
    context.setdefault("run_id", record.run_id)
    run_config["context"] = context
    runtime = Runtime(context=context, store=None)
    run_config.setdefault("configurable", {})["__pregel_runtime"] = runtime


async def _finalize(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    ctx: RunContext,
    *,
    terminal: RunStatus,
    error: str | None,
    error_type: str | None,
    rollback: RollbackPoint | None,
    usage: UsageCollector,
) -> None:
    run_id, thread_id = record.run_id, record.thread_id

    if record.abort_event.is_set() and record.abort_action == "rollback":
        await _best_effort("rollback", restore_rollback_point(ctx, rollback))

    await _best_effort("complete", run_manager.complete(run_id, status=terminal, error=error, totals=usage.totals()))
    final = record.status

    if final in (RunStatus.error, RunStatus.timeout):
        await _best_effort("error frame", bridge.publish(run_id, "error", {
            "error": error_type or "RunError",
            "message": record.error or final.value,
        }))

    if not await run_manager.has_later_run(thread_id, run_id):
        await _best_effort("thread status", ctx.thread_store.set_status(thread_id, _thread_status(final)))

async def _best_effort(step: str, awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("Finalize step %r failed", step)

def _thread_status(status: RunStatus) -> str:
    return "error" if status in (RunStatus.error, RunStatus.timeout) else "idle"



async def _stream_graph(
    bridge: StreamBridge,
    record: RunRecord,
    ctx: RunContext,
    graph: Any,
    usage: UsageCollector,
) -> None:
    lg_modes = to_langgraph_stream_modes(ctx.stream_modes)
    callbacks = [*(ctx.run_config.get("callbacks") or ()), usage]
    config = {**ctx.run_config, "callbacks": callbacks}
    configurable = dict(config.get("configurable") or {})
    configurable.setdefault("thread_id", record.thread_id)
    config["configurable"] = configurable

    stream = graph.astream(
        ctx.graph_input,
        config=config,
        stream_mode=lg_modes,
        subgraphs=False,
    )
    try:
        async for item in stream:
            _raise_if_aborted(record)
            mode, chunk, namespace = unpack_stream_item(item)
            await bridge.publish(
                record.run_id,
                sse_event_name(mode, namespace),
                serialize(chunk, mode=mode),
            )
            await usage.maybe_report()
    finally:
        await _close_quitely(stream)

def _raise_if_aborted(record: RunRecord) -> None:
    if record.abort_event.is_set():
        raise RunAborted(record.abort_action)

def unpack_stream_item(item: Any) -> tuple[str, Any, tuple[str, ...]]:
    """把 LangGraph stream 的一条输出拆成 (mode, payload)。

    v2（``stream(version="v2")``）每条都是字典 ``{"type": mode, "ns": ..., "data": payload}``。
    mode 在 ``type`` 里，真正的状态或消息在 ``data`` 里。``messages`` 的 data 仍是
    ``(message, metadata)``，后面的 ``_unpack_message_chunk`` 再拆第二层。

    v1 多 mode 时是二元组 ``(mode, payload)``。只订一个 mode 时没有 mode 前缀，
    那条输出就是 payload 本身；LangGraph 默认 mode 是 values，所以按状态快照处理。
    """
    if isinstance(item, tuple) and len(item) == 3: #subgraph=True
        namespace, mode, chunk = item
        return mode, chunk, tuple(namespace)
    if isinstance(item, tuple) and len(item) == 2: #单mode v1
        mode, chunk = item
        return mode, chunk, ()
    return "values", item, () # 防御：不该走到。单 mode 字符串才会产裸 chunk

def unpack_stream_part(part: Mapping) -> tuple[str, Any, tuple[str, ...]]:
    return str(part["type"]), part["data"], tuple(part.get("ns") or ())

def sse_event_name(mode: str, namespace: tuple[str, ...]) -> str:
    return "|".join((mode, *namespace)) if namespace else mode

async def _close_quitely(stream: AsyncIterable[Any]) -> None:
    try:
        await stream.aclose()
    except Exception:
        logger.exception("Failed to close graph stream", exc_info=True)

def serialize(chunk: Any, *, mode: str) -> Any:
    if mode == "messages":
        message, metadata = chunk
        return [to_jsonable(message), to_jsonable(metadata) if isinstance(metadata, dict) else {}]
    if mode == "values" and isinstance(chunk, dict):
        return {k: to_jsonable(v) for k, v in chunk.items() if not k.startswith("__pregel_")}
    return to_jsonable(chunk)

def to_jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):                       # LangChain 消息、pydantic 模型
        return to_jsonable(obj.model_dump())
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    
    return str(obj)

@dataclass(frozen=True, slots=True)
class RollbackPoint:
    thread_id: str
    checkpoint_config: dict | None #运行前的head坐标
    messages: tuple[Any, ...] = () #运行前的messages，tuple类型可防止messages被修改
    pending_writes: tuple[tuple[str, str, Any], ...] = () #运行前的pending writes，taskid、channel、value

class RollbackUnavailable(RuntimeError):
    """运行前的状态没有捕获到，回滚无从做起。"""

def capture_rollback_point(ctx: RunContext, thread_id: str) -> RollbackPoint | None:
    """None 表示捕获失败：之后的 rollback 请求退化成 interrupt（fail-closed）。"""
    try:
        head = await ctx.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    except Exception:
        logger.warning("Rollback point capture failed for %s; rollback disabled for this run", thread_id, exc_info=True)
        return None

    if head is None:
        return RollbackPoint(thread_id = thread_id, checkpoint_config = None)

    checkpoint = head.checkpoint if isinstance(head.checkpoint, Mapping) else {}
    channels = checkpoint.get("channel_values")
    if not isinstance(channels, Mapping):
        logger.warning("Rollback point for %s has no channel_values; rollback disabled", thread_id)
        return None
    
    return RollbackPoint(
        thread_id = thread_id,
        checkpoint_config = head.config,
        messages = tuple(channels.get("messages", ())),
        pending_writes = tuple(head.pending_writes or ()),
    )
    
async def restore_rollback_point(ctx: RunContext, point: RollbackPoint | None) -> None:
    if point is None:
        raise RollbackUnavailable("pre-run state was not captured")
    if point.checkpoint_config is None:
        await ctx.checkpointer.adelete_thread(point.thread_id) #运行前是空thread
        return 
    
    mutation = build_state_mutation_graph(
        "rollback_store",
        checkpointer=ctx.checkpointer,
        state_schema=get_thread_state_schema(),
    )
    # 生成新的head和checkpoint_id
    restored = await mutation.aupdate_state(
        point.checkpoint_config,
        {"messages": Overwrite(list(point.messages))},
        as_node="rollback_restore",
    )
    for task_id, writes in _group_writes_by_task(point.pending_writes):
        await ctx.checkpointer.aput_writes(restored, writes, task_id=task_id)
    
def build_state_mutation_graph(
    node_name: str,
    *,
    checkpointer: BaseCheckpointSaver,
    state_schema: type | dict,
) -> Any:
    from langgraph.graph import StateGraph

    builder = StateGraph(state_schema)

    async def _noop(state: Any) -> Any:
        return {}
    
    builder.add_node(node_name, _noop)
    builder.set_entry_point(node_name)
    builder.set_finish_point(node_name)
    return builder.compile(checkpointer=checkpointer)

def _group_writes_by_task(
    pending_writes: tuple[tuple[str, str, Any], ...],
) -> list[tuple[str, list[tuple[str, Any]]]]:
    """ 按task_id分组，每个task_id对应一个list，list中每个元素是一个tuple，tuple中第一个元素是channel，第二个元素是value"""
    grouped: dict[str, list[tuple[str, Any]]] = {}

    for task_id, channel, value in pending_writes:
        grouped.setdefault(task_id, []).append((channel, value))
    
    return list(grouped.items())