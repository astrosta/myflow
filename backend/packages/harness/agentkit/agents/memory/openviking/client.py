"""Thin wrapper around the OpenViking HTTP client.

The wrapper keeps SDK-specific exception names out of the session manager. The
SDK import is lazy so tests and deployments that do not enable the adapter can
still import the package.
"""

from __future__ import annotations

from typing import Any

from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from agentkit.agents.memory.openviking.session import SessionAlreadyExists, SessionNotFound


class OpenVikingSessionClient:
    def __init__(self, sdk_client: Any) -> None:
        self._sdk = sdk_client

    async def get_session(self, session_id: str, *, auto_create: bool = False) -> dict[str, Any]:
        try:
            return await self._sdk.get_session(session_id, auto_create=auto_create)
        except Exception as exc:
            _raise_mapped(exc)

    async def create_session(self, session_id: str, *, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._sdk.create_session(session_id, options=options)
        except Exception as exc:
            _raise_mapped(exc)

    async def update_session_config(
        self, session_id: str, *, options: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return await self._sdk.update_session_config(session_id, options=options)
        except Exception as exc:
            _raise_mapped(exc)

    async def delete_session(self, session_id: str) -> None:
        try:
            await self._sdk.delete_session(session_id)
        except Exception as exc:
            _raise_mapped(exc)

    async def batch_add_messages(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            return await self._sdk.batch_add_messages(session_id, messages)
        except Exception as exc:
            _raise_mapped(exc)


def _raise_mapped(exc: Exception) -> None:
    code = str(getattr(exc, "code", "") or type(exc).__name__).upper()
    if "NOT_FOUND" in code:
        raise SessionNotFound(str(exc)) from exc
    if "ALREADY_EXISTS" in code:
        raise SessionAlreadyExists(str(exc)) from exc
    raise exc


async def build_async_client(config: OpenVikingMemoryConfig, *, ov_user: str) -> Any:
    """Build and initialize an SDK client for one user.

    The import is intentionally lazy: openviking-sdk is an optional runtime
    dependency, and unit tests use the local protocol fakes instead.
    """

    try:
        from openviking_sdk import AsyncHTTPClient
    except ImportError as exc:
        raise RuntimeError(
            "openviking-sdk is not installed; install the memory/openviking extra"
        ) from exc
    client = AsyncHTTPClient(
        url=config.url,
        api_key=config.root_api_key,
        account=config.account,
        user=ov_user,
        timeout=config.timeout_seconds,
    )
    await client.initialize()
    return client


__all__ = ["OpenVikingSessionClient", "build_async_client"]
