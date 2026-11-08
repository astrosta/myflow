"""ContextForge Admin REST client. This layer never touches the browser."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.models import (
    ContextForgeAuthError,
    ContextForgeConflict,
    ContextForgeError,
    ContextForgeNotFound,
    ContextForgeProtocolError,
    ContextForgeTimeout,
    ContextForgeTool,
    ContextForgeUnavailable,
    ContextForgeValidationError,
    ToolWrite,
    tool_from_wire,
)


class ContextForgeAdminClient:
    """Small, synchronous-in-spirit REST adapter with explicit error mapping."""

    def __init__(
        self,
        config: ContextForgeConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                config.request_timeout_seconds,
                connect=config.connect_timeout_seconds,
            ),
            verify=config.verify_tls,
            follow_redirects=False,
        )

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._config.admin_token:
            headers["Authorization"] = f"Bearer {self._config.admin_token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                self._url(path),
                headers=self._headers(),
                json=json_body,
                params=params,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise ContextForgeTimeout("ContextForge request timed out") from exc
        except httpx.HTTPError as exc:
            raise ContextForgeUnavailable("ContextForge is unreachable") from exc

        if response.status_code < 400:
            return response
        if response.status_code in (401, 403):
            # Keep the upstream status: 401 is a bad/expired token, 403 is a
            # valid identity that lacks tools.read.  The no-auth compose mode
            # used to produce the latter while the UI called it "bad token".
            raise ContextForgeAuthError(
                f"ContextForge rejected the admin credential (HTTP {response.status_code})"
            )
        if response.status_code == 404:
            raise ContextForgeNotFound("ContextForge tool not found")
        if response.status_code == 409:
            raise ContextForgeConflict("ContextForge tool name conflicts")
        if response.status_code in (400, 422):
            raise ContextForgeValidationError(
                _validation_detail(response)
            )
        if response.status_code >= 500:
            raise ContextForgeUnavailable(
                f"ContextForge returned HTTP {response.status_code}"
            )
        raise ContextForgeProtocolError(
            f"unexpected ContextForge response HTTP {response.status_code}"
        )

    async def list_tools(
        self,
        *,
        limit: int = 100,
        skip: int = 0,
        include_inactive: bool = True,
    ) -> list[ContextForgeTool]:
        response = await self._request(
            "GET",
            "/tools",
            params={
                "include_inactive": str(bool(include_inactive)).lower(),
                "limit": int(limit),
                "skip": int(skip),
            },
        )
        return _tools_from_response(response)

    async def get_tool(self, tool_id: str) -> ContextForgeTool:
        response = await self._request("GET", f"/tools/{quote(tool_id, safe='')}")
        return tool_from_wire(_json_object(response))

    async def create_tool(self, data: ToolWrite) -> ContextForgeTool:
        response = await self._request("POST", "/tools", json_body=data.to_create_payload())
        return tool_from_wire(_json_object(response))

    async def update_tool(self, tool_id: str, data: ToolWrite) -> ContextForgeTool:
        response = await self._request(
            "PUT",
            f"/tools/{quote(tool_id, safe='')}",
            json_body=data.to_update_payload(),
        )
        return tool_from_wire(_json_object(response))

    async def delete_tool(self, tool_id: str) -> None:
        await self._request("DELETE", f"/tools/{quote(tool_id, safe='')}")

    async def set_tool_state(self, tool_id: str, enabled: bool) -> ContextForgeTool:
        response = await self._request(
            "POST",
            f"/tools/{quote(tool_id, safe='')}/state",
            params={"activate": str(bool(enabled)).lower()},
        )
        return tool_from_wire(_json_object(response))

    async def health(self) -> dict[str, Any]:
        response = await self._request("GET", "/health")
        return _json_object(response)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _tools_from_response(response: httpx.Response) -> list[ContextForgeTool]:
    payload = _json_value(response)
    if isinstance(payload, list):
        raw_tools = payload
    elif isinstance(payload, dict):
        raw_tools = payload.get("tools", payload.get("data", []))
    else:
        raise ContextForgeProtocolError("tool list has an unsupported shape")
    if not isinstance(raw_tools, list):
        raise ContextForgeProtocolError("tool list is not an array")
    return [tool_from_wire(item) for item in raw_tools if isinstance(item, dict)]


def _json_value(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ContextForgeProtocolError("ContextForge returned invalid JSON") from exc


def _json_object(response: httpx.Response) -> dict[str, Any]:
    payload = _json_value(response)
    if not isinstance(payload, dict):
        raise ContextForgeProtocolError("ContextForge response is not an object")
    return payload


def _validation_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "ContextForge rejected the tool payload"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return f"ContextForge rejected the tool payload: {detail[:300]}"
    return "ContextForge rejected the tool payload"


__all__ = ["ContextForgeAdminClient"]
