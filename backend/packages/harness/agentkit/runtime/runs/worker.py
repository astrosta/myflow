"""
run_agent
"""

from __future__ import annotations

import asyncio
import logging
import inspect
from typing import Any, AsyncIterable, Callable, Mapping, Awaitable
from dataclasses import asdict, dataclass


from langgraph.types import Overwrite
from langgraph.checkpoint.base import BaseCheckpointSaver

from agentkit.agents.thread_state import get_thread_state_schema
from agentkit.runtime.context import RunContext
from agentkit.runtime.events.journal import RunJournal
from agentkit.runtime.runs.manager import RunManager, RunRecord
from agentkit.runtime.runs.schemas import RunStartOutcome, RunStatus
from agentkit.runtime.runs.usage import UsageCollector
from agentkit.runtime.stream_bridge.base import StreamBridge
from agentkit.runtime.stream_modes import to_langgraph_stream_modes
from agentkit.runtime.task import spawn_detached
from agentkit.runtime.user_context import AUTO
from agentkit.agents.leader_agent import unwrap_agent_graph
from agentkit.agents.leader_agent.runtime_keys import CLIENT_IDENTITY_KEYS
from agentkit.trace_context import TRACE_METADATA_KEY

logger = logging.getLogger(__name__)
STREAM_RETENTION_SECONDS = 60.0      # END 之后桥缓冲再留多久，给晚到的 /join 续传
# thread metadata存储超时时间
THREAD_METADATA_SETUP_TIMEOUT_SECONDS = 5.0
# 身份和 trace 属于这一轮 run，不抄进跨 run 的 thread 行。
_RESERVED_METADATA_KEYS = CLIENT_IDENTITY_KEYS | {TRACE_METADATA_KEY}
AgentFactory = Callable[..., Any]

class RunAborted(Exception):
    def __init__(self, action: str) -> None:
        super().__init__(action)
        self.action = action

async def run_after_metadata(
    record: RunRecord,
    run_ctx: RunContext,
    *,
    bridge: StreamBridge,
    run_manager: RunManager,
    owner_user_id: str | None,
    require_existing_thread: bool,
) -> None:
    """worker task 的入口：尽力补 thread 行，然后把控制权交给 run_agent"""
    interrupted: asyncio.CancelledError | None = None
    try:
        failure = await _ensure_thread_metadata_bounded(
            record, run_ctx.thread_store, owner_user_id=owner_user_id, require_existing=require_existing_thread,
        )
    except asyncio.CancelledError as exc:
        interrupted, failure = exc, None
        record.abort_event.set()

    if failure is not None:
        if require_existing_thread:
            await run_manager.fail_start_if_pending(record.run_id, error=f"Thread verification failed: {failure!r}")
        else:
            logger.warning("Failed to ensure thread_meta for %s (non-fatal): %s", record.run_id, failure)
    
    await run_agent(bridge, run_manager, record, run_ctx)
    if interrupted is not None:
        raise interrupted


def _log_detached_setup_result(task: asyncio.Task) -> None:
    """Read a setup task we stopped waiting for, so its failure is logged instead of lost."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("Detached thread metadata setup failed", exc_info=error)


async def _ensure_thread_metadata_bounded(record, thread_store, *, owner_user_id, require_existing) -> BaseException | None:
    """最多等 5 秒，abort 一到就放手。返回 None 表示就绪或已被 abort；否则返回失败原因。"""
    setup = asyncio.create_task(
        _ensure_thread_metadata(thread_store, record, owner_user_id=owner_user_id, require_existing=require_existing)
    )
    aborted = asyncio.create_task(record.abort_event.wait())
    try:
        done, _ =await asyncio.wait(
            {setup, aborted}, timeout=THREAD_METADATA_SETUP_TIMEOUT_SECONDS, return_when = asyncio.FIRST_COMPLETED
        )
    finally:
        aborted.cancel()
        if not setup.done():
            setup.cancel()
            setup.add_done_callback(_log_detached_setup_result)
    
    if setup in done:
        return None if setup.cancelled() else setup.exception()
    if aborted in done:
        return None
    return TimeoutError(f"thread metadate setup exceeded {THREAD_METADATA_SETUP_TIMEOUT_SECONDS} seconds")

async def _ensure_thread_metadata(thread_store, record, *, owner_user_id, require_existing) -> None:
    """threads_meta 缺了才 INSERT。失败一律抛出，由 _ensure_thread_metadata_bounded 收成返回值。"""
    # 后台 task 上没有 HTTP 的 ContextVar（M4 §3.3），owner 必须显式传；传不出则退回 AUTO，
    # 由 resolve_user_id 抛 MissingUserContextError —— 读不到用户就失败，绝不写成全局行。
    user_id = owner_user_id if owner_user_id is not None else AUTO
    if await thread_store.get(record.thread_id, user_id=user_id) is not None:
        return

    if require_existing:
        raise LookupError(f"Thread {record.thread_id} was deleted during run admission")

    await thread_store.create(
        record.thread_id,
        assistant_id=record.assistant_id,
        user_id=user_id,
        metadata={k: v for k, v in record.metadata.items() if k not in _RESERVED_METADATA_KEYS},
    )

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
    journal = _open_journal(ctx, record)
    deadline: asyncio.Timeout | None = None
    timeout_s: float | None = None

    try:
        await run_manager.wait_for_prior_finalizing(thread_id, run_id, abort_event=record.abort_event)

        if await run_manager.try_start(run_id) is RunStartOutcome.cancelled:
            return

        await ctx.thread_store.update_status(thread_id, "running", user_id=record.user_id)
        rollback = await capture_rollback_point(ctx, thread_id)
        await bridge.publish(run_id, "metadata", {"run_id": run_id, "thread_id": thread_id})

        assembly = _call_agent_factory(ctx.agent_factory, ctx.run_config, app_config=ctx.app_config,checkpointer=ctx.checkpointer)
        graph = unwrap_agent_graph(assembly)
        timeout_s = ctx.app_config.runs.timeout_seconds if ctx.app_config is not None else None
        deadline = asyncio.timeout(timeout_s) #开始计时
        async with deadline:
            await _stream_graph(bridge, record, ctx, graph, usage, journal) #每一帧都查abort
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
                journal=journal,
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
    journal: RunJournal | None = None,
) -> None:
    run_id, thread_id = record.run_id, record.thread_id

    if journal is not None:
        await _best_effort("journal flush", journal.flush())

    if record.abort_event.is_set() and record.abort_action == "rollback":
        await _best_effort("rollback", restore_rollback_point(ctx, rollback))

    #M4
    if record.ownership_lost:
        return

    await _best_effort("title fallback", _fallback_thread_title(run_manager, record, ctx))

    await ctx.run_store.update_run_completion(
        run_id,
        status=getattr(terminal, "value", terminal),
        error=error,
        **asdict(usage.totals()),
    )

    await _best_effort("complete", run_manager.complete(run_id, status=terminal, error=error, totals=usage.totals()))
    final = record.status

    if final in (RunStatus.error, RunStatus.timeout):
        await _best_effort("error frame", bridge.publish(run_id, "error", {
            "error": error_type or "RunError",
            "message": record.error or final.value,
        }))

    if not await run_manager.has_later_run(thread_id, run_id):
        await _best_effort(
            "thread status",
            ctx.thread_store.update_status(thread_id, _thread_status(final), user_id=record.user_id),
        )
    #M6

async def _fallback_thread_title(run_manager: RunManager, record: RunRecord, ctx: RunContext | None) -> None:
    """Persist a local title when the first turn never reached after_model."""
    if ctx is None or await run_manager.later_run_is_running(record.thread_id, record.run_id):
        return
    from agentkit.agents.middlewares.title_middleware import fallback_title_from_state
    from agentkit.config.title_config import TitleConfig

    channels = await _checkpoint_channels(ctx, record.thread_id)
    existing = channels.get("title") if isinstance(channels, Mapping) else None
    if isinstance(existing, str) and existing.strip():
        await ctx.thread_store.update_display_name(record.thread_id, existing, user_id=record.user_id)
        return

    source: Mapping[str, Any] = {}
    if isinstance(channels, Mapping) and channels.get("messages"):
        source = {
            "messages": channels.get("messages"),
            "title": existing,
            "uploaded_files": channels.get("uploaded_files"),
        }
    elif isinstance(ctx.graph_input, Mapping):
        source = ctx.graph_input
    if not source:
        return

    title_config = ctx.app_config.title if ctx.app_config is not None else TitleConfig()
    result = fallback_title_from_state(source, title_config=title_config)
    title = result.get("title") if isinstance(result, Mapping) else None
    if not isinstance(title, str) or not title.strip():
        return

    head = await _checkpoint_head(ctx, record.thread_id)
    if head is not None and getattr(head, "config", None):
        mutation = build_state_mutation_graph(
            "title_fallback",
            checkpointer=ctx.checkpointer,
            state_schema=get_thread_state_schema(),
        )
        await mutation.aupdate_state(head.config, {"title": title}, as_node="title_fallback")
    await ctx.thread_store.update_display_name(record.thread_id, title, user_id=record.user_id)


async def _checkpoint_head(ctx: RunContext, thread_id: str) -> Any:
    return await ctx.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})


async def _checkpoint_channels(ctx: RunContext, thread_id: str) -> Mapping[str, Any] | None:
    head = await _checkpoint_head(ctx, thread_id)
    if head is None:
        return None
    checkpoint = head.checkpoint if isinstance(head.checkpoint, Mapping) else {}
    channels = checkpoint.get("channel_values")
    return channels if isinstance(channels, Mapping) else None


async def _best_effort(step: str, awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("Finalize step %r failed", step)

def _thread_status(status: RunStatus) -> str:
    return "error" if status in (RunStatus.error, RunStatus.timeout) else "idle"



def _open_journal(ctx: RunContext, record: RunRecord) -> RunJournal | None:
    """One ledger producer per run. Token totals stay on UsageCollector."""
    if ctx.event_store is None:
        return None
    settings = getattr(ctx.app_config, "run_events", None)
    if settings is None:
        return RunJournal(ctx.event_store, record)
    return RunJournal(
        ctx.event_store,
        record,
        batch_size=settings.batch_size,
        flush_interval=settings.flush_interval,
        track_token_usage=settings.track_token_usage,
        max_trace_content=settings.max_trace_content,
    )


async def _stream_graph(
    bridge: StreamBridge,
    record: RunRecord,
    ctx: RunContext,
    graph: Any,
    usage: UsageCollector,
    journal: RunJournal | None,
) -> None:
    lg_modes = to_langgraph_stream_modes(ctx.stream_modes)
    callbacks = [*(ctx.run_config.get("callbacks") or ()), usage]
    if journal is not None:
        callbacks.append(journal)
    config = {**ctx.run_config, "callbacks": callbacks}
    configurable = dict(config.get("configurable") or {})
    configurable.setdefault("thread_id", record.thread_id)
    config["configurable"] = configurable
    context = dict(config.pop("context", None) or {})
    context.setdefault("thread_id", record.thread_id)
    context.setdefault("run_id", record.run_id)

    stream = graph.astream(
        ctx.graph_input,
        config=config,
        context=context,
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

async def capture_rollback_point(ctx: RunContext, thread_id: str) -> RollbackPoint | None:
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