"""Compact conversation history into summary_text. Do not put the summary back into messages."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware.summarization import (
    ContextSize,
    SummarizationMiddleware,
)
from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.messages.utils import get_buffer_string, trim_messages
from langgraph.constants import TAG_NOSTREAM
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from agentkit.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from agentkit.agents.middlewares.message_utils import is_real_user_message
from agentkit.agents.thread_state import get_thread_state_schema
from agentkit.config import AppConfig, get_app_config
from agentkit.config.summarization_config import ContextSize as ConfigContextSize
from agentkit.config.summarization_config import SummarizationConfig
from agentkit.models import create_chat_model
from agentkit.runtime.runs.manager import ConflictError, RunManager
from agentkit.runtime.runs.schemas import ThreadOperationKind
from agentkit.runtime.runs.worker import build_state_mutation_graph

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_PROMPT = """<role>
上下文提取助手
</role>

<primary_objective>
你的唯一目标是从下面的对话历史中提取质量最高、最相关的上下文。
</primary_objective>

<objective_information>
你即将达到可接受的输入 token 上限，因此必须从对话历史中提取最重要、最相关的信息。
这些上下文之后会替换下面的完整对话历史。请确保只保留继续完成总体目标所必需的信息。
</objective_information>

<instructions>
下面的对话历史会被你提取的上下文替换。你要确保不会重复已经完成的操作，
因此提取内容应聚焦于对总体目标最重要的信息。

请按以下章节组织摘要。每个章节都像检查清单：有内容就填写，没有内容就明确写“无”。

## 会话意图

用户的主要目标或请求是什么？你正在完成什么总体任务？要简洁，但足以理解整个会话的目的。

## 摘要

提取并记录对话历史中最重要的上下文，包括对话中做出的重要选择、结论或策略，
以及关键决策背后的理由。记录被否决的选项及其原因。

## 产物

本次对话创建、修改或访问了哪些产物、文件或资源？如果是文件修改，
请列出具体路径并简述改动。这个章节用于防止产物信息静默丢失。

## 后续步骤

为完成会话意图，还有哪些具体任务需要执行？下一步应该做什么？

</instructions>

用户会提供完整消息历史，你需要从中提取用于替换的上下文。请仔细通读，
认真判断哪些信息对总体目标最重要、最应该保留：

<messages>
需要总结的消息：
{messages}
</messages>

只返回提取出的上下文，不要添加任何额外信息，也不要在前后添加说明。
"""
_CANNED_SUMMARIES = frozenset({
    "没有更早的对话历史。",
    "之前的对话过长，无法生成摘要。",
})
_SUMMARY_TAGS = ["middleware:summarize", TAG_NOSTREAM]


class SummaryGenerationError(RuntimeError):
    """Manual compact failed to produce a summary."""


@dataclass(frozen=True, slots=True)
class ContextCompactionResult:
    summary_text: str
    messages_to_summarize: tuple
    preserved_messages: tuple
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CompactOutcome:
    thread_id: str
    compacted: bool
    summary_text: str | None = None
    removed_message_count: int = 0
    preserved_message_count: int = 0
    checkpoint_id: str | None = None
    total_tokens: int | None = None


def compact_http_error(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, ConflictError):
        return 409, "thread_busy"
    if isinstance(exc, SummaryGenerationError):
        return 500, "summarization_failed"
    raise exc


class MyFlowSummarizationMiddleware(SummarizationMiddleware):
    """Anchor model stays untagged. The summary copy carries nostream."""

    def __init__(
        self,
        model: Any,
        *,
        trigger: ContextSize | list[ContextSize] | None = None,
        keep: ContextSize = ("messages", 20),
        token_counter: Callable[[Iterable], int] | None = None,
        summary_prompt: str | None = None,
        trim_tokens_to_summarize: int | None = 4000,
        fallback_model: Any = None,
        app_config: AppConfig | None = None,
        run_model_name: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "trigger": trigger,
            "keep": keep,
            "summary_prompt": summary_prompt or DEFAULT_SUMMARY_PROMPT,
            "trim_tokens_to_summarize": trim_tokens_to_summarize,
        }
        if token_counter is not None:
            kwargs["token_counter"] = token_counter
        super().__init__(model, **kwargs)
        tagged = self.model.with_config(tags=list(_SUMMARY_TAGS))
        retry = getattr(tagged, "with_retry", None)
        self._summary_model = retry() if callable(retry) else tagged
        self._fallback_model = fallback_model
        self._app_config = app_config
        self._run_model_name = run_model_name
        self.before_summarization: list = []

    def _trim_messages_for_summary(self, messages):
        """Keep the tail. A human-only start drops a pure AI/tool window to empty."""
        if self.trim_tokens_to_summarize is None:
            return messages
        try:
            trimmed = trim_messages(
                messages,
                max_tokens=self.trim_tokens_to_summarize,
                token_counter=self.token_counter,
                start_on="human",
                strategy="last",
                allow_partial=True,
                include_system=True,
            )
            if trimmed:
                return list(trimmed)
            trimmed = trim_messages(
                messages,
                max_tokens=self.trim_tokens_to_summarize,
                token_counter=self.token_counter,
                strategy="last",
                allow_partial=True,
                include_system=True,
            )
            return list(trimmed) if trimmed else list(messages)
        except Exception:
            return list(messages)

    def _prepare_compaction(self, state: dict, *, force: bool):
        messages = list(state.get("messages") or [])
        self._ensure_message_ids(messages)
        previous_summary = state.get("summary_text")
        counted = list(messages)
        if isinstance(previous_summary, str) and previous_summary.strip():
            counted.append(HumanMessage(content=previous_summary, name="summary"))
        total_tokens = self.token_counter(counted)
        if not force and not self._should_summarize(counted, total_tokens):
            return None
        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None
        protected = _protected_ids(messages)
        to_summarize = [message for index, message in enumerate(messages) if index < cutoff_index and message.id not in protected]
        preserved = [message for index, message in enumerate(messages) if index >= cutoff_index or message.id in protected]
        if not to_summarize:
            return None
        return to_summarize, preserved, previous_summary if isinstance(previous_summary, str) else None, total_tokens

    def _bundle(self, messages: list, previous_summary: str | None) -> list:
        if previous_summary:
            return [HumanMessage(content=previous_summary, name="summary"), *messages]
        return messages

    def _summarize_with(self, messages: list, *, previous_summary: str | None, raise_on_failure: bool) -> str | None:
        bundle = self._bundle(messages, previous_summary)
        for hook in self.before_summarization:
            hook(bundle)
        try:
            return self._create_summary(bundle)
        except Exception:
            logger.debug("Primary summary model failed", exc_info=True)
        fallback = self._fallback_summary(bundle)
        if fallback is not None:
            return fallback
        if raise_on_failure:
            raise SummaryGenerationError("summary generation failed")
        return None

    async def _asummarize_with(self, messages: list, *, previous_summary: str | None, raise_on_failure: bool = False) -> str | None:
        bundle = self._bundle(messages, previous_summary)
        for hook in self.before_summarization:
            hook(bundle)
        try:
            return await self._acreate_summary(bundle)
        except Exception:
            logger.debug("Primary summary model failed", exc_info=True)
        fallback = await self._afallback_summary(bundle)
        if fallback is not None:
            return fallback
        if raise_on_failure:
            raise SummaryGenerationError("summary generation failed")
        return None

    def _create_summary(self, messages: list) -> str:
        if not messages:
            return "没有更早的对话历史。"
        if not self._trim_messages_for_summary(messages):
            return "之前的对话过长，无法生成摘要。"
        return super()._create_summary(messages)

    async def _acreate_summary(self, messages: list) -> str:
        if not messages:
            return "没有更早的对话历史。"
        if not self._trim_messages_for_summary(messages):
            return "之前的对话过长，无法生成摘要。"
        return await super()._acreate_summary(messages)

    def _summary_prompt_text(self, messages: list) -> str | None:
        if not messages:
            return None
        trimmed = self._trim_messages_for_summary(messages)
        if not trimmed:
            return None
        formatted = get_buffer_string(trimmed, format="xml")
        return self.summary_prompt.format(messages=formatted).rstrip()

    def _canned_summary(self, messages: list) -> str:
        if not messages:
            return "没有更早的对话历史。"
        return "之前的对话过长，无法生成摘要。"

    def _fallback_summary(self, messages: list) -> str | None:
        model = self._fallback_model
        if model is None:
            return None
        prompt = self._summary_prompt_text(messages)
        if prompt is None:
            return self._canned_summary(messages)
        try:
            response = model.invoke(prompt, config={"tags": list(_SUMMARY_TAGS)})
            return response.text.strip()
        except Exception:
            logger.debug("Fallback summary model failed", exc_info=True)
            return None

    async def _afallback_summary(self, messages: list) -> str | None:
        model = self._fallback_model
        if model is None:
            return None
        prompt = self._summary_prompt_text(messages)
        if prompt is None:
            return self._canned_summary(messages)
        try:
            response = await model.ainvoke(prompt, config={"tags": list(_SUMMARY_TAGS)})
            return response.text.strip()
        except Exception:
            logger.debug("Fallback summary model failed", exc_info=True)
            return None

    def _state_update(self, summary: str, preserved: list) -> dict:
        return {
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *preserved],
            "summary_text": summary,
        }

    def before_model(self, state, runtime: Runtime) -> dict | None:
        del runtime
        try:
            prepared = self._prepare_compaction(state, force=False)
            if prepared is None:
                return None
            to_summarize, preserved, previous, _total = prepared
            summary = self._summarize_with(to_summarize, previous_summary=previous, raise_on_failure=False)
            if summary is None:
                return None
            return self._state_update(summary, preserved)
        except Exception:
            logger.debug("Automatic summarization failed; leaving the turn unchanged", exc_info=True)
            return None

    async def abefore_model(self, state, runtime: Runtime) -> dict | None:
        del runtime
        try:
            prepared = self._prepare_compaction(state, force=False)
            if prepared is None:
                return None
            to_summarize, preserved, previous, _total = prepared
            summary = await self._asummarize_with(to_summarize, previous_summary=previous)
            if summary is None:
                return None
            return self._state_update(summary, preserved)
        except Exception:
            logger.debug("Automatic summarization failed; leaving the turn unchanged", exc_info=True)
            return None

    async def acompact_state(self, state, runtime, *, force: bool = False, raise_on_failure: bool = False) -> ContextCompactionResult | None:
        del runtime
        prepared = self._prepare_compaction(state, force=force)
        if prepared is None:
            return None
        to_summarize, preserved, previous, total_tokens = prepared
        summary = await self._asummarize_with(to_summarize, previous_summary=previous, raise_on_failure=raise_on_failure)
        if summary is None:
            return None
        return ContextCompactionResult(
            summary_text=summary,
            messages_to_summarize=tuple(to_summarize),
            preserved_messages=tuple(preserved),
            total_tokens=total_tokens,
        )


def _protected_ids(messages: list) -> set[str]:
    protected: set[str] = set()
    latest = next((message for message in reversed(messages) if is_real_user_message(message)), None)
    if latest is not None and latest.id:
        protected.add(latest.id)
    for message in messages:
        if is_dynamic_context_reminder(message) and message.id:
            protected.add(message.id)
    return protected


def _context_window(model: Any) -> int | None:
    profile = getattr(model, "profile", None)
    if isinstance(profile, dict):
        value = profile.get("max_input_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _trigger_tuples(trigger: ConfigContextSize | list[ConfigContextSize] | None) -> list[tuple[str, int | float]] | None:
    if trigger is None:
        return None
    items = trigger if isinstance(trigger, list) else [trigger]
    return [item.to_tuple() for item in items]


def _drop_fraction(values: list[tuple[str, int | float]] | None, *, kind: str) -> list[tuple[str, int | float]] | None:
    if not values:
        return None
    kept = [item for item in values if item[0] != "fraction"]
    if len(kept) != len(values):
        logger.warning("Dropping %s fraction limits because the anchor model has no context_window", kind)
    return kept or None


def create_summarization_middleware(
    *,
    app_config: AppConfig | None = None,
    keep: tuple[str, int | float] | None = None,
    run_model_name: str | None = None,
) -> MyFlowSummarizationMiddleware | None:
    config = (app_config or get_app_config()).summarization
    if not config.enabled:
        return None
    names: list[str | None] = []
    primary = config.model_name or run_model_name
    if primary:
        names.append(primary)
    if run_model_name and run_model_name not in names:
        names.append(run_model_name)
    if None not in names:
        names.append(None)
    model = None
    for name in names:
        try:
            kwargs: dict[str, Any] = {"name": name, "thinking_enabled": False, "attach_tracing": False}
            if app_config is not None:
                kwargs["app_config"] = app_config
            model = create_chat_model(**kwargs)
            break
        except Exception:
            logger.debug("Summary model %r failed to build", name, exc_info=True)
    if model is None:
        return None
    window = _context_window(model)
    trigger = _trigger_tuples(config.trigger)
    keep_tuple = keep or config.keep.to_tuple()
    if window is None:
        trigger = _drop_fraction(trigger, kind="trigger")
        if keep_tuple[0] == "fraction":
            logger.warning("Dropping keep fraction because the anchor model has no context_window")
            keep_tuple = ("messages", 20)
    fallback = None
    if config.model_name and run_model_name and run_model_name != config.model_name:
        try:
            fallback_kwargs: dict[str, Any] = {
                "name": run_model_name,
                "thinking_enabled": False,
                "attach_tracing": False,
            }
            if app_config is not None:
                fallback_kwargs["app_config"] = app_config
            fallback = create_chat_model(**fallback_kwargs)
        except Exception:
            logger.debug("Fallback summary model %r failed to build", run_model_name, exc_info=True)
    parent_trigger: Any = trigger[0] if trigger and len(trigger) == 1 else trigger
    return MyFlowSummarizationMiddleware(
        model,
        trigger=parent_trigger,
        keep=keep_tuple,
        summary_prompt=config.summary_prompt,
        trim_tokens_to_summarize=config.trim_tokens_to_summarize,
        fallback_model=fallback,
        app_config=app_config,
        run_model_name=run_model_name,
    )


async def compact_thread_context(
    checkpointer,
    thread_id: str,
    *,
    middleware: MyFlowSummarizationMiddleware,
    force: bool = False,
) -> CompactOutcome:
    head = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    if head is None:
        return CompactOutcome(thread_id=thread_id, compacted=False)
    checkpoint = head.checkpoint if isinstance(head.checkpoint, dict) else {}
    channels = checkpoint.get("channel_values") if isinstance(checkpoint, dict) else None
    if not isinstance(channels, dict):
        return CompactOutcome(thread_id=thread_id, compacted=False)
    state = {"messages": list(channels.get("messages") or []), "summary_text": channels.get("summary_text")}
    result = await middleware.acompact_state(state, None, force=force, raise_on_failure=True)
    if result is None:
        return CompactOutcome(thread_id=thread_id, compacted=False)
    mutation = build_state_mutation_graph("compact", checkpointer=checkpointer, state_schema=get_thread_state_schema())
    updated = await mutation.aupdate_state(
        head.config,
        {
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result.preserved_messages],
            "summary_text": result.summary_text,
        },
        as_node="compact",
    )
    checkpoint_id = None
    if isinstance(updated, dict):
        checkpoint_id = (updated.get("configurable") or {}).get("checkpoint_id")
    return CompactOutcome(
        thread_id=thread_id,
        compacted=True,
        summary_text=result.summary_text,
        removed_message_count=len(result.messages_to_summarize),
        preserved_message_count=len(result.preserved_messages),
        checkpoint_id=checkpoint_id,
        total_tokens=result.total_tokens,
    )


async def compact_with_reservation(
    run_manager: RunManager,
    checkpointer,
    thread_id: str,
    *,
    user_id: str | None,
    middleware: MyFlowSummarizationMiddleware,
    force: bool = False,
) -> CompactOutcome:
    async with run_manager.reserve_thread_operation(
        thread_id,
        kind=ThreadOperationKind.checkpoint_write,
        user_id=user_id,
    ):
        return await compact_thread_context(checkpointer, thread_id, middleware=middleware, force=force)
