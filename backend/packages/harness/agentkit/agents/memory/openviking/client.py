"""Thin wrapper around the OpenViking HTTP client.

The wrapper keeps SDK-specific exception names out of the session manager. The
SDK import is lazy so tests and deployments that do not enable the adapter can
still import the package.
"""

from __future__ import annotations

import asyncio
import weakref
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

    async def commit_session(
        self, session_id: str, *, keep_recent_count: int = 0
    ) -> dict[str, Any]:
        try:
            return await self._sdk.commit_session(
                session_id, keep_recent_count=keep_recent_count
            )
        except Exception as exc:
            _raise_mapped(exc)

    async def find(self, **kwargs: Any) -> dict[str, Any]:
        return await self._sdk.find(**kwargs)

    async def read(self, uri: str) -> str:
        return await self._sdk.read(uri)

    async def overview(self, uri: str) -> str:
        return await self._sdk.overview(uri)

    async def abstract(self, uri: str) -> str:
        return await self._sdk.abstract(uri)

    async def ls(self, uri: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._sdk.ls(uri, **kwargs)

    async def write(
        self,
        uri: str,
        content: str,
        *,
        mode: str = "replace",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._sdk.write(uri, content, mode=mode, options=options)

    async def rm(self, uri: str, *, recursive: bool = False) -> None:
        await self._sdk.rm(uri, recursive=recursive)


def _raise_mapped(exc: Exception) -> None:
    code = str(getattr(exc, "code", "") or type(exc).__name__).upper()
    if "NOT_FOUND" in code:
        raise SessionNotFound(str(exc)) from exc
    if "ALREADY_EXISTS" in code:
        raise SessionAlreadyExists(str(exc)) from exc
    raise exc


class OpenVikingClientFactory:
    """Per-user client cache for one process/event loop.

    The factory is not thread-safe by itself; the runtime owns it on the
    gateway loop. It keeps one initialized SDK client per OpenViking user.
    """

    def __init__(self, config: OpenVikingMemoryConfig, *, builder: Any = None) -> None:
        self._config = config
        self._builder = builder or build_async_client
        # The SDK client binds to the loop that created it. Retrieval now also runs
        # on the subagent isolated loop, so the cache is keyed by (loop, ov_user).
        self._clients: weakref.WeakKeyDictionary[
            Any, dict[str, OpenVikingSessionClient]
        ] = weakref.WeakKeyDictionary()
        self._locks: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = (
            weakref.WeakKeyDictionary()
        )

    async def client_for(self, ov_user: str) -> OpenVikingSessionClient:
        loop = asyncio.get_running_loop()
        clients = self._clients.setdefault(loop, {})
        cached = clients.get(ov_user)
        if cached is not None:
            return cached
        # A user can have threads on several shards, so two worker loops may
        # request the same client concurrently. Keep one builder per (loop, user).
        locks = self._locks.setdefault(loop, {})
        lock = locks.setdefault(ov_user, asyncio.Lock())
        async with lock:
            cached = clients.get(ov_user)
            if cached is not None:
                return cached
            sdk = await self._builder(self._config, ov_user=ov_user)
            client = OpenVikingSessionClient(sdk)
            clients[ov_user] = client
            return client

    async def close(self) -> None:
        for clients in list(self._clients.values()):
            for client in list(clients.values()):
                close = getattr(client._sdk, "close", None)
                if close is None:
                    continue
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001 - a foreign loop may already be gone
                    pass
        self._clients.clear()
        self._locks.clear()


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


__all__ = ["OpenVikingClientFactory", "OpenVikingSessionClient", "build_async_client"]
