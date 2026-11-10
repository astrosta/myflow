"""Judge configuration and one OpenAI-compatible chat client.

The evaluation package must work with any OpenAI-compatible endpoint: a hosted
provider, a company gateway, or a local server.  Only this module knows the
transport details.  Everything a caller sees is a JudgeConfig whose API key is
excluded from repr and a ChatModel protocol that returns text.

Configuration is intentionally all-or-nothing.  A half-configured judge is a
mistake an operator wants reported, not silently downgraded to rule-only
evaluation, so a partial EVAL_JUDGE_* block raises JudgeError.  No prompt, key,
or provider response body is ever included in an error message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

JUDGE_BASE_URL_ENV = "EVAL_JUDGE_BASE_URL"
JUDGE_API_KEY_ENV = "EVAL_JUDGE_API_KEY"
JUDGE_MODEL_ENV = "EVAL_JUDGE_MODEL"
JUDGE_TEMPERATURE_ENV = "EVAL_JUDGE_TEMPERATURE"
JUDGE_TIMEOUT_ENV = "EVAL_JUDGE_TIMEOUT_SECONDS"
JUDGE_MAX_TOKENS_ENV = "EVAL_JUDGE_MAX_TOKENS"
_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_TOKENS = 2048
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


class JudgeError(RuntimeError):
    """The judge is unusable or returned something a metric cannot parse.

    Messages name the setting or the failure class only.  They never carry an
    API key, a prompt, a response body, or a retrieved chunk.
    """


class ChatModel(Protocol):
    """The one judge capability the metric engines need."""

    def complete(self, *, system: str, prompt: str) -> str: ...


@dataclass(frozen=True)
class JudgeConfig:
    """One OpenAI-compatible judge endpoint; the key is never printed."""

    base_url: str
    api_key: str = field(repr=False)
    model: str
    temperature: float = 0.0
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = _DEFAULT_MAX_TOKENS


class OpenAICompatibleChat:
    """A stateless chat-completions client over one configured endpoint."""

    def __init__(
        self, config: JudgeConfig, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._config = config
        self._transport = transport

    def complete(self, *, system: str, prompt: str) -> str:
        """Send one system+user turn and return the trimmed assistant text."""
        url = _endpoint(self._config.base_url)
        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        try:
            with httpx.Client(
                timeout=self._config.timeout_seconds, transport=self._transport
            ) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise JudgeError(f"judge request failed ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise JudgeError(f"judge request failed with HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise JudgeError("judge returned a non-JSON response") from None
        content = _assistant_content(body)
        if content is None:
            raise JudgeError("judge returned empty content")
        return content


def judge_config_from_env(environ: Mapping[str, str]) -> JudgeConfig | None:
    """Build a judge config from the environment, or None when unset.

    Both EVAL_JUDGE_API_KEY and the conventional OPENAI_API_KEY are accepted as
    the key source; the explicit evaluation variable wins.
    """
    base_url = _value(environ, JUDGE_BASE_URL_ENV)
    model = _value(environ, JUDGE_MODEL_ENV)
    api_key = _value(environ, JUDGE_API_KEY_ENV) or _value(environ, _OPENAI_API_KEY_ENV)
    if not (base_url or model or api_key):
        return None
    missing = [
        name
        for name, value in (
            (JUDGE_BASE_URL_ENV, base_url),
            (JUDGE_MODEL_ENV, model),
            (f"{JUDGE_API_KEY_ENV} or {_OPENAI_API_KEY_ENV}", api_key),
        )
        if not value
    ]
    if missing:
        raise JudgeError(f"incomplete judge configuration; set {', '.join(missing)}")
    return JudgeConfig(
        base_url=base_url,  # type: ignore[arg-type]
        api_key=api_key,  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
        temperature=_float_env(environ, JUDGE_TEMPERATURE_ENV, 0.0),
        timeout_seconds=_float_env(
            environ, JUDGE_TIMEOUT_ENV, _DEFAULT_TIMEOUT_SECONDS
        ),
        max_tokens=_int_env(environ, JUDGE_MAX_TOKENS_ENV, _DEFAULT_MAX_TOKENS),
    )


def model_name(environ: Mapping[str, str]) -> str | None:
    """The configured judge model name, for experiment metadata only."""
    return _value(environ, JUDGE_MODEL_ENV)


def _value(environ: Mapping[str, str], name: str) -> str | None:
    raw = environ.get(name)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _float_env(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = _value(environ, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise JudgeError(f"{name} must be a number") from None
    if value != value or value < 0:
        raise JudgeError(f"{name} must be a non-negative number")
    return value


def _int_env(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = _value(environ, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise JudgeError(f"{name} must be an integer") from None
    if value < 1:
        raise JudgeError(f"{name} must be a positive integer")
    return value


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(_CHAT_COMPLETIONS_SUFFIX):
        return base
    return f"{base}{_CHAT_COMPLETIONS_SUFFIX}"


def _assistant_content(body: Any) -> str | None:
    if not isinstance(body, Mapping):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    return text or None
