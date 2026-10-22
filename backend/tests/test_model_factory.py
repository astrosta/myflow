from __future__ import annotations

import logging
from typing import Any, ClassVar

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai.chat_models.base import BaseChatOpenAI

import agentkit.models.factory as factory_mod
from agentkit.config import AppConfig, ModelConfig
from agentkit.models.factory import (
    _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS,
    create_chat_model,
)

METADATA_KEYS = (
    "display_name",
    "supports_thinking",
    "supports_reasoning_effort",
    "supports_vision",
    "context_window",
    "pricing",
)


class RecordingChatModel(BaseChatModel):
    last_init_kwargs: ClassVar[dict[str, Any]] = {}
    instances: ClassVar[list[RecordingChatModel]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_init_kwargs = dict(kwargs)
        type(self).instances.append(self)
        known = {k: v for k, v in kwargs.items() if k in self.__class__.model_fields}
        if "profile" not in known:
            known["profile"] = {"tool_calling": True, "structured_output": True}
        super().__init__(**known)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    @property
    def _llm_type(self) -> str:
        return "recording"


class RecordingOpenAI(BaseChatOpenAI):
    last_init_kwargs: ClassVar[dict[str, Any]] = {}
    instances: ClassVar[list[RecordingOpenAI]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_init_kwargs = dict(kwargs)
        type(self).instances.append(self)


@pytest.fixture
def install_model_class(monkeypatch):
    def _install(cls):
        cls.last_init_kwargs = {}
        cls.instances = []
        monkeypatch.setattr(
            factory_mod,
            "resolve_class",
            lambda *args, **kwargs: cls,
        )
        return cls

    return _install


def make_app_config(**fields) -> AppConfig:
    payload = {
        "name": "test-model",
        "use": "tests.test_model_factory:RecordingChatModel",
        "model": "fake-model",
        "api_key": "sk-test",
    }
    payload.update(fields)
    return AppConfig(models=[ModelConfig.model_validate(payload)])


def build_model(install_model_class, cls=RecordingChatModel, **create_kwargs):
    install_model_class(cls)
    app_config = create_kwargs.pop("app_config", None)
    if app_config is None:
        app_config = make_app_config()
    return create_chat_model(app_config=app_config, **create_kwargs)


def test_metadata_fields_are_not_passed_to_constructor(install_model_class):
    build_model(
        install_model_class,
        app_config=make_app_config(
            display_name="Pretty",
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=True,
            context_window=8192,
            pricing={"input": 1, "output": 2},
            thinking={"type": "enabled"},
            when_thinking_enabled={"extra_body": {"thinking": {"type": "enabled"}}},
        ),
    )
    kwargs = RecordingChatModel.last_init_kwargs
    for key in METADATA_KEYS:
        assert key not in kwargs


def test_reasoning_effort_is_passed_only_when_the_model_publishes_levels(install_model_class):
    build_model(
        install_model_class,
        app_config=make_app_config(
            supports_reasoning_effort=True, reasoning_efforts=["low", "high"]
        ),
        reasoning_effort="high",
    )
    assert RecordingChatModel.last_init_kwargs["reasoning_effort"] == "high"

    build_model(install_model_class, reasoning_effort="high")
    assert "reasoning_effort" not in RecordingChatModel.last_init_kwargs


def test_reasoning_efforts_stay_metadata_only(install_model_class):
    build_model(
        install_model_class,
        app_config=make_app_config(reasoning_efforts=["low", {"id": "high", "label": "高"}]),
    )
    kwargs = RecordingChatModel.last_init_kwargs
    assert "reasoning_efforts" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_model_overrides_ignore_none_then_later_step_can_drop_them(install_model_class, monkeypatch):
    snapshots: list[dict[str, Any]] = []
    original = factory_mod._apply_stream_chunk_timeout_default

    def spy(model_class, settings):
        snapshots.append(dict(settings))
        original(model_class, settings)
        snapshots.append(dict(settings))

    monkeypatch.setattr(factory_mod, "_apply_stream_chunk_timeout_default", spy)
    install_model_class(RecordingChatModel)
    create_chat_model(
        app_config=make_app_config(max_tokens=100, stream_chunk_timeout=99, temperature=0.2),
        model_overrides={
            "max_tokens": None,
            "stream_chunk_timeout": 12,
            "temperature": None,
        },
    )

    before, after = snapshots
    assert before["stream_chunk_timeout"] == 12
    assert before["max_tokens"] == 100
    assert before["temperature"] == 0.2
    assert "stream_chunk_timeout" not in after
    assert "stream_chunk_timeout" not in RecordingChatModel.last_init_kwargs
    assert RecordingChatModel.last_init_kwargs["max_tokens"] == 100
    assert RecordingChatModel.last_init_kwargs["temperature"] == 0.2


def test_thinking_off_explicit_when_thinking_disabled(install_model_class):
    build_model(
        install_model_class,
        thinking_enabled=False,
        app_config=make_app_config(
            supports_thinking=True,
            when_thinking_enabled={"extra_body": {"thinking": {"type": "enabled"}}},
            when_thinking_disabled={"disabled_flag": True},
        ),
    )
    kwargs = RecordingChatModel.last_init_kwargs
    assert kwargs["disabled_flag"] is True
    assert (kwargs.get("extra_body") or {}).get("thinking") != {"type": "disabled"}


def test_thinking_off_extra_body_dialect(install_model_class):
    build_model(
        install_model_class,
        thinking_enabled=False,
        app_config=make_app_config(
            supports_thinking=True,
            extra_body={"keep_me": 1},
            supports_reasoning_effort=True,
            when_thinking_enabled={"extra_body": {"thinking": {"type": "enabled"}}},
        ),
    )
    kwargs = RecordingChatModel.last_init_kwargs
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert kwargs["reasoning_effort"] == "minimal"


def test_thinking_off_vllm_chat_template_kwargs_dialect(install_model_class):
    build_model(
        install_model_class,
        thinking_enabled=False,
        app_config=make_app_config(
            supports_thinking=True,
            when_thinking_enabled={
                "extra_body": {
                    "chat_template_kwargs": {"thinking": True, "enable_thinking": True},
                }
            },
        ),
    )
    kwargs = RecordingChatModel.last_init_kwargs
    assert kwargs["extra_body"]["chat_template_kwargs"] == {
        "thinking": False,
        "enable_thinking": False,
    }


def test_thinking_off_native_thinking_dialect(install_model_class):
    build_model(
        install_model_class,
        thinking_enabled=False,
        app_config=make_app_config(
            supports_thinking=True,
            thinking={"type": "enabled", "budget": 8},
        ),
    )
    kwargs = RecordingChatModel.last_init_kwargs
    assert kwargs["thinking"] == {"type": "disabled"}


def test_thinking_off_does_nothing_without_thinking_config(install_model_class):
    build_model(install_model_class, thinking_enabled=False, app_config=make_app_config())
    kwargs = RecordingChatModel.last_init_kwargs
    extra_body = kwargs.get("extra_body") or {}
    assert extra_body.get("thinking") != {"type": "disabled"}
    assert "chat_template_kwargs" not in extra_body
    assert kwargs.get("thinking") != {"type": "disabled"}
    assert "disabled_flag" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_thinking_off_extra_body_deep_merge_keeps_sibling_keys(install_model_class):
    build_model(
        install_model_class,
        thinking_enabled=False,
        app_config=make_app_config(
            supports_thinking=True,
            extra_body={"gateway_flag": True, "nested": {"a": 1}},
            when_thinking_enabled={"extra_body": {"thinking": {"type": "enabled"}}},
        ),
    )
    extra_body = RecordingChatModel.last_init_kwargs["extra_body"]
    assert extra_body["gateway_flag"] is True
    assert extra_body["nested"] == {"a": 1}
    assert extra_body["thinking"] == {"type": "disabled"}


def test_thinking_shortcut_overrides_when_thinking_enabled_on_overlap(install_model_class):
    build_model(
        install_model_class,
        thinking_enabled=True,
        app_config=make_app_config(
            supports_thinking=True,
            when_thinking_enabled={"thinking": {"type": "enabled", "budget": 1, "from_wte": True}},
            thinking={"budget": 9, "from_shortcut": True},
        ),
    )
    assert RecordingChatModel.last_init_kwargs["thinking"] == {
        "type": "enabled",
        "budget": 9,
        "from_wte": True,
        "from_shortcut": True,
    }


def test_thinking_enabled_raises_when_unsupported(install_model_class):
    install_model_class(RecordingChatModel)
    with pytest.raises(ValueError, match="does not support thinking"):
        create_chat_model(
            thinking_enabled=True,
            app_config=make_app_config(
                supports_thinking=False,
                thinking={"type": "enabled"},
            ),
        )


def test_reasoning_effort_cleared_from_kwargs_and_settings_when_unsupported(install_model_class):
    build_model(
        install_model_class,
        app_config=make_app_config(
            supports_reasoning_effort=False,
            reasoning_effort="high",
        ),
        reasoning_effort="low",
    )
    kwargs = RecordingChatModel.last_init_kwargs
    assert "reasoning_effort" not in kwargs


def test_stream_chunk_timeout_openai_default_and_explicit(install_model_class):
    build_model(
        install_model_class,
        RecordingOpenAI,
        app_config=make_app_config(model="gpt-4"),
    )
    assert RecordingOpenAI.last_init_kwargs["stream_chunk_timeout"] == _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS

    build_model(
        install_model_class,
        RecordingOpenAI,
        app_config=make_app_config(model="gpt-4", stream_chunk_timeout=12),
    )
    assert RecordingOpenAI.last_init_kwargs["stream_chunk_timeout"] == 12


def test_stream_chunk_timeout_stripped_for_non_openai(install_model_class):
    build_model(
        install_model_class,
        app_config=make_app_config(stream_chunk_timeout=12),
    )
    assert "stream_chunk_timeout" not in RecordingChatModel.last_init_kwargs


def test_unknown_key_warning_only_for_openai_family(install_model_class, caplog):
    with caplog.at_level(logging.WARNING, logger="agentkit.models.factory"):
        build_model(
            install_model_class,
            RecordingOpenAI,
            app_config=make_app_config(model="gpt-4", maxx_tokens=16),
        )
    assert "maxx_tokens" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agentkit.models.factory"):
        build_model(
            install_model_class,
            app_config=make_app_config(maxx_tokens=16),
        )
    assert "maxx_tokens" not in caplog.text


def test_stream_usage_defaults_true_unless_kwargs_set_it(install_model_class):
    build_model(
        install_model_class,
        RecordingOpenAI,
        app_config=make_app_config(model="gpt-4"),
    )
    assert RecordingOpenAI.last_init_kwargs["stream_usage"] is True

    build_model(
        install_model_class,
        RecordingOpenAI,
        app_config=make_app_config(model="gpt-4"),
        stream_usage=False,
    )
    assert RecordingOpenAI.last_init_kwargs["stream_usage"] is False


def test_context_window_merges_into_profile_after_construct(install_model_class):
    model = build_model(
        install_model_class,
        app_config=make_app_config(context_window=4096),
    )
    assert "profile" not in RecordingChatModel.last_init_kwargs
    assert model.profile["max_input_tokens"] == 4096
    assert model.profile["tool_calling"] is True
    assert model.profile["structured_output"] is True


def test_create_chat_model_returns_new_instance_each_call(install_model_class):
    install_model_class(RecordingChatModel)
    app_config = make_app_config()
    first = create_chat_model(app_config=app_config)
    second = create_chat_model(app_config=app_config)
    assert first is not second
    assert len(RecordingChatModel.instances) == 2
