"""Persisted run kwargs must not echo credentials or server trace keys."""

from backend.app.gateway.services import redact_config_secrets, redact_context_secrets


def test_redact_config_secrets_drops_secrets_dunder_and_trace_keys() -> None:
    config = {
        "recursion_limit": 5,
        "tags": ["a"],
        "metadata": {"topic": "中文", "myflow-trace_id": "trace-1", "__internal": 1},
        "configurable": {"thread_id": "thread-1", "secrets": {"token": "hidden"}},
        "context": {"model_name": "lead", "secrets": {"api": "secret"}, "trace_id": "trace-1"},
    }

    cleaned = redact_config_secrets(config)

    assert cleaned == {
        "recursion_limit": 5,
        "tags": ["a"],
        "metadata": {"topic": "中文"},
        "configurable": {"thread_id": "thread-1"},
        "context": {"model_name": "lead"},
    }
    assert config["context"]["secrets"] == {"api": "secret"}
    assert config["metadata"]["__internal"] == 1
    assert redact_config_secrets(None) is None


def test_redact_context_secrets_uses_the_same_strip() -> None:
    context = {"model_name": "lead", "secrets": {"api": "secret"}, "__pregel": True}
    assert redact_context_secrets(context) == {"model_name": "lead"}
    assert "secrets" in context
    assert redact_context_secrets(None) is None
