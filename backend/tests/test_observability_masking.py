from __future__ import annotations

import json

from agentkit.observability.masking import (
    CONTENT_ATTRIBUTE_MAX_BYTES,
    is_content_attribute,
    mask_attributes,
    redact_attribute_value,
)


def test_content_attribute_detection_does_not_touch_usage_counters() -> None:
    assert is_content_attribute("langfuse.observation.input")
    assert is_content_attribute("langfuse.observation.output")
    assert is_content_attribute("gen_ai.prompt.0.content")
    assert is_content_attribute("gen_ai.completion.0.content")
    assert is_content_attribute("tool.input")
    assert is_content_attribute("status_message")

    assert not is_content_attribute("gen_ai.usage.input_tokens")
    assert not is_content_attribute("gen_ai.usage.output_tokens")
    assert not is_content_attribute("langfuse.observation.metadata.run_id")
    assert not is_content_attribute("langfuse.observation.model.name")


def test_redacted_value_removes_literal_pii_and_handles() -> None:
    value = json.dumps(
        {
            "email": "alice@example.com",
            "phone": "13800138000",
            "handle": "[[PII:EMAIL:deadbeef:1]]",
            "count": 2,
        }
    )

    redacted = redact_attribute_value(value)
    decoded = json.loads(redacted)

    assert decoded["email"] == "[REDACTED]"
    assert decoded["phone"] == "[REDACTED]"
    assert decoded["handle"] == "[REDACTED]"
    assert decoded["count"] == 2
    assert "alice@example.com" not in redacted


def test_redacted_value_handles_plain_text_and_malformed_json() -> None:
    assert "alice@example.com" not in redact_attribute_value("alice@example.com")
    assert redact_attribute_value("{not-json") == "{not-json"


def test_redacted_value_also_redacts_object_keys() -> None:
    value = json.dumps({"alice@example.com": "note"})

    redacted = redact_attribute_value(value)

    assert "alice@example.com" not in redacted
    assert json.loads(redacted)["[REDACTED]"] == "note"


def test_redacted_value_is_bounded() -> None:
    assert redact_attribute_value("x" * (CONTENT_ATTRIBUTE_MAX_BYTES + 1)) == "[TRUNCATED]"


def test_metadata_only_deletes_content_but_keeps_usage_and_model() -> None:
    attrs = {
        "langfuse.observation.input": "secret prompt",
        "langfuse.observation.output": "secret answer",
        "gen_ai.prompt.0.content": "secret prompt",
        "gen_ai.usage.input_tokens": 12,
        "gen_ai.request.model": "deepseek-reasoner",
    }

    patched, deleted = mask_attributes(attrs, "metadata_only")

    assert deleted == (
        "langfuse.observation.input",
        "langfuse.observation.output",
        "gen_ai.prompt.0.content",
    )
    assert patched == {}


def test_metadata_only_redacts_pii_in_metadata_but_not_usage() -> None:
    attrs = {
        "langfuse.observation.metadata.note": json.dumps({"email": "alice@example.com"}),
        "gen_ai.usage.input_tokens": 12,
    }

    patched, deleted = mask_attributes(attrs, "metadata_only")

    assert deleted == ()
    assert "alice@example.com" not in patched["langfuse.observation.metadata.note"]
    assert "gen_ai.usage.input_tokens" not in patched


def test_redacted_mode_masks_pii_in_tag_sequences() -> None:
    attrs = {"langfuse.trace.tags": ("myflow", "alice@example.com")}

    patched, deleted = mask_attributes(attrs, "redacted")

    assert deleted == ()
    assert patched["langfuse.trace.tags"] == ["myflow", "[REDACTED]"]


def test_redacted_mode_keeps_structure_but_redacts_content() -> None:
    attrs = {
        "langfuse.observation.input": json.dumps({"text": "alice@example.com"}),
        "gen_ai.usage.input_tokens": 12,
    }

    patched, deleted = mask_attributes(attrs, "redacted")

    assert deleted == ()
    assert "alice@example.com" not in patched["langfuse.observation.input"]
    assert "gen_ai.usage.input_tokens" not in patched, "usage counters must stay untouched"


def test_full_mode_still_bounds_large_content() -> None:
    attrs = {"langfuse.observation.input": "x" * (CONTENT_ATTRIBUTE_MAX_BYTES + 1)}

    patched, deleted = mask_attributes(attrs, "full")

    assert deleted == ()
    assert patched["langfuse.observation.input"] == "[TRUNCATED]"

def test_sdk_mask_hook_returns_a_patch_for_exported_content() -> None:
    import pytest

    langfuse_types = pytest.importorskip("langfuse.types")
    from agentkit.observability.masking import build_mask_otel_spans

    identifier = langfuse_types.OtelSpanIdentifier(trace_id="a" * 32, span_id="b" * 16)
    span = langfuse_types.OtelSpanData(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        name="generation",
        instrumentation_scope_name=None,
        instrumentation_scope_version=None,
        attributes={
            "langfuse.observation.input": json.dumps({"email": "alice@example.com"}),
            "gen_ai.usage.input_tokens": 3,
        },
        resource_attributes={},
    )

    result = build_mask_otel_spans("redacted")(
        params=langfuse_types.MaskOtelSpansParams(spans={identifier: span})
    )

    assert result is not None
    patch = result.span_patches[identifier]
    assert "alice@example.com" not in patch.set_attributes["langfuse.observation.input"]
    assert patch.delete_attributes == ()

def test_mask_hook_deletes_content_if_redaction_fails(monkeypatch) -> None:
    import pytest

    langfuse_types = pytest.importorskip("langfuse.types")
    import agentkit.observability.masking as masking

    def _boom(*args, **kwargs):
        raise RuntimeError("redactor broke")

    monkeypatch.setattr(masking, "mask_attributes", _boom)
    identifier = langfuse_types.OtelSpanIdentifier(trace_id="a" * 32, span_id="b" * 16)
    span = langfuse_types.OtelSpanData(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        name="generation",
        instrumentation_scope_name=None,
        instrumentation_scope_version=None,
        attributes={"langfuse.observation.input": "do not export me"},
        resource_attributes={},
    )

    result = masking.build_mask_otel_spans("redacted")(
        params=langfuse_types.MaskOtelSpansParams(spans={identifier: span})
    )

    assert result is not None
    assert "langfuse.observation.input" in result.span_patches[identifier].delete_attributes
