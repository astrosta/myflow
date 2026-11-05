from __future__ import annotations

from agentkit.config import AppConfig
from backend.app.gateway.services import build_run_config


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {"models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]}
    )


def test_client_metadata_cannot_spoof_langfuse_identity_or_trace_attributes() -> None:
    run_metadata = {"myflow-trace_id": "a" * 32}
    request_config = {
        "metadata": {
            "safe": "kept",
            "langfuse_user_id": "attacker",
            "langfuse_session_id": "someone-else",
            "langfuse_trace_name": "spoofed",
            "langfuse_tags": ["admin"],
            "trace_parent_span_id": "b" * 16,
        }
    }

    built = build_run_config("thread-1", request_config, run_metadata, app_config=_config())

    assert built["metadata"]["safe"] == "kept"
    assert built["metadata"]["myflow-trace_id"] == "a" * 32
    assert not any(key.startswith("langfuse_") for key in built["metadata"])
    assert "trace_parent_span_id" not in built["metadata"]


def test_non_string_metadata_keys_are_ignored_instead_of_crashing() -> None:
    built = build_run_config(
        "thread-1",
        {"metadata": {1: "not-a-key", "safe": "kept"}},
        {"myflow-trace_id": "a" * 32},
        app_config=_config(),
    )

    assert built["metadata"]["safe"] == "kept"
    assert 1 not in built["metadata"]
