import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from agentkit.runtime.runs.manager import RunManager

logger = logging.getLogger(__name__)
PROGRESS_REPORT_INTERVAL_SECONDS = 5.0

@dataclass(frozen=True, slots=True)
class RunTotals:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    token_usage_by_model: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        attributed = self.lead_agent_tokens + self.subagent_tokens + self.middleware_tokens
        total = self.total_input_tokens + self.total_output_tokens
        if attributed != total:
            logger.error("Token attribution %d does not add up to total %d", attributed, total)

@dataclass(frozen=True, slots=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int=0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

class UsageCollector(AsyncCallbackHandler):
    run_inline = True

    def __init__(self, run_manager: RunManager, run_id: str) -> None:
        self._run_manager = run_manager
        self._run_id = run_id
        self._by_model: dict[str, dict[str, int]] = {}
        self._calls = 0
        self._counted_llm_run_ids: set[str] = set()
        self._last_report = time.monotonic()

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        usage = _extract_usage(response)
        if usage.total <=0:
            return
        
        rid = str(run_id) if run_id is not None else None
        if rid is not None and rid in self._counted_llm_run_ids:
            return
        if rid is not None:
            self._counted_llm_run_ids.add(rid)
        self._calls += 1
        model = _model_name(response) or "unknown"
        bucket = self.by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
        bucket["input"] += usage.input_tokens
        bucket["output"] += usage.output_tokens
        bucket["cache_read"] += usage.cache_read_tokens

    def totals(self) -> RunTotals:
        input_tokens = sum(bucket["input"] for bucket in self._by_model.values())
        output_tokens = sum(bucket["output"] for bucket in self._by_model.values())
        return RunTotals(
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            llm_call_count=self._calls,
            lead_agent_tokens=input_tokens+output_tokens,
            token_usage_by_model={model: dict(bucket) for model, bucket in self._by_model.items()},
        )
    async def maybe_report(self) -> None:
        now = time.monotonic()
        if now - self._last_report >= PROGRESS_REPORT_INTERVAL_SECONDS:
            self._last_report = now
            await self._run_manager.update_run_progress(self._run_id, self.totals())

def _extract_usage(response: LLMResult) -> _Usage:
    """优先 message.usage_metadata；没有再看 llm_output['token_usage']。缺字段当 0。"""
    input_tokens = output_tokens = cache_read = 0
    # generations外层list对应不同的输入prompt，内层list对应同一个prompt的多次生成结果
    for generation in itertools.chain.from_iterable(response.generations or ()):
        message = getattr(generation, "message", None)
        meta = dict(getattr(message, "usage_metadata", None)) or {}
        if not meta:
            llm_output = getattr(response, "llm_output", None) or {}
            meta = dict(llm_output.get("token_usage") or llm_output.get("usage")) or {}
            if "prompt_tokens" in meta and "input_tokens" not in meta:
                meta["input_tokens"] = meta["prompt_tokens"]
            if "completion_tokens" in meta and "output_tokens" not in meta:
                meta["output_tokens"] = meta["completion_tokens"]
        input_tokens += int(meta.get("input_tokens") or 0)
        output_tokens += int(meta.get("output_tokens") or 0)
        details = meta.get("input_token_details") or {}
        if isinstance(details, Mapping):
            cache_read += int(details.get("cache_read") or 0)
    return _Usage(input_tokens, output_tokens, cache_read)

def _model_name(response: LLMResult) -> str | None:
    for generation in itertools.chain.from_iterable(response.generations or ()):
        message = getattr(generation, "message", None)
        meta = getattr(message, "response_metadata", None) or {}
        if isinstance(meta, Mapping) and (name := meta.get("model_name") or meta.get("model")):
            return message.str(name)
    return None