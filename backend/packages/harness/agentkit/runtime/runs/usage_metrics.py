"""LLM 用量抽取的**唯一口径**（M3 的 lead 账本与 M8 §15 的子代理账本共用）。

同一份 provider 响应会被两条链读到：父循环上的 ``UsageCollector``（lead）和隔离循环上的
``SubagentTokenCollector``（子代理，§15）。两边各写一份解析的后果是同一张账单在两个报表里
对不上——所以这里只放"从 ``LLMResult`` 里读什么"这一件事，两边都 import 它。

口径（与 M4 的 ``RunJournal._extract_usage`` 一致）：优先 ``message.usage_metadata``，
其次 ``llm_output['token_usage' | 'usage']``，并认 ``prompt_tokens`` / ``completion_tokens``
别名；缺字段按 0 累加。**全 0 不等于"没有消耗"**：调用方拿 ``known`` 判断（§15：
"0 和未知是不同的信息"）。

本模块是**叶子**：只 import ``langchain_core``，免得 ``runtime.runs.usage`` 与 ``subagents``
互相拖进对方的依赖链。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.outputs import LLMResult

#: provider 没报模型名时的占位。``RunTotals.token_usage_by_model`` 需要一个键。
UNKNOWN_MODEL = "unknown"

#: 外部用量记录的 ``caller`` 前缀：M3 的账本按 lead / subagent / middleware 三个维度分栏
#: （``RunJournal._caller`` 用的是同一族字符串）。M8 §15 的外部记录用 ``subagent:{name}``。
MIDDLEWARE_CALLER_PREFIX = "middleware:"
SUBAGENT_CALLER_PREFIX = "subagent:"


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """一次模型响应的用量。**没有** provider 数据时是 ``known=False``，不是"0 token"。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        """输入 + 输出。缓存命中已经算在输入里，不再加一遍（与 provider 的 total 同口径）。"""
        return self.input_tokens + self.output_tokens

    @property
    def known(self) -> bool:
        """provider 到底给没给用量。全 0 视为没给：流式提供商的第一份响应就是这个形状。"""
        return self.total_tokens > 0


def extract_llm_usage(response: LLMResult) -> LlmUsage:
    """把一次 ``on_llm_end`` 的响应折成 :class:``LlmUsage``。"""
    input_tokens = output_tokens = cache_read = 0
    # generations 外层 list 对应不同的输入 prompt，内层 list 对应同一个 prompt 的多次生成
    for generation in itertools.chain.from_iterable(response.generations or ()):
        meta = _generation_usage(generation, response)
        input_tokens += int(meta.get("input_tokens") or 0)
        output_tokens += int(meta.get("output_tokens") or 0)
        details = meta.get("input_token_details") or {}
        if isinstance(details, Mapping):
            cache_read += int(details.get("cache_read") or 0)
    return LlmUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        model=_model_name(response),
    )


def _generation_usage(generation: Any, response: LLMResult) -> Mapping[str, Any]:
    """这一条 generation 自己的用量；它没有才退到 ``llm_output``（整体级）。"""
    message = getattr(generation, "message", None)
    meta = dict(getattr(message, "usage_metadata", None) or {})
    if meta:
        return meta
    llm_output = getattr(response, "llm_output", None) or {}
    meta = dict(llm_output.get("token_usage") or llm_output.get("usage") or {})
    if "prompt_tokens" in meta and "input_tokens" not in meta:
        meta["input_tokens"] = meta["prompt_tokens"]
    if "completion_tokens" in meta and "output_tokens" not in meta:
        meta["output_tokens"] = meta["completion_tokens"]
    return meta


def _model_name(response: LLMResult) -> str | None:
    for generation in itertools.chain.from_iterable(response.generations or ()):
        message = getattr(generation, "message", None)
        meta = getattr(message, "response_metadata", None) or {}
        if isinstance(meta, Mapping) and (name := meta.get("model_name") or meta.get("model")):
            return str(name)
    return None


__all__ = [
    "MIDDLEWARE_CALLER_PREFIX",
    "SUBAGENT_CALLER_PREFIX",
    "UNKNOWN_MODEL",
    "LlmUsage",
    "extract_llm_usage",
]
