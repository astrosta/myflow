"""The only regular producer of the run-event ledger (M4 §5.7).

Token totals stay on `UsageCollector` and `update_run_progress`: this class
never accumulates usage, never writes a token, never changes run status. It only
turns LangChain callbacks into the six-field envelope of §4.3.

Callback → event lives in exactly one place: §5.7 of the design doc, implemented
by the methods below. Do not re-derive that mapping anywhere else.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agentkit.runtime.events.catalog import (
    LLM_AI_RESPONSE,
    LLM_ERROR_EVENT,
    LLM_HUMAN_INPUT,
    LLM_TOOL_RESULT,
    RUN_END_EVENT,
    RUN_ERROR_EVENT,
    RUN_START_EVENT,
    RunEventDefinition,
)
from agentkit.runtime.events.retrieval import (
    RETRIEVAL_TOOL_EVENTS,
    TOOL_SEARCH_TOOL_NAME,
    retrieval_event_for_tool,
)
from agentkit.persistence.events.base import RunEventStore
from agentkit.persistence.events.codec import dumps, limit_trace

logger = logging.getLogger(__name__)

MIDDLEWARE_TAG_PREFIX = "middleware:"
LEAD_CALLER = "lead"
ERROR_TEXT_MAX_CHARS = 2000


class _PendingLLM:
    """A response that arrived without usage. Held until superseded, or committed at flush."""

    __slots__ = ("run_id", "envelopes")

    def __init__(self, run_id: str, envelopes: list[dict[str, Any]]) -> None:
        self.run_id = run_id
        self.envelopes = envelopes


class RunJournal(AsyncCallbackHandler):
    """Buffer ledger rows and flush them as one `put_batch` (§5.7).

    Flush when the buffer reaches `batch_size`, when the root chain ends or
    errors, and when the worker calls `flush` in `finally`. `on_llm_new_token`
    is intentionally not implemented: tokens travel on the SSE and progress pipes.
    """

    run_inline = True

    def __init__(
        self,
        store: RunEventStore,
        record: Any,
        *,
        batch_size: int = 20,
        flush_interval: float = 5.0,
        track_token_usage: bool = True,
        max_trace_content: int = 10240,
        usage_sink: Any | None = None,
    ) -> None:
        super().__init__()
        # M8 §7.2：这个 handler 绑定在**父循环**上（持有父循环的任务和连接池），子代理跑在
        # 隔离循环上，所以拷贝 ContextVar 进隔离循环时必须把它摘掉——否则子代理的 LLM/Tool
        # 回调会被记进**父 run** 的消息账本，刷新页面后用户的主会话里就会冒出子代理的中间
        # 消息。标记名与 ``subagents.executor.LOOP_BOUND_MARKER`` 是同一个字符串。
        self.myflow_loop_bound = True
        self._store = store
        self._record = record
        #: §15：子代理用量的**转发**目标（父 run 的 ``UsageCollector``）。本类仍然
        #: "never accumulates usage"（模块 docstring）：累加只有一处，就是那个累加器。
        self.usage_sink = usage_sink
        self._batch_size = batch_size
        self.flush_interval = flush_interval
        self.track_token_usage = track_token_usage
        self._max_trace_content = max_trace_content
        self._buf: list[dict[str, Any]] = []
        # 同一条 LangChain run_id 只写一次：流式提供商会先回一次空 usage，再回放一次带 usage 的
        self._written_llm: set[str] = set()
        self._pending_llm: _PendingLLM | None = None
        # 消息身份去重：同一条消息会在不同回调里被重复交出（回放、对账）
        self._message_identities: set[str] = set()
        # 检索证据的去重身份：on_tool_end 的回放与 _reconcile_messages 会交出同一次工具调用
        self._retrieval_identities: set[str] = set()
        self._tool_names: dict[str, str] = {}
        self._llm_started: dict[str, float] = {}
        self._llm_index: dict[str, int] = {}
        self._llm_calls = 0

    # ---- chain ----

    async def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is not None:
            return  # 节点级开始不写：行数翻倍而信息量近零
        await self._put(
            RUN_START_EVENT,
            content={"chain": (serialized or {}).get("name", "unknown")},
            metadata={"caller": self._caller(tags)},
        )

    async def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is not None:
            return
        await self._reconcile_messages(outputs)  # 漏回调的最后一道保险
        await self._put(RUN_END_EVENT, content=outputs, metadata={"status": "success"})
        await self.flush()

    async def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        await self._put(
            RUN_ERROR_EVENT,
            content=_error_text(error),
            metadata={"error_type": type(error).__name__},
        )
        await self.flush()  # 错误路径最不能丢

    # ---- llm ----

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """chat 模型的主路径。文本补全模型不走这里，走 on_llm_start。"""
        index = self._begin_llm(run_id)
        if _is_middleware(tags):
            return  # 中间件自己的调用不产生用户消息
        for message in _flatten_messages(messages):
            if not isinstance(message, HumanMessage):
                continue
            if self._mark_message(message):
                await self._put(
                    LLM_HUMAN_INPUT,
                    content=message.model_dump(),
                    metadata={"caller": self._caller(tags), "llm_call_index": index},
                )

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """仅文本补全模型（BaseLLM）触发。不写事件，只记开始时间给 latency_ms。"""
        self._begin_llm(run_id)

    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        key = _key(run_id)
        if key in self._written_llm:
            return  # 流式回放：同一 run_id 只写一次，写两次就是两条 AI 消息
        usage = _extract_usage(response)
        envelopes = self._ai_envelopes(response, run_id=key, tags=tags, usage=usage)
        if not envelopes:
            return
        if usage is None:
            # 第一次空 usage：整份挂起。直接写掉会让带 usage 的那份变成第二条消息
            self._pending_llm = _PendingLLM(key, envelopes)
            return
        self._written_llm.add(key)
        self._commit_pending_llm(except_run_id=key)  # 挂起的那份被这一份取代
        await self._emit(envelopes)

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        key = _key(run_id)
        self._written_llm.add(key)
        await self._put(
            LLM_ERROR_EVENT,
            content=_error_text(error),
            metadata={"error_type": type(error).__name__, "llm_call_index": self._index_for(key)},
        )

    # ---- tool ----

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """不写事件：参数已经在前一条 AI 消息的 tool_calls 里。只记 id → name。"""
        tool_call_id = _tool_call_id(metadata, inputs, kwargs)
        if tool_call_id:
            self._tool_names[tool_call_id] = str((serialized or {}).get("name") or "")

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if not isinstance(output, ToolMessage):
            return
        if self._mark_message(output):
            await self._put(
                LLM_TOOL_RESULT,
                content=output.model_dump(),
                metadata={
                    "tool_name": self._tool_name_for(output),
                    "status": getattr(output, "status", None),
                },
            )
        # 回放也走派生证据：llm.tool.result 由消息身份去重，retrieval 由自己的身份去重。
        await self._emit_retrieval(output)

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """不写：LangChain 会把工具异常变成一条 ToolMessage，走 on_tool_end。"""

    # ---- 缓冲与落库 ----

    async def flush(self) -> None:
        """worker 的 finally 也必须调它。失败时整批放回队首再抛。"""
        self._commit_pending_llm()
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        try:
            await self._store.put_batch(batch)  # seq / created_at / user_id 归 store（§5.6）
        except Exception:
            self._buf = batch + self._buf  # 放回队首：下一次 flush 重试
            raise

    # ---- §15 子代理用量（本类只转发，不累加）----

    def record_external_llm_usage_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        """把隔离循环报回来的**子代理**用量转给父 run 的累加器（M8 §15）。

        ``task`` 工具在父循环里按**方法名**找"父 run 的用量记录器"（§9），而它手上最稳定的
        把手就是账本，所以这个入口留在本类上。注意 worker 组装的 callback 顺序是
        ``[..., usage, journal]``：正常路径解析到的是 ``UsageCollector`` 本身，走到这里的
        是"只挂了 journal"的调用方（单测 / 嵌入）。两条路都汇到同一个累加器，**不会记两遍**
        ——``SubagentResult.claim_usage_report`` 保证一次委派只交付一次（§7.5）。

        本方法必须是**同步**且只在父循环上跑的：累加器的字段是无锁的读-改-写（§7.5）。
        """
        sink = self.usage_sink
        if sink is None:
            logger.debug(
                "no usage sink attached; dropping %d subagent usage record(s)", len(records or ())
            )
            return
        sink.record_external_llm_usage_records(records)

    async def _put(
        self,
        definition: RunEventDefinition,
        *,
        content: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._commit_pending_llm()  # 挂起的那份先入缓冲，seq 顺序才是对的
        await self._emit([self._envelope(definition, content=content, metadata=metadata)])

    async def _emit(self, envelopes: list[dict[str, Any]]) -> None:
        self._buf.extend(envelopes)
        if len(self._buf) >= self._batch_size:
            await self.flush()

    def _envelope(
        self,
        definition: RunEventDefinition,
        *,
        content: Any,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # 判定顺序是"先入缓冲、后截断"：截断按 category 走，消息类绝不截断（§5.6）
        content, metadata = limit_trace(content, metadata, definition.category, self._max_trace_content)
        return {
            "thread_id": self._record.thread_id,
            "run_id": self._record.run_id,
            "user_id": self._record.user_id,  # 后台写没有 ContextVar，必须显式传
            "event_type": definition.event_type,
            "category": definition.category,  # ★ 来自 catalog
            "content": content,
            "metadata": metadata,
        }

    def _commit_pending_llm(self, *, except_run_id: str | None = None) -> None:
        """挂起的那份入缓冲。一直没等到带 usage 的第二份时，内容不能丢（§5.7）。

        提交即视为这个 run_id 已经写过：迟到的那份回放再打进来，宁可丢 usage
        也不能在同一条 thread 上留下两条一样的 AI 消息（§5.7 的去重规则）。
        """
        pending, self._pending_llm = self._pending_llm, None
        if pending is None or pending.run_id == except_run_id:
            return
        self._written_llm.add(pending.run_id)
        self._buf.extend(pending.envelopes)

    def _ai_envelopes(
        self,
        response: Any,
        *,
        run_id: str,
        tags: Sequence[str] | None,
        usage: dict[str, int] | None,
    ) -> list[dict[str, Any]]:
        """每条 generation 的 AI 消息一行。usage / latency 只进 metadata（§5.6）。"""
        metadata: dict[str, Any] = {
            "caller": self._caller(tags),
            "llm_call_index": self._index_for(run_id),
        }
        latency = self._latency_ms(run_id)
        if latency is not None:
            metadata["latency_ms"] = latency
        envelopes: list[dict[str, Any]] = []
        for message in _ai_messages(response):
            if _message_id(message):
                self._mark_message(message)  # 对账时不再补写同一条
            row_metadata = dict(metadata)
            row_usage = _usage_of(message) or usage  # 一行的 usage 优先用它自己那条 generation 的
            if row_usage is not None and self.track_token_usage:
                row_metadata["usage"] = row_usage
            envelopes.append(
                self._envelope(LLM_AI_RESPONSE, content=message.model_dump(), metadata=row_metadata)
            )
        return envelopes

    async def _reconcile_messages(self, outputs: Any) -> None:
        """on_chain_end 的最后一道保险：补上被取消 / 覆盖而没触发回调的消息。

        Tool / Human 一定要补；AI 只在带 message.id 时补，避免与 on_llm_end 双写（§5.7）。
        """
        for message in _messages_of(outputs):
            if isinstance(message, AIMessage) and not _message_id(message):
                continue  # 没有 id 就不补，避免与 on_llm_end 双写（§5.7）
            if isinstance(message, ToolMessage):
                if self._mark_message(message):
                    await self._put(LLM_TOOL_RESULT, content=message.model_dump())
                # 对账可能交出同一工具调用的新实例：工具结果被消息身份挡住，
                # 派生证据仍必须经过自己的身份去重，而不是依赖消息身份的副作用。
                await self._emit_retrieval(message)
                continue
            if not self._mark_message(message):
                continue
            if isinstance(message, HumanMessage):
                await self._put(LLM_HUMAN_INPUT, content=message.model_dump())
            elif isinstance(message, AIMessage):
                await self._put(LLM_AI_RESPONSE, content=message.model_dump())

    async def _emit_retrieval(self, message: ToolMessage) -> None:
        """tool_search 的结果在 llm.tool.result 之后再补一行 retrieval.tool。

        只写 tool_search：另两个检索工具（skill / kb）目前不由父循环产出。
        归一化器返回 None（坏 JSON、检索不可用）时什么都不写——证据只能来自真正
        发生过的检索，"没检索"不能被记成"检索到 0 条"。

        去重按 tool_call_id（没有 ID 时按内容摘要）：同一次工具调用会在 on_tool_end
        的回放和对账里被交出多份，派生证据只该有一行。
        """
        tool_name = self._tool_name_for(message)
        if tool_name != TOOL_SEARCH_TOOL_NAME:
            return
        identity = _retrieval_identity(message)
        if identity in self._retrieval_identities:
            return
        event = retrieval_event_for_tool(tool_name=tool_name, content=message.content)
        if event is None:
            return
        self._retrieval_identities.add(identity)
        await self._put(
            RETRIEVAL_TOOL_EVENTS[tool_name],
            content=event.content,
            metadata={"tool_name": tool_name},
        )

    # ---- 归因与去重 ----

    @staticmethod
    def _caller(tags: Sequence[str] | None) -> str:
        """M4 只有 lead；M6 的中间件调用带 middleware:{tag}，照抄进 metadata。"""
        for tag in tags or ():
            if isinstance(tag, str) and tag.startswith(MIDDLEWARE_TAG_PREFIX):
                return tag
        return LEAD_CALLER

    def _tool_name_for(self, message: ToolMessage) -> str | None:
        """工具名：消息自己带的优先，否则用 on_tool_start 记下的映射。"""
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        return getattr(message, "name", None) or self._tool_names.get(tool_call_id)

    def _mark_message(self, message: BaseMessage) -> bool:
        """True 表示这条消息还是第一次见到，可以写。"""
        identity = _message_identity(message)
        if identity is None:
            return True
        if identity in self._message_identities:
            return False
        self._message_identities.add(identity)
        return True

    def _begin_llm(self, run_id: UUID | None) -> int:
        key = _key(run_id)
        self._llm_started[key] = time.monotonic()
        return self._index_for(key)

    def _index_for(self, key: str) -> int:
        """on_llm_end / on_llm_error 与 on_chat_model_start 共用同一个 llm_call_index。"""
        index = self._llm_index.get(key)
        if index is None:
            index = self._llm_calls
            self._llm_calls += 1
            self._llm_index[key] = index
        return index

    def _latency_ms(self, key: str) -> int | None:
        started = self._llm_started.pop(key, None)
        if started is None:
            return None
        return int((time.monotonic() - started) * 1000)


def _message_id(message: BaseMessage) -> str | None:
    message_id = getattr(message, "id", None)
    return message_id if isinstance(message_id, str) and message_id else None


def _message_identity(message: BaseMessage) -> str | None:
    """消息身份：工具结果优先用 tool_call_id，其余用 LangChain 的 message.id。"""
    tool_call_id = getattr(message, "tool_call_id", None)
    if isinstance(tool_call_id, str) and tool_call_id:
        return f"tool:{tool_call_id}"
    message_id = _message_id(message)
    if message_id:
        return f"message:{message_id}"
    return None


def _retrieval_identity(message: ToolMessage) -> str:
    """检索证据的身份：优先 tool_call_id，没有 ID 时退回内容摘要。"""
    tool_call_id = getattr(message, "tool_call_id", None)
    if isinstance(tool_call_id, str) and tool_call_id:
        return f"tool:{tool_call_id}"
    return "content:" + hashlib.sha256(dumps(message.content).encode("utf-8")).hexdigest()


def _flatten_messages(messages: Any) -> list[BaseMessage]:
    if isinstance(messages, BaseMessage):
        return [messages]
    if isinstance(messages, (list, tuple)):
        flat: list[BaseMessage] = []
        for item in messages:
            flat.extend(_flatten_messages(item))
        return flat
    return []


def _messages_of(outputs: Any) -> list[BaseMessage]:
    if isinstance(outputs, Mapping):
        return _flatten_messages(outputs.get("messages"))
    return _flatten_messages(outputs)


def _ai_messages(response: Any) -> list[BaseMessage]:
    """每条 generation 的 AI 消息。没有 message 的 generation 不产生行。"""
    messages: list[BaseMessage] = []
    for group in getattr(response, "generations", None) or ():
        for generation in group:
            message = getattr(generation, "message", None)
            if isinstance(message, BaseMessage) and not isinstance(message, ToolMessage):
                messages.append(message)
    return messages


def _normalize_usage(meta: Mapping[str, Any]) -> dict[str, int]:
    details = meta.get("input_token_details") or {}
    return {
        "input_tokens": int(meta.get("input_tokens") or 0),
        "output_tokens": int(meta.get("output_tokens") or 0),
        "cache_read_tokens": int(details.get("cache_read") or 0) if isinstance(details, Mapping) else 0,
    }


def _usage_of(message: Any) -> dict[str, int] | None:
    """这一条 generation 自己带的 usage。没有就是没有，不要拿别人的凑。"""
    meta = dict(getattr(message, "usage_metadata", None) or {})
    return _normalize_usage(meta) if meta else None


def _extract_usage(response: Any) -> dict[str, int] | None:
    """与 UsageCollector 同口径：优先 message.usage_metadata，其次 llm_output。

    全 0 视为"这次没带 usage"——流式提供商的第一份响应就是这样，要挂起而不是写掉。
    """
    input_tokens = output_tokens = cache_read = 0
    for group in getattr(response, "generations", None) or ():
        for generation in group:
            message = getattr(generation, "message", None)
            meta = dict(getattr(message, "usage_metadata", None) or {})
            if not meta:
                llm_output = getattr(response, "llm_output", None) or {}
                meta = dict(llm_output.get("token_usage") or llm_output.get("usage") or {})
                if "prompt_tokens" in meta and "input_tokens" not in meta:
                    meta["input_tokens"] = meta["prompt_tokens"]
                if "completion_tokens" in meta and "output_tokens" not in meta:
                    meta["output_tokens"] = meta["completion_tokens"]
            row = _normalize_usage(meta)
            input_tokens += row["input_tokens"]
            output_tokens += row["output_tokens"]
            cache_read += row["cache_read_tokens"]
    if input_tokens <= 0 and output_tokens <= 0 and cache_read <= 0:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
    }


def _tool_call_id(
    metadata: Mapping[str, Any] | None,
    inputs: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
) -> str | None:
    for source in (metadata, inputs, kwargs):
        if not isinstance(source, Mapping):
            continue
        value = source.get("tool_call_id")
        if isinstance(value, str) and value:
            return value
    return None


def _is_middleware(tags: Sequence[str] | None) -> bool:
    return any(isinstance(tag, str) and tag.startswith(MIDDLEWARE_TAG_PREFIX) for tag in (tags or ()))


def _error_text(error: BaseException) -> str:
    message = str(error).strip() or type(error).__name__
    return f"{type(error).__name__}: {message}"[:ERROR_TEXT_MAX_CHARS]


def _key(run_id: UUID | None) -> str:
    return str(run_id) if run_id is not None else ""
