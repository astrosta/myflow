"""Judge configuration and OpenAI-compatible chat client tests."""

from __future__ import annotations

import json

import httpx
import pytest

from myflow_evals.llm import (
    JudgeConfig,
    JudgeError,
    OpenAICompatibleChat,
    judge_config_from_env,
    model_name,
)

KEY = "judge-secret-key"


def _config(**overrides: object) -> JudgeConfig:
    fields: dict[str, object] = {
        "base_url": "https://judge.test/v1",
        "api_key": KEY,
        "model": "judge-model",
    }
    fields.update(overrides)
    return JudgeConfig(**fields)  # type: ignore[arg-type]


def _chat(handler: object, config: JudgeConfig | None = None) -> OpenAICompatibleChat:
    return OpenAICompatibleChat(
        config or _config(), transport=httpx.MockTransport(handler)  # type: ignore[arg-type]
    )


def test_judge_config_is_absent_when_no_judge_variable_is_set() -> None:
    assert judge_config_from_env({}) is None
    assert model_name({}) is None


def test_judge_config_rejects_a_partial_configuration() -> None:
    with pytest.raises(JudgeError, match="EVAL_JUDGE_MODEL"):
        judge_config_from_env({"EVAL_JUDGE_BASE_URL": "https://judge.test/v1"})
    with pytest.raises(JudgeError, match="EVAL_JUDGE_BASE_URL"):
        judge_config_from_env({"EVAL_JUDGE_API_KEY": KEY, "EVAL_JUDGE_MODEL": "m"})


def test_judge_config_reads_values_and_never_reprs_the_key() -> None:
    config = judge_config_from_env(
        {
            "EVAL_JUDGE_BASE_URL": "https://judge.test/v1",
            "EVAL_JUDGE_API_KEY": KEY,
            "EVAL_JUDGE_MODEL": "judge-model",
            "EVAL_JUDGE_TEMPERATURE": "0",
            "EVAL_JUDGE_TIMEOUT_SECONDS": "12.5",
            "EVAL_JUDGE_MAX_TOKENS": "512",
        }
    )
    assert config == _config(temperature=0.0, timeout_seconds=12.5, max_tokens=512)
    assert KEY not in repr(config)
    assert "judge-secret" not in repr(config)


def test_judge_config_falls_back_to_the_openai_key_alias() -> None:
    config = judge_config_from_env(
        {
            "EVAL_JUDGE_BASE_URL": "https://judge.test/v1",
            "EVAL_JUDGE_MODEL": "judge-model",
            "OPENAI_API_KEY": "openai-key",
        }
    )
    assert config is not None
    assert config.api_key == "openai-key"


def test_judge_config_rejects_invalid_numbers() -> None:
    with pytest.raises(JudgeError, match="EVAL_JUDGE_TEMPERATURE"):
        judge_config_from_env(
            {
                "EVAL_JUDGE_BASE_URL": "https://judge.test/v1",
                "EVAL_JUDGE_API_KEY": KEY,
                "EVAL_JUDGE_MODEL": "judge-model",
                "EVAL_JUDGE_TEMPERATURE": "hot",
            }
        )


def test_complete_posts_a_chat_completion_and_returns_the_content() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  a score  "}}]},
        )

    chat = _chat(handler)
    result = chat.complete(system="be terse", prompt="score this")

    assert result == "a score"
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://judge.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "judge-model"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 2048
    assert body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "score this"},
    ]


def test_complete_keeps_an_explicit_completions_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _chat(
        handler, _config(base_url="https://judge.test/v1/chat/completions")
    ).complete(system="s", prompt="p")
    assert seen == ["https://judge.test/v1/chat/completions"]


def test_complete_reports_an_http_error_without_echoing_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited", "key": KEY})

    with pytest.raises(JudgeError, match="429") as caught:
        _chat(handler).complete(system="s", prompt="p")
    assert KEY not in str(caught.value)
    assert "rate limited" not in str(caught.value)


def test_complete_rejects_non_json_and_empty_content() -> None:
    def non_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    with pytest.raises(JudgeError, match="non-JSON"):
        _chat(non_json).complete(system="s", prompt="p")
    with pytest.raises(JudgeError, match="empty"):
        _chat(empty).complete(system="s", prompt="p")


def test_complete_reports_a_transport_failure_as_a_judge_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(JudgeError, match="ConnectError") as caught:
        _chat(handler).complete(system="s", prompt="p")
    assert KEY not in str(caught.value)


def test_model_name_reports_the_configured_model_or_none() -> None:
    assert model_name({"EVAL_JUDGE_MODEL": "judge-model"}) == "judge-model"
    assert model_name({"EVAL_JUDGE_MODEL": "  "}) is None
