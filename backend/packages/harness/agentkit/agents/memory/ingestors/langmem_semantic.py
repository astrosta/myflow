"""LangMem-style semantic extraction (M10 section 2.4.1).

LangMem is used here only as an extraction pattern: one model call returns
profile patches, facts, and instructions. It is intentionally not a hard
dependency; the model is injected through the host's model factory, and the
proposal gate remains deterministic. LangMem never writes canonical records.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from agentkit.agents.memory.ingestors.base import MemoryIngestor, visible_turn_text
from agentkit.agents.memory.types import (
    PROFILE_FIELD_PATHS,
    MemoryKind,
    MemoryProposal,
    MemoryScope,
)

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """你负责从一轮对话中抽取长期记忆候选。
只返回一个 JSON 对象，必须且只能包含三个键：profile_patches、facts、instructions。

profile_patches：{{field, value, source, evidence, confidence}} 的列表
  - field 必须是以下之一：{fields}
  - source 必须是 user_explicit_durable 或 inferred（inferred 会转成 fact）
  - evidence 必须是用户原文中的逐字引用
facts：{{content, topic_key, category, source, confidence, evidence}} 的列表
  - source 必须是 user_explicit 或 inferred 之一
  - category 必须是 preference、project、context、profile、instruction 之一
instructions：{{operation, target_fact_id, evidence}} 的列表
  - operation 必须是 forget、supersede、unprotect 之一
  - target_fact_id 必填

永远不要声称用户手动设置或确认了某件事：这类信号只能来自 UI 或确定性的宿主逻辑。
不要把凭据、令牌、密码或身份号码写进任何字段。如果没有值得长期保存的内容，
返回空列表。不要编造上面没有列出的字段。

用户文本：
<user>
{user}
</user>

助手最终文本：
<assistant>
{assistant}
</assistant>
"""


class LangMemSemanticIngestor(MemoryIngestor):
    kind = MemoryKind.SEMANTIC

    def __init__(self, model: Any, *, max_fact_chars: int = 2_000) -> None:
        self._model = model
        self._max_fact_chars = max_fact_chars

    @classmethod
    def from_config(
        cls,
        backend_config: dict[str, Any],
        *,
        app_config: Any = None,
        model: Any = None,
    ) -> "LangMemSemanticIngestor | None":
        if model is None:
            model_name = backend_config.get("extraction_model")
            if not model_name:
                logger.info("memory extraction model is not configured; facts extraction stays disabled")
                return None
            from agentkit.models import create_chat_model

            model = create_chat_model(
                name=model_name,
                thinking_enabled=False,
                attach_tracing=False,
                app_config=app_config,
            )
        return cls(model)

    def extract(self, scope: MemoryScope, messages: list[Any]) -> list[MemoryProposal]:
        del scope
        user_text, assistant_text = visible_turn_text(messages)
        if not user_text.strip():
            return []
        prompt = _EXTRACTION_PROMPT.format(
            fields=", ".join(sorted(PROFILE_FIELD_PATHS)),
            user=user_text[:12_000],
            assistant=assistant_text[:6_000],
        )
        try:
            response = self._model.invoke(prompt)
            raw = getattr(response, "content", response)
            if isinstance(raw, list):
                raw = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in raw
                )
            text = str(raw).strip()
            fence = chr(96) * 3
            if text.startswith(fence):
                text = text[len(fence) :].lstrip()
                if text.lower().startswith("json"):
                    text = text[4:].lstrip()
            if text.endswith(fence):
                text = text[: -len(fence)].rstrip()
            data = json.loads(text)
        except Exception as exc:
            # Do not attach exc_info: provider errors can contain request/prompt fragments.
            logger.warning("Memory extraction failed (%s); dropping this batch", type(exc).__name__)
            return []

        if not isinstance(data, dict) or not {"profile_patches", "facts", "instructions"} <= data.keys():
            logger.warning("Memory extraction returned a malformed batch; failing closed")
            return []
        profile_patches = data.get("profile_patches")
        facts = data.get("facts")
        instructions = data.get("instructions")
        if not isinstance(profile_patches, list) or not isinstance(facts, list) or not isinstance(instructions, list):
            logger.warning("Memory extraction returned non-list sections; failing closed")
            return []
        try:
            return _build_proposals(
                profile_patches,
                facts,
                instructions,
                user_text=user_text,
                transcript=f"{user_text}\n{assistant_text}",
                max_fact_chars=self._max_fact_chars,
            )
        except ValueError as exc:
            logger.warning("Memory extraction failed proposal validation: %s", exc)
            return []


def _build_proposals(
    profile_patches: list[Any],
    facts: list[Any],
    instructions: list[Any],
    *,
    user_text: str,
    transcript: str,
    max_fact_chars: int,
) -> list[MemoryProposal]:
    proposals: list[MemoryProposal] = []
    for patch in profile_patches:
        if not isinstance(patch, dict):
            raise ValueError("profile patch is not an object")
        if not {"field", "source", "evidence", "value"} <= patch.keys():
            raise ValueError("profile patch is missing a required field")
        field = patch.get("field")
        source = patch.get("source")
        evidence = patch.get("evidence")
        if field not in PROFILE_FIELD_PATHS:
            continue
        if source == "inferred":
            content = str(patch.get("value") or "").strip()
            if content:
                proposals.append(_fact_proposal(content, patch, transcript, max_fact_chars))
            continue
        # The model can propose a durable user instruction, never a manual setting or a
        # confirmation. Those two authorities only come from the UI / deterministic host.
        if source != "user_explicit_durable":
            continue
        if not isinstance(evidence, str) or not evidence.strip() or evidence not in user_text:
            continue
        proposals.append(
            MemoryProposal(
                id=f"proposal_{uuid4().hex}",
                kind=MemoryKind.PROFILE,
                operation="upsert",
                payload={
                    "fields": {field: patch.get("value")},
                    "source": source,
                },
                evidence=evidence,
                source="conversation",
                confidence=float(patch.get("confidence", 0.9)),
            )
        )
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("fact is not an object")
        if "content" not in fact:
            raise ValueError("fact is missing content")
        content = fact.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        proposals.append(_fact_proposal(content, fact, transcript, max_fact_chars))
    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise ValueError("instruction is not an object")
        operation = instruction.get("operation")
        target = instruction.get("target_fact_id")
        evidence = instruction.get("evidence")
        if operation not in {"forget", "supersede", "unprotect"} or not target:
            continue
        if not isinstance(evidence, str) or not evidence.strip() or evidence not in transcript:
            continue
        proposals.append(
            MemoryProposal(
                id=f"proposal_{uuid4().hex}",
                kind=MemoryKind.SEMANTIC,
                operation=operation,
                target_record_id=str(target),
                evidence=evidence,
                source="conversation",
            )
        )
    return proposals


def _fact_proposal(content: str, raw: dict, transcript: str, max_fact_chars: int) -> MemoryProposal:
    content = content.strip()
    if len(content) > max_fact_chars:
        content = content[:max_fact_chars]
    evidence = raw.get("evidence")
    return MemoryProposal(
        id=f"proposal_{uuid4().hex}",
        kind=MemoryKind.SEMANTIC,
        subtype="fact",
        operation="upsert",
        payload={
            "content": content,
            "topic_key": raw.get("topic_key") or None,
            "category": raw.get("category") or "context",
            "source": raw.get("source") if raw.get("source") in {"user_explicit", "inferred"} else "inferred",
            "confidence": float(raw.get("confidence", 0.8)),
            "explicit_remember": bool(raw.get("source") == "user_explicit"),
        },
        evidence=evidence if isinstance(evidence, str) and evidence in transcript else "",
        source="conversation",
        confidence=float(raw.get("confidence", 0.8)),
    )
