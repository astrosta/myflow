"""Subscribe to ContextForge GET /admin/events SSE.

The event payload is a signal only. Callers must re-read MCP tools/list and
reconcile the tool index; data is never treated as a tool definition.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.models import ContextForgeAdminEvent

logger = logging.getLogger(__name__)

EventCallback = Callable[[ContextForgeAdminEvent], Awaitable[None]]

STATUS_DISABLED = "disabled"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_RECONNECTING = "reconnecting"
STATUS_UNSUPPORTED = "unsupported"
STATUS_AUTH_FAILED = "auth_failed"

_TOOL_CHANGE_EVENTS = frozenset(
    {
        "tool_added",
        "tool_updated",
        "tool_deleted",
        "tool_activated",
        "tool_deactivated",
        "tool_offline",
        "tool_removed",
    }
)


def parse_sse_block(block: str) -> ContextForgeAdminEvent | None:
    """Parse one SSE event block; comments/keepalives return None."""
    event_type = "message"
    data_lines: list[str] = []
    for line in block.splitlines():
        if not line:
            break
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("Ignoring malformed /admin/events payload")
        return None
    data = payload if isinstance(payload, dict) else {"value": payload}
    return ContextForgeAdminEvent(type=event_type, data=data, raw=raw)


def should_refresh_tool_event(event_type: str) -> bool:
    normalized = event_type.strip().lower()
    return normalized in _TOOL_CHANGE_EVENTS or normalized.startswith("gateway_")


class AdminEventSubscriber:
    """One long-lived SSE reader with bounded reconnect backoff."""

    def __init__(
        self,
        config: ContextForgeConfig,
        on_event: EventCallback,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.request_timeout_seconds,
                connect=config.connect_timeout_seconds,
                read=config.admin_events.read_timeout_seconds,
            ),
            verify=config.verify_tls,
            follow_redirects=False,
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._status = STATUS_DISABLED
        self._last_event_at: str | None = None
        self._events_received = 0

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_event_at(self) -> str | None:
        return self._last_event_at

    @property
    def events_received(self) -> int:
        return self._events_received

    async def start(self) -> None:
        if not self._config.enabled or not self._config.admin_events.enabled:
            self._status = STATUS_DISABLED
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="contextforge-admin-events")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._owns_client:
            await self._client.aclose()
        self._status = STATUS_DISABLED

    async def _run(self) -> None:
        delay = self._config.admin_events.reconnect_min_seconds
        while not self._stop.is_set():
            try:
                should_retry = await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect is the policy
                logger.warning("/admin/events stream failed: %s", type(exc).__name__)
                should_retry = True
            if not should_retry or self._stop.is_set():
                return
            self._status = STATUS_RECONNECTING
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            delay = min(
                self._config.admin_events.reconnect_max_seconds,
                max(self._config.admin_events.reconnect_min_seconds, delay * 2),
            )

    async def _stream_once(self) -> bool:
        url = self._config.resolved_admin_events_url()
        headers = {"Accept": "text/event-stream"}
        # No-auth deployments (ALLOW_UNAUTHENTICATED_ADMIN) leave the token
        # empty.  Sending "Bearer " with an empty value is still a malformed
        # credential and can be rejected by the gateway, so omit the header
        # entirely instead of sending a blank one.
        token = self._config.resolved_admin_events_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._status = STATUS_CONNECTING
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == 404:
                self._status = STATUS_UNSUPPORTED
                return False
            if response.status_code in (401, 403):
                self._status = STATUS_AUTH_FAILED
                return False
            response.raise_for_status()
            self._status = STATUS_CONNECTED
            buffer: list[str] = []
            async for line in response.aiter_lines():
                if self._stop.is_set():
                    return False
                if line == "":
                    event = parse_sse_block("\n".join(buffer))
                    buffer = []
                    if event is not None and should_refresh_tool_event(event.type):
                        self._events_received += 1
                        self._last_event_at = event.data.get("timestamp")
                        await self._on_event(event)
                else:
                    buffer.append(line)
            return True


__all__ = [
    "AdminEventSubscriber",
    "STATUS_AUTH_FAILED",
    "STATUS_CONNECTED",
    "STATUS_CONNECTING",
    "STATUS_DISABLED",
    "STATUS_RECONNECTING",
    "STATUS_UNSUPPORTED",
    "parse_sse_block",
    "should_refresh_tool_event",
]
