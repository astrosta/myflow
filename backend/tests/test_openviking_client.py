"""SDK wrapper maps OpenViking errors into the local session protocol."""

from __future__ import annotations

import asyncio

import pytest

from agentkit.agents.memory.openviking.client import OpenVikingClientFactory, OpenVikingSessionClient
from agentkit.agents.memory.openviking.session import SessionAlreadyExists, SessionNotFound


class FakeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FakeSDK:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail: str | None = None

    def _maybe_fail(self) -> None:
        if self.fail:
            raise FakeError(self.fail)

    async def get_session(self, session_id: str, *, auto_create: bool = False) -> dict:
        self.calls.append(("get", session_id, auto_create))
        self._maybe_fail()
        return {"session_id": session_id}

    async def create_session(self, session_id: str, *, options: dict) -> dict:
        self.calls.append(("create", session_id, options))
        self._maybe_fail()
        return {"session_id": session_id, "options": options}

    async def update_session_config(self, session_id: str, *, options: dict) -> dict:
        self.calls.append(("update", session_id, options))
        self._maybe_fail()
        return {"session_id": session_id}

    async def delete_session(self, session_id: str) -> None:
        self.calls.append(("delete", session_id))
        self._maybe_fail()

    async def batch_add_messages(self, session_id: str, messages: list[dict]) -> dict:
        self.calls.append(("batch", session_id, messages))
        self._maybe_fail()
        return {"messages": [{"id": "ov-1"}]}

    async def commit_session(self, session_id: str, *, keep_recent_count: int = 0) -> dict:
        self.calls.append(("commit", session_id, keep_recent_count))
        self._maybe_fail()
        return {"session_id": session_id}

    async def find(self, **kwargs) -> dict:
        self.calls.append(("find", kwargs))
        return {"memories": []}

    async def read(self, uri: str) -> str:
        self.calls.append(("read", uri))
        return "body"

    async def overview(self, uri: str) -> str:
        self.calls.append(("overview", uri))
        return "overview"


@pytest.mark.asyncio
async def test_wrapper_maps_not_found_and_already_exists() -> None:
    sdk = FakeSDK()
    client = OpenVikingSessionClient(sdk)
    sdk.fail = "NOT_FOUND"
    with pytest.raises(SessionNotFound):
        await client.get_session("s1", auto_create=False)
    sdk.fail = "ALREADY_EXISTS"
    with pytest.raises(SessionAlreadyExists):
        await client.create_session("s1", options={})
    sdk.fail = None
    await client.batch_add_messages("s1", [{"role": "user", "parts": []}])
    assert sdk.calls[-1][0] == "batch"
    await client.commit_session("s1")
    assert sdk.calls[-1] == ("commit", "s1", 0)
    await client.find(query="q", target_uri="viking://user/u/memories/preferences", limit=5)
    assert sdk.calls[-1][0] == "find"
    await client.read("viking://user/u/memories/preferences/a.md")
    await client.overview("viking://user/u/memories/preferences/a.md")
    assert sdk.calls[-1][0] == "overview"


@pytest.mark.asyncio
async def test_factory_caches_per_user() -> None:
    from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig

    built: list[str] = []

    async def builder(config, *, ov_user):
        built.append(ov_user)
        return FakeSDK()

    factory = OpenVikingClientFactory(
        OpenVikingMemoryConfig(user_id_salt="s" * 32),
        builder=builder,
    )
    first = await factory.client_for("u1")
    second = await factory.client_for("u1")
    third = await factory.client_for("u2")
    assert first is second
    assert first is not third
    assert built == ["u1", "u2"]
    await factory.close()


@pytest.mark.asyncio
async def test_factory_builds_one_client_under_concurrent_first_use() -> None:
    from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig

    built: list[str] = []

    async def builder(config, *, ov_user):
        built.append(ov_user)
        await asyncio.sleep(0)
        return FakeSDK()

    factory = OpenVikingClientFactory(
        OpenVikingMemoryConfig(user_id_salt="s" * 32),
        builder=builder,
    )

    clients = await asyncio.gather(*(factory.client_for("u1") for _ in range(8)))

    assert len({id(client) for client in clients}) == 1
    assert built == ["u1"]
    await factory.close()
