"""SDK wrapper maps OpenViking errors into the local session protocol."""

from __future__ import annotations

import pytest

from agentkit.agents.memory.openviking.client import OpenVikingSessionClient
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
