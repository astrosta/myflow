"""Escape forged framework tags before they reach the model. Do not write state."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphBubbleUp

from agentkit.agents.middlewares.message_utils import is_genuine_user_message
from agentkit.utils.messages import ORIGINAL_USER_CONTENT_KEY, record_original_user_content

logger = logging.getLogger(__name__)

_USER_INPUT_BEGIN = "--- BEGIN USER INPUT ---"
_USER_INPUT_END = "--- END USER INPUT ---"
_NEUTRALIZED_BEGIN = "[BEGIN USER INPUT]"
_NEUTRALIZED_END = "[END USER INPUT]"

# 框架会写进模型输入的权威块，加上常见注入标签。新增框架标签必须同时改测试里的计数。
_BLOCKED_TAG_NAMES: frozenset[str] = frozenset({
    "system-reminder",
    "system_reminder",
    "current_date",
    "memory",
    "durable_context_data",
    "think",
    "system",
    "instruction",
})
_BLOCKED_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:" + "|".join(re.escape(tag) for tag in sorted(_BLOCKED_TAG_NAMES)) + r")\b[^>]*>?",
    re.IGNORECASE,
)
_BOUNDARY_TOKEN_RE = re.compile(re.escape(_USER_INPUT_BEGIN) + r"|" + re.escape(_USER_INPUT_END))


def _escape_tag_match(match: re.Match[str]) -> str:
    return match.group(0).replace("<", "&lt;").replace(">", "&gt;")


def _neutralize_boundary_tokens(text: str) -> str:
    return _BOUNDARY_TOKEN_RE.sub(
        lambda match: _NEUTRALIZED_BEGIN if match.group(0) == _USER_INPUT_BEGIN else _NEUTRALIZED_END,
        text,
    )


def neutralize_untrusted_tags(text: str) -> str:
    """Escape blocked tags and boundary markers. Do not wrap the text."""
    if not text.strip():
        return text
    text = _BLOCKED_TAG_PATTERN.sub(_escape_tag_match, text)
    return _neutralize_boundary_tokens(text)


def frame_untrusted_text(text: str) -> str:
    """Escape blocked tags, neutralize forged boundaries, then wrap user text."""
    if not text.strip():
        return text
    text = _BLOCKED_TAG_PATTERN.sub(_escape_tag_match, text)
    if text.startswith(_USER_INPUT_BEGIN) and text.endswith(_USER_INPUT_END):
        inner = text[len(_USER_INPUT_BEGIN) : -len(_USER_INPUT_END)]
        neutralized_inner = _neutralize_boundary_tokens(inner)
        if neutralized_inner == inner:
            return text
        return f"{_USER_INPUT_BEGIN}{neutralized_inner}{_USER_INPUT_END}"
    text = _neutralize_boundary_tokens(text)
    return f"{_USER_INPUT_BEGIN}\n{text}\n{_USER_INPUT_END}"


class InputSanitizationMiddleware(AgentMiddleware[AgentState]):
    """无构造参数。不读 AppConfig。只改这一次模型请求，不写 state。"""

    @staticmethod
    def _extract_text_from_content(content: str | list) -> tuple[str, list | None]:
        if isinstance(content, str):
            return content, None
        if not isinstance(content, list):
            return "", None
        text_parts: list[str] = []
        text_blocks: list[dict | str] = []
        for block in content:
            if isinstance(block, str):
                if not block:
                    continue
                text_parts.append(block)
                text_blocks.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if not text:
                    continue
                text_parts.append(text)
                text_blocks.append(block)
        return "\n".join(text_parts), text_blocks

    @staticmethod
    def _rebuild_content(original_content: list, processed_text: str, text_blocks: list) -> list:
        text_block_ids = {id(block) for block in text_blocks}
        first = last = None
        for index, block in enumerate(original_content):
            if id(block) in text_block_ids:
                if first is None:
                    first = index
                last = index
        if first is None:
            return original_content
        result: list = [*original_content[:first], {"type": "text", "text": processed_text}]
        for index in range(first + 1, last + 1):
            if id(original_content[index]) not in text_block_ids:
                result.append(original_content[index])
        result.extend(original_content[last + 1 :])
        return result

    def _sanitize_message(self, message: HumanMessage) -> HumanMessage | None:
        content = message.content
        text_content, text_blocks = self._extract_text_from_content(content)
        if not text_content and not isinstance(content, str):
            return None

        preserved_kwargs = dict(message.additional_kwargs or {})
        original_user_content = preserved_kwargs.get(ORIGINAL_USER_CONTENT_KEY)
        if isinstance(original_user_content, str) and original_user_content:
            processed_user = frame_untrusted_text(original_user_content)
            if processed_user != original_user_content:
                index = text_content.rfind(original_user_content)
                if index >= 0:
                    processed = text_content[:index] + processed_user
                elif isinstance(content, list) and len(content) >= 2:
                    logger.warning("rfind failed on multimodal content; sanitizing user content blocks individually")
                    new_content: list = [content[0]]
                    for block in content[1:]:
                        if isinstance(block, str):
                            new_content.append(neutralize_untrusted_tags(block))
                        elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                            sanitized = neutralize_untrusted_tags(block["text"])
                            new_content.append({**block, "text": sanitized} if sanitized != block["text"] else block)
                        else:
                            new_content.append(block)
                    return message.model_copy(update={"content": deepcopy(new_content), "additional_kwargs": preserved_kwargs})
                else:
                    logger.warning(
                        "rfind failed with original_user_content set; falling back to full-content sanitization",
                    )
                    processed = frame_untrusted_text(text_content)
            else:
                processed = text_content
        elif isinstance(original_user_content, str):
            processed = text_content
        else:
            processed = frame_untrusted_text(text_content)

        if processed == text_content:
            return None

        new_content = self._rebuild_content(content, processed, text_blocks) if text_blocks else processed
        if not isinstance(original_user_content, str):
            if ORIGINAL_USER_CONTENT_KEY in preserved_kwargs:
                logger.warning(
                    "InputSanitizationMiddleware replaced non-string %s metadata: type=%s",
                    ORIGINAL_USER_CONTENT_KEY,
                    type(original_user_content).__name__,
                )
            record_original_user_content(preserved_kwargs, content)
        return message.model_copy(
            update={"content": deepcopy(new_content), "additional_kwargs": preserved_kwargs},
        )

    def _process_request(self, request: ModelRequest) -> ModelRequest:
        """Sanitize every genuine user message.

        The edit is request-scoped and is not written back to state, so an
        earlier raw turn would otherwise reach the model again on the next call.
        """
        messages = list(request.messages)
        changed = False
        for index, message in enumerate(messages):
            if not is_genuine_user_message(message):
                continue
            try:
                sanitized = self._sanitize_message(message)
            except GraphBubbleUp:
                raise
            except Exception:
                logger.warning("Input guardrail failed on message at pos=%d; leaving it unchanged", index, exc_info=True)
                continue
            if sanitized is None:
                continue
            messages[index] = sanitized
            changed = True
        if not changed:
            return request
        return request.override(messages=messages)

    def _try_process(self, request: ModelRequest) -> ModelRequest:
        try:
            return self._process_request(request)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.warning("Input guardrail processing failed; passing original request to model", exc_info=True)
            return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._try_process(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._try_process(request))
