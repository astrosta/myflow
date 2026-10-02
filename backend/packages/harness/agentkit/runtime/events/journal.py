"""The only regular producer of the run-event ledger.

Token totals stay on ``UsageCollector`` and ``update_run_progress``. This class
never writes a token, and it never changes run status.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from agentkit.runtime.events.catalog import (
    LLM_AI_RESPONSE,
    LLM_ERROR_EVENT,
    LLM_TOOL_RESULT,
    RUN_END_EVENT,
    RUN_ERROR_EVENT,
    RUN_START_EVENT,
    RunEventDefinition,
)
from agentkit.runtime.events.store.base import RunEventStore
from agentkit.runtime.events.store.codec import limit_trace


class RunJournal(AsyncCallbackHandler):
    """Buffer ledger rows and flush them as one ``put_batch``.

    Flush when the buffer reaches ``batch_size``, and when the worker calls
    ``flush`` at a phase boundary or in ``finally``. ``on_llm_new_token`` is
    intentionally not implemented: tokens travel on the SSE and progress pipes.
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
    ) -> None:
        super().__init__()
        self._store = store
        self._record = record
        self._batch_size = batch_size
        self.flush_interval = flush_interval
        self.track_token_usage = track_token_usage
        self._max_trace_content = max_trace_content
        self._buf: list[dict[str, Any]] = []

    async def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        await self._append(RUN_START_EVENT, {"name": serialized.get("name"), "inputs": inputs})

    async def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        await self._append(RUN_END_EVENT, outputs)

    async def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        await self._append(RUN_ERROR_EVENT, str(error))

    async def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        await self._append(LLM_AI_RESPONSE, _message_content(response))

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        content = output if isinstance(output, (str, dict)) else str(output)
        await self._append(LLM_TOOL_RESULT, content)

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        await self._append(LLM_ERROR_EVENT, str(error))

    async def flush(self) -> None:
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        try:
            await self._store.put_batch(batch)
        except Exception:
            self._buf = batch + self._buf
            raise

    async def _append(self, definition: RunEventDefinition, content: Any) -> None:
        content, metadata = limit_trace(content, {}, definition.category, self._max_trace_content)
        self._buf.append(
            {
                "thread_id": self._record.thread_id,
                "run_id": self._record.run_id,
                "user_id": self._record.user_id,
                "event_type": definition.event_type,
                "category": definition.category,
                "content": content,
                "metadata": metadata,
            }
        )
        if len(self._buf) >= self._batch_size:
            await self.flush()


def _message_content(response: Any) -> Any:
    generations = getattr(response, "generations", None) or []
    for group in generations:
        for generation in group:
            message = getattr(generation, "message", None)
            content = getattr(message, "content", None)
            if isinstance(content, (str, dict)):
                return content
            if content is not None:
                return str(content)
    return ""
