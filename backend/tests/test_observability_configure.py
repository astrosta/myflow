from __future__ import annotations

import sys
import types

from agentkit.config import AppConfig
from agentkit.observability.runtime import (
    configure_observability,
    get_observability,
    reset_observability,
    shutdown_observability,
)


def _app_config(enabled: bool, *, required: bool = False) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}],
            "observability": {
                "langfuse": {
                    "enabled": enabled,
                    "base_url": "https://langfuse.internal.example",
                    "public_key": "pk-lf-test",
                    "secret_key": "sk-lf-test",
                    "user_id_salt": "a" * 32,
                    "required": required,
                }
            },
        }
    )


def test_disabled_configuration_stays_noop_without_importing_the_sdk(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "langfuse", None)
    reset_observability()

    runtime = configure_observability(_app_config(False))

    assert runtime.enabled is False
    assert get_observability().enabled is False


def test_enabled_configuration_builds_the_client_without_a_network_probe(monkeypatch) -> None:
    calls: dict = {}

    class _Client:
        def __init__(self, **kwargs) -> None:
            calls["init"] = kwargs

        def flush(self) -> None:
            calls["flushed"] = True

        def shutdown(self) -> None:
            calls["shutdown"] = True

    class _Handler:
        def __init__(self, **kwargs) -> None:
            calls["handler"] = kwargs

    def _propagate(**kwargs):
        calls["propagate"] = kwargs
        return _NullCM()

    langfuse_module = types.ModuleType("langfuse")
    langfuse_module.Langfuse = _Client
    langfuse_module.propagate_attributes = _propagate
    langchain_module = types.ModuleType("langfuse.langchain")
    langchain_module.CallbackHandler = _Handler
    monkeypatch.setitem(sys.modules, "langfuse", langfuse_module)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", langchain_module)
    reset_observability()

    runtime = configure_observability(_app_config(True))

    assert runtime.enabled is True
    assert calls["init"]["base_url"] == "https://langfuse.internal.example"
    assert calls["init"]["public_key"] == "pk-lf-test"
    assert calls["init"]["secret_key"] == "sk-lf-test"
    assert "auth_check" not in calls, "startup must not perform a remote health probe"

    shutdown_observability()
    assert calls["flushed"] is True
    assert calls["shutdown"] is True
    assert get_observability().enabled is False


def test_missing_sdk_is_soft_by_default_and_hard_when_required(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "langfuse", None)
    reset_observability()

    runtime = configure_observability(_app_config(True, required=False))
    assert runtime.enabled is False

    try:
        configure_observability(_app_config(True, required=True))
    except RuntimeError as exc:
        assert "langfuse" in str(exc)
    else:
        raise AssertionError("required=true must fail startup when the SDK is absent")


class _NullCM:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False
