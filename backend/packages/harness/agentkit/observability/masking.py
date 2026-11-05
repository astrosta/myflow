"""Export-stage redaction for Langfuse spans.

LangChain callbacks can see more than the model boundary: tool arguments are
restored from the PII vault before a tool runs, and provider output is redacted
only when it is written back to state. Neither is safe to ship to an external
observability backend verbatim.

The hook is deliberately synchronous and best-effort. Langfuse drops a whole
export batch if a mask hook raises, so every failure is converted into a
redacted value instead of an exception.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agentkit.privacy import PIIRedactor

CONTENT_ATTRIBUTE_MAX_BYTES = 256 * 1024
TRUNCATED = "[TRUNCATED]"
REDACTED = "[REDACTED]"

_CONTENT_SUFFIXES = (
    ".input",
    ".output",
    ".content",
    ".status_message",
    ".prompt",
    ".completion",
    ".messages",
)
_CONTENT_KEYS = frozenset({"input", "output", "status_message"})


def is_content_attribute(key: str) -> bool:
    """Return True for prompt/completion/tool payloads, not usage counters."""

    lowered = key.lower()
    if lowered in _CONTENT_KEYS:
        return True
    if lowered.endswith(_CONTENT_SUFFIXES):
        return True
    # OpenTelemetry GenAI uses indexed attributes such as
    # gen_ai.prompt.0.content and gen_ai.completion.0.content.
    return any(
        marker in lowered
        for marker in (".prompt.", ".completion.", ".messages.")
    )


def _redact_json(value: Any, redactor: PIIRedactor) -> Any:
    if isinstance(value, str):
        return redactor.redact_for_memory(value)
    if isinstance(value, list):
        return [_redact_json(item, redactor) for item in value]
    if isinstance(value, tuple):
        return [_redact_json(item, redactor) for item in value]
    if isinstance(value, dict):
        return {
            redactor.redact_for_memory(str(key)): _redact_json(item, redactor)
            for key, item in value.items()
        }
    return value


def redact_attribute_value(value: Any, *, max_chars: int = CONTENT_ATTRIBUTE_MAX_BYTES) -> Any:
    """Redact one span attribute, preserving JSON shape when possible."""

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return REDACTED
    if not isinstance(value, str):
        try:
            return _redact_json(value, PIIRedactor())
        except Exception:  # noqa: BLE001 - masking must never raise
            return REDACTED
    if len(value) > max_chars:
        return TRUNCATED

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    else:
        try:
            redacted = _redact_json(parsed, PIIRedactor())
            return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
        except Exception:  # noqa: BLE001 - masking must never raise
            return REDACTED

    try:
        return PIIRedactor().redact_for_memory(value)
    except Exception:  # noqa: BLE001 - masking must never raise
        return REDACTED


def mask_attributes(
    attributes: Mapping[str, Any],
    capture_content: str,
    *,
    max_chars: int = CONTENT_ATTRIBUTE_MAX_BYTES,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return (set_attributes, delete_attributes) for one exported span."""

    patched: dict[str, Any] = {}
    deleted: list[str] = []

    for key, value in attributes.items():
        if not is_content_attribute(key):
            # Client-supplied metadata can contain PII too. Content-mode
            # masking must not leave it untouched just because it is not a
            # prompt/completion attribute.
            if capture_content in {"redacted", "metadata_only"}:
                if isinstance(value, str):
                    redacted = redact_attribute_value(value, max_chars=max_chars)
                    if redacted != value:
                        patched[key] = redacted
                elif isinstance(value, (list, tuple)):
                    redacted_items = [
                        redact_attribute_value(item, max_chars=max_chars)
                        if isinstance(item, str)
                        else item
                        for item in value
                    ]
                    if redacted_items != list(value):
                        patched[key] = redacted_items
            continue
        if capture_content == "metadata_only":
            deleted.append(key)
            continue

        if capture_content == "full":
            if isinstance(value, str) and len(value) > max_chars:
                patched[key] = TRUNCATED
            continue

        patched[key] = redact_attribute_value(value, max_chars=max_chars)

    return patched, tuple(deleted)


def build_mask_otel_spans(capture_content: str):
    """Build the synchronous mask_otel_spans hook for the Langfuse client."""

    def mask_otel_spans(*, params):
        try:
            from langfuse.types import MaskOtelSpansResult, OtelSpanPatch
        except Exception:  # noqa: BLE001 - optional dependency / version mismatch
            return None

        span_patches: dict[Any, Any] = {}
        for identifier, span in getattr(params, "spans", {}).items():
            attrs = getattr(span, "attributes", None) or {}
            try:
                patched, deleted = mask_attributes(attrs, capture_content)
            except Exception:  # noqa: BLE001 - fail closed, never export raw content
                # Deleting every content-looking attribute is the only safe
                # fallback: returning no patch would ship the original payload.
                deleted = tuple(
                    str(key) for key in attrs if is_content_attribute(str(key))
                )
                patched = {}

            if not patched and not deleted:
                continue
            try:
                span_patches[identifier] = OtelSpanPatch(
                    set_attributes=patched,
                    delete_attributes=deleted,
                )
            except Exception:  # noqa: BLE001 - never fail the batch
                continue

        if not span_patches:
            return None
        return MaskOtelSpansResult(span_patches=span_patches)

    return mask_otel_spans


__all__ = [
    "CONTENT_ATTRIBUTE_MAX_BYTES",
    "build_mask_otel_spans",
    "is_content_attribute",
    "mask_attributes",
    "redact_attribute_value",
]
