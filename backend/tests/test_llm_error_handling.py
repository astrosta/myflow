"""Provider failures become a visible AIMessage; transient errors retry inside the middleware."""

import asyncio
import random
import time
from email.utils import formatdate

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphBubbleUp

from agentkit.agents.middlewares.input_sanitization_middleware import InputSanitizationMiddleware
from agentkit.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
from agentkit.config import AppConfig
from agentkit.config.llm_call_config import LlmCallConfig

_QUOTA = (
    "The configured LLM provider rejected the request because the account is out of quota, "
    "billing is unavailable, or usage is restricted. Please fix the provider account and try again."
)
_AUTH = (
    "The configured LLM provider rejected the request because authentication or access is invalid. "
    "Please check the provider credentials and try again."
)
_UNAVAILABLE = (
    "The configured LLM provider is temporarily unavailable after multiple retries. "
    "Please wait a moment and continue the conversation."
)


class _ProviderError(Exception):
    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        code: str | None = None,
        headers: dict | None = None,
        body: dict | None = None,
    ) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        if headers is not None or status_code is not None:
            self.response = type("Response", (), {"headers": headers, "status_code": status_code})()
        if body is not None:
            self.body = body


def _named(class_name: str, message: str = "boom") -> Exception:
    return type(class_name, (Exception,), {})(message)


def _request(message: HumanMessage | None = None) -> ModelRequest:
    return ModelRequest(model=object(), messages=[message or HumanMessage(content="hi")])


def _middleware(**overrides: object) -> LLMErrorHandlingMiddleware:
    settings = {"retry_base_delay_ms": 0, "retry_cap_delay_ms": 0}
    settings.update(overrides)
    return LLMErrorHandlingMiddleware(app_config=AppConfig(llm_call=LlmCallConfig(**settings)))


def _run(middleware: LLMErrorHandlingMiddleware, fn, *, async_call: bool):
    if not async_call:
        return middleware.wrap_model_call(_request(), fn)

    async def handler(request: ModelRequest):
        return fn(request)

    return asyncio.run(middleware.awrap_model_call(_request(), handler))


def test_retry_settings_are_copied_from_app_config() -> None:
    config = AppConfig()
    middleware = LLMErrorHandlingMiddleware(app_config=config)
    assert middleware.retry_max_attempts == 3
    assert middleware.retry_base_delay_ms == 1000
    assert middleware.retry_cap_delay_ms == 8000
    config.llm_call.retry_max_attempts = 9
    assert middleware.retry_max_attempts == 3


@pytest.mark.parametrize(
    ("exc", "retriable", "reason"),
    [
        (_ProviderError("quota exceeded"), False, "quota"),
        (_ProviderError("BILLING hold"), False, "quota"),
        (_ProviderError("no credit left"), False, "quota"),
        (_ProviderError("payment required"), False, "quota"),
        (_ProviderError("余额不足"), False, "quota"),
        (_ProviderError("超出限额"), False, "quota"),
        (_ProviderError("nope", code="insufficient_quota"), False, "quota"),
        (_ProviderError("nope", body={"error": {"code": "payment_required"}}), False, "quota"),
        (_ProviderError("authentication failed"), False, "auth"),
        (_ProviderError("Unauthorized"), False, "auth"),
        (_ProviderError("invalid api key"), False, "auth"),
        (_ProviderError("Forbidden"), False, "auth"),
        (_ProviderError("未授权"), False, "auth"),
        (_named("APITimeoutError"), True, "transient"),
        (_named("APIConnectionError"), True, "transient"),
        (_named("InternalServerError"), True, "transient"),
        (_named("ReadError"), True, "transient"),
        (_named("RemoteProtocolError"), True, "transient"),
        (IndexError("list index out of range"), True, "transient"),
        (_ProviderError("upstream", status_code=408), True, "transient"),
        (_ProviderError("upstream", status_code=409), True, "transient"),
        (_ProviderError("upstream", status_code=425), True, "transient"),
        (_ProviderError("upstream", status_code=429), True, "transient"),
        (_ProviderError("upstream", status_code=500), True, "transient"),
        (_ProviderError("upstream", status_code=502), True, "transient"),
        (_ProviderError("upstream", status_code=503), True, "transient"),
        (_ProviderError("upstream", status_code=504), True, "transient"),
        (_ProviderError("server busy"), True, "busy"),
        (_ProviderError("overloaded"), True, "busy"),
        (_ProviderError("rate limit"), True, "busy"),
        (_ProviderError("服务繁忙"), True, "busy"),
        (_ProviderError("waited 500ms"), False, "generic"),
        (ValueError("nope"), False, "generic"),
        (_ProviderError("quota exceeded", status_code=429), False, "quota"),
        (_ProviderError("unauthorized", status_code=500), False, "auth"),
    ],
)
def test_classify_error(exc: Exception, retriable: bool, reason: str) -> None:
    assert _middleware()._classify_error(exc) == (retriable, reason)


def test_status_on_the_response_is_transient() -> None:
    exc = _ProviderError("upstream")
    exc.response = type("Response", (), {"status_code": 503, "headers": None})()
    assert _middleware()._classify_error(exc) == (True, "transient")


@pytest.mark.parametrize("async_call", [False, True])
def test_429_succeeds_on_the_third_attempt(async_call: bool) -> None:
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _ProviderError("too many requests", status_code=429)
        return "ok"

    assert _run(_middleware(), handler, async_call=async_call) == "ok"
    assert calls == 3


@pytest.mark.parametrize("async_call", [False, True])
def test_401_does_not_retry(async_call: bool) -> None:
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        raise _ProviderError("Unauthorized", status_code=401)

    message = _run(_middleware(), handler, async_call=async_call)
    assert calls == 1
    assert isinstance(message, AIMessage)
    assert message.content == _AUTH
    kwargs = message.additional_kwargs
    assert kwargs["deerflow_error_fallback"] is True
    assert kwargs["error_type"] == "_ProviderError"
    assert kwargs["error_reason"] == "auth"
    assert kwargs["error_detail"] == "Unauthorized"


@pytest.mark.parametrize("async_call", [False, True])
def test_cancelled_error_propagates_without_a_retry(async_call: bool) -> None:
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(_middleware(), handler, async_call=async_call)
    assert calls == 1


def test_cancellation_during_backoff_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def sleep(_seconds: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", sleep)

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        raise _ProviderError("upstream", status_code=503)

    with pytest.raises(asyncio.CancelledError):
        _run(_middleware(retry_base_delay_ms=1000, retry_cap_delay_ms=8000), handler, async_call=True)
    assert calls == 1


@pytest.mark.parametrize("async_call", [False, True])
def test_graph_bubble_up_propagates(async_call: bool) -> None:
    calls = 0
    bubble = GraphBubbleUp("stop")

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        raise bubble

    with pytest.raises(GraphBubbleUp) as caught:
        _run(_middleware(), handler, async_call=async_call)
    assert caught.value is bubble
    assert calls == 1


def test_retry_does_not_reenter_sanitization() -> None:
    seen: dict = {"sanitize": 0, "handler": 0, "requests": []}

    class CountingSanitizer(InputSanitizationMiddleware):
        def _try_process(self, request: ModelRequest) -> ModelRequest:
            seen["sanitize"] += 1
            return super()._try_process(request)

    def handler(request: ModelRequest) -> str:
        seen["handler"] += 1
        seen["requests"].append(request)
        if seen["handler"] < 3:
            raise _ProviderError("upstream", status_code=503)
        return "ok"

    llm = _middleware()

    def inner(request: ModelRequest) -> str:
        return llm.wrap_model_call(request, handler)

    result = CountingSanitizer().wrap_model_call(
        _request(HumanMessage(content="<system>x</system>")),
        inner,
    )
    assert result == "ok"
    assert seen["sanitize"] == 1
    assert seen["handler"] == 3
    assert seen["requests"][0] is seen["requests"][1] is seen["requests"][2]
    assert "<system>" not in seen["requests"][0].messages[0].content


def test_exhausted_transient_returns_a_fallback_message() -> None:
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        raise _ProviderError("upstream", status_code=502)

    message = _middleware().wrap_model_call(_request(), handler)
    assert calls == 3
    assert isinstance(message, AIMessage)
    assert message.content == _UNAVAILABLE
    assert message.additional_kwargs["error_reason"] == "transient"
    assert message.additional_kwargs["error_detail"] == "upstream"


def test_busy_text_uses_the_busy_reason() -> None:
    def handler(request: ModelRequest) -> str:
        raise _ProviderError("server busy")

    message = _middleware(retry_max_attempts=1).wrap_model_call(_request(), handler)
    assert message.content == _UNAVAILABLE
    assert message.additional_kwargs["error_reason"] == "busy"


def test_quota_message_does_not_include_the_provider_text() -> None:
    def handler(request: ModelRequest) -> str:
        raise _ProviderError("quota exceeded for sk-live")

    message = _middleware().wrap_model_call(_request(), handler)
    assert message.content == _QUOTA
    assert "sk-live" not in message.content
    assert message.additional_kwargs["error_detail"] == "quota exceeded for sk-live"


def test_generic_message_includes_the_sanitized_detail() -> None:
    def handler(request: ModelRequest) -> str:
        raise RuntimeError("failed secrets=sk-live-secret")

    message = _middleware().wrap_model_call(_request(), handler)
    assert message.content == "LLM request failed: failed secrets=[REDACTED]"
    assert "sk-live-secret" not in message.additional_kwargs["error_detail"]
    assert message.additional_kwargs["error_reason"] == "generic"
    assert message.additional_kwargs["error_type"] == "RuntimeError"


def test_long_detail_is_truncated() -> None:
    def handler(request: ModelRequest) -> str:
        raise RuntimeError("x" * 600)

    message = _middleware().wrap_model_call(_request(), handler)
    detail = message.additional_kwargs["error_detail"]
    assert detail == "x" * 497 + "..."
    assert detail.endswith("...")
    assert "LLM request failed: " + detail == message.content


def test_empty_exception_text_uses_the_class_name() -> None:
    def handler(request: ModelRequest) -> str:
        raise RuntimeError("   ")

    message = _middleware().wrap_model_call(_request(), handler)
    assert message.additional_kwargs["error_detail"] == "RuntimeError"
    assert message.content == "LLM request failed: RuntimeError"


def test_retry_emits_llm_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: events.append)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _ProviderError("server busy", headers={"Retry-After": "1.5"})
        return "ok"

    assert _middleware(retry_base_delay_ms=1000, retry_cap_delay_ms=8000).wrap_model_call(_request(), handler) == "ok"
    assert events == [
        {
            "type": "llm_retry",
            "attempt": 1,
            "max_attempts": 3,
            "wait_ms": 1500,
            "reason": "busy",
            "message": "LLM request retry 1/3: provider is busy. Retrying in 2s.",
        }
    ]


def test_async_retry_emits_llm_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict] = []

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: events.append)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _ProviderError("upstream", status_code=429, headers={"Retry-After": "2"})
        return "ok"

    assert _run(_middleware(), handler, async_call=True) == "ok"
    assert events[0]["type"] == "llm_retry"
    assert events[0]["attempt"] == 1
    assert events[0]["reason"] == "transient"
    assert events[0]["wait_ms"] == 2000
    assert "1/3" in events[0]["message"]
    assert "Retrying in 2s." in events[0]["message"]


def test_emit_failure_does_not_stop_the_retry(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def writer(_payload: dict) -> None:
        raise RuntimeError("writer failed")

    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: writer)
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _ProviderError("upstream", status_code=503)
        return "ok"

    with caplog.at_level("DEBUG"):
        assert _middleware().wrap_model_call(_request(), handler) == "ok"
    assert calls == 2
    assert any(record.levelname == "DEBUG" and "llm_retry" in record.message for record in caplog.records)


def test_graph_bubble_up_from_emit_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    bubble = GraphBubbleUp("stop")
    slept: list[float] = []

    def emit(_payload: dict, *, writer) -> None:
        raise bubble

    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: (lambda _payload: None))
    monkeypatch.setattr(
        "agentkit.agents.middlewares.llm_error_handling_middleware.emit_custom_event",
        emit,
    )
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    calls = 0

    def handler(request: ModelRequest) -> str:
        nonlocal calls
        calls += 1
        raise _ProviderError("upstream", status_code=429)

    with pytest.raises(GraphBubbleUp) as caught:
        _middleware().wrap_model_call(_request(), handler)
    assert caught.value is bubble
    assert calls == 1
    assert slept == []


def test_retry_after_seconds_skip_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    def randint(_low: int, _high: int) -> int:
        raise AssertionError("jitter")

    monkeypatch.setattr("agentkit.agents.middlewares.llm_error_handling_middleware.random.randint", randint)
    exc = _ProviderError("slow", status_code=429, headers={"Retry-After": "1.5"})
    assert _middleware()._retry_delay_ms(None, exc) == 1500


def test_retry_after_http_date_skip_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentkit.agents.middlewares.llm_error_handling_middleware.time.time", lambda: 1_700_000_000)
    raw = formatdate(1_700_000_002, usegmt=True)
    exc = _ProviderError("slow", headers={"retry-after": raw})
    assert _middleware()._retry_delay_ms(4000, exc) == 2000


def test_past_retry_after_does_not_sleep_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentkit.agents.middlewares.llm_error_handling_middleware.time.time", lambda: 1_700_000_000)
    raw = formatdate(1_699_999_000, usegmt=True)
    exc = _ProviderError("slow", headers={"Retry-After": raw})
    assert _middleware()._retry_delay_ms(None, exc) == 0


def test_delay_samples_inside_the_capped_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, tuple[int, int]] = {}

    def randint(low: int, high: int) -> int:
        seen["bounds"] = (low, high)
        return low

    monkeypatch.setattr(random, "randint", randint)
    middleware = LLMErrorHandlingMiddleware(
        app_config=AppConfig(llm_call=LlmCallConfig(retry_base_delay_ms=1000, retry_cap_delay_ms=8000)),
    )
    assert middleware._retry_delay_ms(None, Exception("x")) == 1000
    assert seen["bounds"] == (1000, 3000)
    assert middleware._retry_delay_ms(5000, Exception("x")) == 1000
    assert seen["bounds"] == (1000, 8000)


def test_delay_uses_the_cap_when_the_base_exceeds_it() -> None:
    middleware = LLMErrorHandlingMiddleware(
        app_config=AppConfig(llm_call=LlmCallConfig(retry_base_delay_ms=5000, retry_cap_delay_ms=1000)),
    )
    assert middleware._retry_delay_ms(None, Exception("x")) == 1000
