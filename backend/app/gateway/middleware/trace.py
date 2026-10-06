"""Assign a trace id before any later middleware or handler runs."""

from __future__ import annotations

from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class TraceMiddleware:
    """Pure ASGI middleware so streaming responses are not buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id = uuid4().hex
        state = scope.setdefault("state", {})
        state["trace_id"] = trace_id

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers.append("X-Trace-Id", trace_id)
            await send(message)

        await self.app(scope, receive, send_with_trace)
