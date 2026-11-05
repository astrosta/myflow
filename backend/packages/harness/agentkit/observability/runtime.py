"""Process-wide Langfuse runtime and per-run trace objects.

This module deliberately owns no agent logic. It only turns a MyFlow run into
one Langfuse trace, gives the lead and isolated subagents their own callback
handlers, and closes the trace even when the run fails or is cancelled.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from functools import partial
import re
import threading
from collections.abc import Mapping
from typing import Any

from langchain_core.callbacks import BaseCallbackManager

from agentkit.config.observability_config import LangfuseConfig
from agentkit.observability.masking import build_mask_otel_spans
from agentkit.trace_context import TRACE_METADATA_KEY

logger = logging.getLogger(__name__)

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_MAX_STATUS_CHARS = 2000


def _valid_trace_id(value: Any) -> str | None:
    if isinstance(value, str) and _TRACE_ID_RE.fullmatch(value):
        return value
    return None


def _valid_span_id(value: Any) -> str | None:
    if isinstance(value, str) and _SPAN_ID_RE.fullmatch(value):
        return value
    return None


def _fallback_trace_id(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


def _safe_tag(value: str) -> str:
    return value if value.isascii() and len(value) <= 200 else ""


def pseudonymize_user_id(user_id: str | None, config: LangfuseConfig) -> str | None:
    if not user_id or config.user_id_mode == "none":
        return None
    if config.user_id_mode == "raw":
        return user_id if user_id.isascii() and len(user_id) <= 200 else None
    if not config.user_id_salt:
        return None
    digest = hmac.new(
        config.user_id_salt.encode("utf-8"),
        user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def _tags(config: LangfuseConfig, model_name: str | None) -> list[str]:
    tags = ["myflow", f"env:{config.environment}"]
    if model_name:
        model_tag = _safe_tag(f"model:{model_name}")
        if model_tag:
            tags.append(model_tag)
    return tags


def attach_callback_handler(
    config: Mapping[str, Any],
    handler: Any,
) -> dict[str, Any]:
    """Return a copy of a RunnableConfig with one more callback handler."""

    merged_config = dict(config)
    existing = merged_config.get("callbacks")

    if handler is None:
        return merged_config
    if isinstance(existing, BaseCallbackManager):
        merged = existing.copy()
        merged.handlers = [*merged.handlers, handler]
        merged.inheritable_handlers = [*merged.inheritable_handlers, handler]
    elif isinstance(existing, (list, tuple)):
        merged = [*existing, handler]
    elif existing is None:
        merged = [handler]
    else:
        merged = [existing, handler]

    merged_config["callbacks"] = merged
    return merged_config


class RunTrace:
    """One run's root span and lead callback handler."""

    def __init__(
        self,
        *,
        enabled: bool,
        config: LangfuseConfig | None = None,
        client: Any = None,
        trace_id: str | None = None,
        root_span_id: str | None = None,
        span: Any = None,
        root_cm: Any = None,
        propagation_cm: Any = None,
        handler: Any = None,
    ) -> None:
        self.enabled = enabled
        self.config = config
        self._client = client
        self.trace_id = trace_id
        self.root_span_id = root_span_id
        self.handler = handler
        self._span = span
        self._root_cm = root_cm
        self._propagation_cm = propagation_cm
        self._finished = False

    @classmethod
    def disabled(cls) -> "RunTrace":
        return cls(enabled=False)

    def finish(self, *, status: str, error: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True

        if self._span is not None:
            try:
                if status == "success":
                    self._span.update(output={"status": status})
                else:
                    level = "WARNING" if status == "interrupted" else "ERROR"
                    message = (error or status)[:_MAX_STATUS_CHARS]
                    self._span.update(
                        level=level,
                        status_message=message,
                        output={"status": status},
                    )
            except Exception:  # noqa: BLE001 - tracing must not affect the run
                logger.warning("Failed to update Langfuse run span", exc_info=True)

        self._exit_cm(self._propagation_cm, "propagation")
        self._exit_cm(self._root_cm, "root")
        self._propagation_cm = None
        self._root_cm = None

    async def aclose(self, *, status: str, error: str | None = None) -> None:
        self.finish(status=status, error=error)
        if self.config is not None and self.config.flush_on_run_end and self._client is not None:
            try:
                await asyncio.to_thread(self._client.flush)
            except Exception:  # noqa: BLE001 - tracing must not affect the run
                logger.warning("Langfuse flush failed", exc_info=True)

    @staticmethod
    def _exit_cm(cm: Any, label: str) -> None:
        if cm is None:
            return
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - tracing must not affect the run
            logger.warning("Failed to close Langfuse %s context", label, exc_info=True)


class SubagentTrace:
    """One isolated subagent's agent span and callback handler."""

    def __init__(
        self,
        *,
        enabled: bool,
        trace_id: str | None = None,
        span_id: str | None = None,
        span: Any = None,
        root_cm: Any = None,
        propagation_cm: Any = None,
        handler: Any = None,
    ) -> None:
        self.enabled = enabled
        self.trace_id = trace_id
        self.root_span_id = span_id
        self.handler = handler
        self._span = span
        self._root_cm = root_cm
        self._propagation_cm = propagation_cm
        self._finished = False

    @classmethod
    def disabled(cls) -> "SubagentTrace":
        return cls(enabled=False)

    def finish(self, *, status: str, error: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        if self._span is not None:
            try:
                if status == "success":
                    self._span.update(output={"status": status})
                else:
                    self._span.update(
                        level="WARNING" if status == "cancelled" else "ERROR",
                        status_message=(error or status)[:_MAX_STATUS_CHARS],
                        output={"status": status},
                    )
            except Exception:  # noqa: BLE001 - tracing must not affect the subagent
                logger.warning("Failed to update Langfuse subagent span", exc_info=True)

        RunTrace._exit_cm(self._propagation_cm, "subagent propagation")
        RunTrace._exit_cm(self._root_cm, "subagent root")
        self._propagation_cm = None
        self._root_cm = None

    async def aclose(self, *, status: str, error: str | None = None) -> None:
        self.finish(status=status, error=error)


class ObservabilityRuntime:
    """Client + handler factory. Constructed once during gateway startup."""

    def __init__(
        self,
        config: LangfuseConfig,
        *,
        client: Any = None,
        handler_factory: Any = None,
        propagate_attributes: Any = None,
    ) -> None:
        self.config = config
        self._client = client
        self._handler_factory = handler_factory
        self._propagate_attributes = propagate_attributes

    @classmethod
    def disabled(cls) -> "ObservabilityRuntime":
        return cls(LangfuseConfig(), client=None)

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.enabled
            and self._client is not None
            and self._handler_factory is not None
        )

    def _client_span(self, **kwargs):
        client = self._client
        if client is None:
            return None, None
        cm = client.start_as_current_observation(**kwargs)
        span = cm.__enter__()
        return cm, span

    def start_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        user_id: str | None,
        model_name: str | None,
        assistant_id: str | None,
        trace_id: str | None,
    ) -> RunTrace:
        if not self.enabled:
            return RunTrace.disabled()

        valid_trace_id = _valid_trace_id(trace_id)
        if valid_trace_id is None:
            if trace_id is not None:
                logger.warning("Skipping Langfuse run trace: invalid trace id")
                return RunTrace.disabled()
            valid_trace_id = _fallback_trace_id(run_id)
        client = self._client
        assert client is not None

        root_cm = None
        propagation_cm = None
        try:
            root_cm, span = self._client_span(
                name="myflow.run",
                as_type="agent",
                trace_context={"trace_id": valid_trace_id},
                metadata={
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "assistant_id": assistant_id or "",
                    "model_name": model_name or "",
                },
            )
            root_span_id = _valid_span_id(client.get_current_observation_id())

            if self._propagate_attributes is not None:
                propagation_cm = self._propagate_attributes(
                    trace_name="myflow.run",
                    session_id=thread_id,
                    user_id=pseudonymize_user_id(user_id, self.config),
                    tags=_tags(self.config, model_name),
                    metadata={"run_id": run_id},
                )
                propagation_cm.__enter__()

            handler = None
            if self._handler_factory is not None:
                trace_context = {"trace_id": valid_trace_id}
                if root_span_id:
                    trace_context["parent_span_id"] = root_span_id
                handler = self._handler_factory(trace_context=trace_context)
                handler.myflow_loop_bound = True

            return RunTrace(
                enabled=True,
                config=self.config,
                client=client,
                trace_id=valid_trace_id,
                root_span_id=root_span_id,
                span=span,
                root_cm=root_cm,
                propagation_cm=propagation_cm,
                handler=handler,
            )
        except Exception:  # noqa: BLE001 - tracing must not affect the run
            logger.warning("Failed to initialize Langfuse run trace", exc_info=True)
            RunTrace._exit_cm(propagation_cm, "propagation")
            RunTrace._exit_cm(root_cm, "root")
            return RunTrace.disabled()

    def start_subagent(
        self,
        *,
        trace_id: str | None,
        parent_span_id: str | None,
        name: str,
        execution_id: str,
        tool_call_id: str | None,
        user_id: str | None,
        thread_id: str | None,
        model_name: str | None,
    ) -> SubagentTrace:
        if not self.enabled or not self.config.trace_subagents:
            return SubagentTrace.disabled()

        valid_trace_id = _valid_trace_id(trace_id)
        if valid_trace_id is None:
            return SubagentTrace.disabled()

        client = self._client
        assert client is not None
        trace_context: dict[str, str] = {"trace_id": valid_trace_id}
        valid_parent = _valid_span_id(parent_span_id)
        if valid_parent:
            trace_context["parent_span_id"] = valid_parent

        root_cm = None
        propagation_cm = None
        try:
            root_cm, span = self._client_span(
                name=f"subagent:{name}",
                as_type="agent",
                trace_context=trace_context,
                metadata={
                    "subagent_name": name,
                    "execution_id": execution_id,
                    "tool_call_id": tool_call_id or "",
                    "model_name": model_name or "",
                },
            )
            span_id = _valid_span_id(client.get_current_observation_id())

            if self._propagate_attributes is not None:
                propagation_cm = self._propagate_attributes(
                    trace_name="myflow.run",
                    session_id=thread_id,
                    user_id=pseudonymize_user_id(user_id, self.config),
                    tags=_tags(self.config, model_name),
                    metadata={"run_id": execution_id},
                )
                propagation_cm.__enter__()

            handler = None
            if self._handler_factory is not None:
                child_context = {"trace_id": valid_trace_id}
                if span_id:
                    child_context["parent_span_id"] = span_id
                handler = self._handler_factory(trace_context=child_context)

            return SubagentTrace(
                enabled=True,
                trace_id=valid_trace_id,
                span_id=span_id,
                span=span,
                root_cm=root_cm,
                propagation_cm=propagation_cm,
                handler=handler,
            )
        except Exception:  # noqa: BLE001 - tracing must not affect the subagent
            logger.warning("Failed to initialize Langfuse subagent trace", exc_info=True)
            RunTrace._exit_cm(propagation_cm, "subagent propagation")
            RunTrace._exit_cm(root_cm, "subagent root")
            return SubagentTrace.disabled()

    def flush(self) -> None:
        if self._client is not None:
            self._client.flush()

    def shutdown(self) -> None:
        client = self._client
        if client is None:
            return
        for method_name in ("flush", "shutdown"):
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                method()
            except Exception:  # noqa: BLE001 - shutdown must not mask app teardown
                logger.warning("Langfuse %s failed", method_name, exc_info=True)


_runtime: ObservabilityRuntime | None = None
_runtime_lock = threading.RLock()


def get_observability() -> ObservabilityRuntime:
    runtime = _runtime
    return runtime if runtime is not None else ObservabilityRuntime.disabled()


def configure_observability(app_config: Any) -> ObservabilityRuntime:
    """Build the process-wide Langfuse client, or a no-op runtime.

    No network probe happens here. A remote Langfuse instance may be down at
    startup and come back later; that must not prevent the agent from serving.
    """

    global _runtime
    config = app_config.observability.langfuse
    with _runtime_lock:
        _runtime = None
        if not config.enabled:
            _runtime = ObservabilityRuntime.disabled()
            return _runtime

        try:
            from langfuse import Langfuse, propagate_attributes
            from langfuse.langchain import CallbackHandler
        except ImportError as exc:
            message = (
                "observability.langfuse.enabled=true requires the optional "
                "langfuse package: cd backend && uv sync --extra observability"
            )
            if config.required:
                raise RuntimeError(message) from exc
            logger.error(message)
            _runtime = ObservabilityRuntime.disabled()
            return _runtime

        try:
            client = Langfuse(
                public_key=config.public_key,
                secret_key=config.secret_key,
                base_url=config.base_url,
                environment=config.environment,
                release=config.release or None,
                timeout=config.timeout_seconds,
                sample_rate=config.sample_rate,
                mask_otel_spans=build_mask_otel_spans(config.capture_content),
            )
        except Exception as exc:  # noqa: BLE001 - optional observability
            if config.required:
                raise RuntimeError("Failed to initialize Langfuse client") from exc
            logger.exception("Failed to initialize Langfuse; tracing is disabled")
            _runtime = ObservabilityRuntime.disabled()
            return _runtime

        _runtime = ObservabilityRuntime(
            config,
            client=client,
            handler_factory=partial(CallbackHandler, public_key=config.public_key),
            propagate_attributes=propagate_attributes,
        )
        logger.info(
            "Langfuse tracing enabled: base_url=%s environment=%s",
            config.base_url,
            config.environment,
        )
        return _runtime


def reset_observability() -> None:
    global _runtime
    with _runtime_lock:
        _runtime = None


def shutdown_observability() -> None:
    global _runtime
    with _runtime_lock:
        runtime = _runtime
        _runtime = None
    if runtime is not None:
        runtime.shutdown()


def start_run_trace(record: Any) -> RunTrace:
    runtime = get_observability()
    trace_id = None
    metadata = getattr(record, "metadata", None)
    if isinstance(metadata, Mapping):
        trace_id = metadata.get(TRACE_METADATA_KEY)
    return runtime.start_run(
        run_id=str(getattr(record, "run_id", "")),
        thread_id=str(getattr(record, "thread_id", "")),
        user_id=getattr(record, "user_id", None),
        model_name=getattr(record, "model_name", None),
        assistant_id=getattr(record, "assistant_id", None),
        trace_id=trace_id if isinstance(trace_id, str) else None,
    )


def start_subagent_trace(
    *,
    trace_id: str | None,
    parent_span_id: str | None,
    name: str,
    execution_id: str,
    tool_call_id: str | None,
    user_id: str | None,
    thread_id: str | None,
    model_name: str | None,
) -> SubagentTrace:
    return get_observability().start_subagent(
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        name=name,
        execution_id=execution_id,
        tool_call_id=tool_call_id,
        user_id=user_id,
        thread_id=thread_id,
        model_name=model_name,
    )


__all__ = [
    "ObservabilityRuntime",
    "RunTrace",
    "SubagentTrace",
    "attach_callback_handler",
    "configure_observability",
    "get_observability",
    "pseudonymize_user_id",
    "reset_observability",
    "shutdown_observability",
    "start_run_trace",
    "start_subagent_trace",
]
