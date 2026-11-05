from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentkit.config import AppConfig
from agentkit.config.observability_config import LangfuseConfig, ObservabilityConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX


def _models() -> list[dict]:
    return [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]


def _enabled(**overrides):
    values = {
        "enabled": True,
        "base_url": "https://langfuse.internal.example",
        "public_key": "pk-lf-test",
        "secret_key": "sk-lf-test",
        "user_id_salt": "a" * 32,
    }
    values.update(overrides)
    return LangfuseConfig(**values)


def test_observability_defaults_are_off_and_safe() -> None:
    cfg = AppConfig.model_validate({"models": _models()})

    assert isinstance(cfg.observability, ObservabilityConfig)
    assert cfg.observability.langfuse.enabled is False
    assert cfg.observability.langfuse.capture_content == "redacted"
    assert cfg.observability.langfuse.user_id_mode == "hashed"
    assert cfg.observability.langfuse.trace_subagents is True


def test_disabled_section_may_be_empty() -> None:
    cfg = LangfuseConfig()
    assert cfg.enabled is False


def test_enabled_requires_remote_credentials() -> None:
    with pytest.raises(ValidationError, match="base_url"):
        _enabled(base_url="")

    with pytest.raises(ValidationError, match="public_key"):
        _enabled(public_key="")

    with pytest.raises(ValidationError, match="secret_key"):
        _enabled(secret_key="")


def test_base_url_must_be_http_or_https() -> None:
    with pytest.raises(ValidationError, match="http"):
        _enabled(base_url="langfuse.internal.example")


def test_base_url_must_not_embed_credentials() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        _enabled(base_url="https://user:pass@langfuse.internal.example")


def test_hashed_user_id_requires_a_dedicated_salt() -> None:
    with pytest.raises(ValidationError, match="user_id_salt"):
        _enabled(user_id_salt="short")

    cfg = _enabled(user_id_salt="a" * 16)
    assert cfg.user_id_mode == "hashed"


def test_raw_user_id_mode_does_not_require_a_salt() -> None:
    cfg = _enabled(user_id_mode="raw", user_id_salt="")
    assert cfg.user_id_mode == "raw"


def test_sample_rate_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _enabled(sample_rate=1.1)
    with pytest.raises(ValidationError):
        _enabled(sample_rate=-0.1)


def test_capture_content_is_a_closed_vocabulary() -> None:
    with pytest.raises(ValidationError):
        _enabled(capture_content="everything")


def test_environment_must_be_a_langfuse_environment_name() -> None:
    with pytest.raises(ValidationError, match="environment"):
        _enabled(environment="Production EU")


def test_observability_is_startup_only() -> None:
    assert "observability.langfuse" in STARTUP_ONLY_FIELDS
    description = AppConfig.model_fields["observability"].description or ""
    assert description.startswith(f"{STARTUP_ONLY_PREFIX} ")


def test_config_example_contains_the_remote_section() -> None:
    from pathlib import Path
    import yaml

    path = Path(__file__).resolve().parents[2] / "config.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = data["observability"]["langfuse"]
    assert section["enabled"] is False
    assert section["base_url"] == ""
    assert section["capture_content"] == "redacted"
