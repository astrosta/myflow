"""Speculative sandbox acquisition that never blocks the first model token.

DeerFlow's AIO provider starts one optional acquisition while the model is
generating, then lets the first sandbox-backed tool claim that same result.
This module is the provider-neutral half of that pattern:

- start() submits the normal provider.acquire() to a worker thread;
- the middleware returns immediately, so the model call is not delayed;
- the first tool that needs a sandbox calls acquire(); the provider's per-thread
  lock serializes it behind the speculative acquire and then serves the cache;
- if no tool ever arrives, after_agent closes the handle and releases/parks the
  unused sandbox.

It is deliberately not a "warm container pool". A sandbox is still bound to a
thread at creation; speculation only overlaps that creation with model time.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage

from agentkit.runtime.user_context import DEFAULT_USER_ID, resolve_runtime_user_id
from agentkit.sandbox.lease import lead_lease_owner_id, lease_owner_from_context
from agentkit.sandbox.sandbox_provider import SandboxProvider, get_sandbox_provider

logger = logging.getLogger(__name__)

PREWARM_CONTEXT_KEY = "sandbox_prewarm"

_CODE_FENCE_RE = re.compile(r"```")
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>]+")
_PATH_RE = re.compile(r"(?:^|[\s`\"'(])(?:~/(?:[\w.-]+/)*[\w.-]+|/(?:[\w.-]+)(?:/[\w.-]+)*|[A-Za-z]:\\)")
_COMMAND_RE = re.compile(
    r"(?:^|[\s])(?:\$\s+\S|python(?:3)?\s+(?:-[cmV]\b|\S+\.py\b)|"
    r"(?:pip|pip3)\s+(?:install|list|show|freeze|check)\b|"
    r"git\s+(?:status|diff|log|show|clone|checkout|switch|fetch|pull|push|commit|add|branch)\b|"
    r"(?:npm|pnpm)\s+(?:run|install|test|build|exec|add)\b|"
    r"uv\s+(?:run|sync|pip|add|lock|venv)\b|"
    r"pytest(?:\s+(?:-|\S+[/\\.]|tests\b)|\s*$)|make\s+(?:test|check|build|install|dev)\b|"
    r"docker\s+(?:run|build|compose|ps|logs|exec|inspect)\b|kubectl\s+(?:get|describe|logs|exec|apply)\b)",
    re.MULTILINE,
)
_TRACEBACK_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|^\s*File [\"'].+[\"'], line \d+)",
    re.MULTILINE,
)

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sandbox-prewarm")
_slot_lock = threading.Lock()
_slots: dict[int, tuple[SandboxProvider, int]] = {}


def _take_slot(provider: SandboxProvider, limit: int) -> bool:
    with _slot_lock:
        entry = _slots.get(id(provider))
        count = entry[1] if entry is not None and entry[0] is provider else 0
        if count >= max(1, limit):
            return False
        _slots[id(provider)] = provider, count + 1
        return True


def _return_slot(provider: SandboxProvider) -> None:
    with _slot_lock:
        entry = _slots.get(id(provider))
        if entry is None or entry[0] is not provider:
            return
        if entry[1] <= 1:
            _slots.pop(id(provider), None)
        else:
            _slots[id(provider)] = provider, entry[1] - 1


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    if isinstance(message, dict) and message.get("type") == "human":
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def should_prewarm_sandbox(state: Any) -> bool:
    """Only speculate when the latest user turn looks like it will use tools."""

    messages = state.get("messages") if isinstance(state, dict) else None
    if not isinstance(messages, list):
        return False
    message = next(
        (
            item
            for item in reversed(messages)
            if isinstance(item, HumanMessage)
            or (isinstance(item, dict) and item.get("type") == "human")
        ),
        None,
    )
    if message is None:
        return False
    text = _URL_RE.sub("", _message_text(message))
    return bool(
        _CODE_FENCE_RE.search(text)
        or _PATH_RE.search(text)
        or _COMMAND_RE.search(text)
        or _TRACEBACK_RE.search(text)
    )


@dataclass
class SandboxPrewarm:
    """One speculative provider.acquire running on a worker thread."""

    provider: SandboxProvider
    thread_id: str
    user_id: str
    owner_id: str | None
    _future: Future[str | None] | None = field(default=None, init=False)
    _sandbox_id: str | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def start(self) -> None:
        try:
            self._future = _EXECUTOR.submit(self._acquire)
        except Exception:
            _return_slot(self.provider)
            raise

    def _acquire(self) -> str | None:
        try:
            sandbox_id = self.provider.acquire(self.thread_id, user_id=self.user_id)
            self._sandbox_id = sandbox_id
            logger.info(
                "Sandbox prewarm completed for thread=%s sandbox=%s",
                self.thread_id,
                sandbox_id,
            )
            return sandbox_id
        except Exception:
            logger.info(
                "Sandbox prewarm failed for thread=%s; normal tool acquisition remains authoritative",
                self.thread_id,
                exc_info=True,
            )
            return None
        finally:
            # The budget bounds concurrent container startups, not the lifetime
            # of a cached sandbox. Releasing it here also avoids a permanently
            # occupied slot when a cancelled run never reaches after_agent.
            _return_slot(self.provider)

    def result(self, timeout: float | None = None) -> str | None:
        future = self._future
        if future is None:
            return self._sandbox_id
        try:
            return future.result(timeout=timeout)
        except Exception:
            return self._sandbox_id

    def close_sync(self, *, claimed: bool) -> None:
        """Wait for the speculative acquire, then release it if unused.

        This runs after the model turn, so it cannot delay the first token. It
        does wait for a still-running acquire at teardown; releasing earlier
        would race a tool or the next run that picks up the same cached handle.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            sandbox_id = self.result()
            if sandbox_id is not None and not claimed:
                self.provider.release(sandbox_id)
        except Exception:
            logger.warning("Sandbox prewarm cleanup failed", exc_info=True)

    async def close_async(self, *, claimed: bool) -> None:
        await asyncio.to_thread(self.close_sync, claimed=claimed)


def _runtime_thread_id(runtime: Any) -> str | None:
    context = getattr(runtime, "context", None)
    if isinstance(context, dict) and isinstance(context.get("thread_id"), str):
        return context["thread_id"]
    config = getattr(runtime, "config", None)
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _runtime_user_id(runtime: Any) -> str:
    context = getattr(runtime, "context", None)
    return resolve_runtime_user_id(context, allow_default=True) or DEFAULT_USER_ID


def _active_sandbox_id(runtime: Any) -> str | None:
    """Only a sandbox stamped during this run counts as claimed.

    Checkpoint state can carry a sandbox_id from a previous run whose container
    is gone. Treating that stale id as claimed would make an unused prewarm
    skip its release, so the current runtime context is authoritative here.
    """

    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        value = context.get("sandbox_id")
        if isinstance(value, str) and value:
            return value
    return None


def start_sandbox_prewarm(
    state: Any, runtime: Any, *, provider: SandboxProvider | None = None
) -> SandboxPrewarm | None:
    """Start one speculative acquisition without waiting for it.

    Returns the handle when a worker was started; returns None when the turn is
    text-only, the provider opted out, or the speculative budget is full.
    """

    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        return None
    if PREWARM_CONTEXT_KEY in context or _active_sandbox_id(runtime) is not None:
        return None
    provider = provider or get_sandbox_provider()
    if not getattr(provider, "prewarm_enabled", False):
        return None
    thread_id = _runtime_thread_id(runtime)
    if not thread_id or not should_prewarm_sandbox(state):
        return None
    # A subagent shares the lead thread sandbox and has its own lease owner.
    # Speculating there could acquire an already-warm handle and later release
    # the lead's sandbox from after_agent; let subagent tools acquire lazily.
    if lease_owner_from_context(context) is not None:
        return None
    owner_id = lead_lease_owner_id(thread_id)
    limit = int(getattr(provider, "prewarm_max_concurrent", 1) or 1)
    if not _take_slot(provider, limit):
        logger.debug("Sandbox prewarm skipped: speculative budget occupied")
        return None

    handle = SandboxPrewarm(
        provider=provider,
        thread_id=thread_id,
        user_id=_runtime_user_id(runtime),
        owner_id=owner_id,
    )
    started = time.monotonic()
    try:
        handle.start()
    except Exception:
        logger.warning("Sandbox prewarm could not start", exc_info=True)
        return None
    context[PREWARM_CONTEXT_KEY] = handle
    logger.debug("Sandbox prewarm started for thread=%s in %.3fs", thread_id, time.monotonic() - started)
    return handle


def close_sandbox_prewarm(state: Any, runtime: Any) -> None:
    context = getattr(runtime, "context", None)
    handle = context.pop(PREWARM_CONTEXT_KEY, None) if isinstance(context, dict) else None
    if isinstance(handle, SandboxPrewarm):
        handle.close_sync(claimed=_active_sandbox_id(runtime) is not None)


async def close_sandbox_prewarm_async(state: Any, runtime: Any) -> None:
    context = getattr(runtime, "context", None)
    handle = context.pop(PREWARM_CONTEXT_KEY, None) if isinstance(context, dict) else None
    if isinstance(handle, SandboxPrewarm):
        await handle.close_async(claimed=_active_sandbox_id(runtime) is not None)


__all__ = [
    "PREWARM_CONTEXT_KEY",
    "SandboxPrewarm",
    "close_sandbox_prewarm",
    "close_sandbox_prewarm_async",
    "should_prewarm_sandbox",
    "start_sandbox_prewarm",
]
