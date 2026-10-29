"""Session lifecycle for the OpenViking memory adapter.

A session is always created explicitly. The only supported new-session path is
get(auto_create=False) -> create(options=...). This keeps the 8-type memory
allowlist and the idle-only auto-commit policy on every new session.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any, Protocol

from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from agentkit.agents.memory.openviking.contracts import SessionRef
from agentkit.agents.memory.openviking.store import MemorySyncStore


class SessionNotFound(Exception):
    pass


class SessionAlreadyExists(Exception):
    pass


class SessionTombstoned(Exception):
    pass


class SessionPolicyMismatch(Exception):
    pass


class SessionClient(Protocol):
    async def get_session(self, session_id: str, *, auto_create: bool = False) -> dict[str, Any]: ...

    async def create_session(self, session_id: str, *, options: dict[str, Any]) -> dict[str, Any]: ...

    async def update_session_config(
        self, session_id: str, *, options: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def delete_session(self, session_id: str) -> None: ...


def _hmac_hex(salt: str, value: str) -> str:
    if len(salt) < 32:
        raise ValueError("user_id_salt must be at least 32 characters")
    return hmac.new(salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


class SessionManager:
    def __init__(
        self,
        sync_store: MemorySyncStore,
        client: SessionClient,
        config: OpenVikingMemoryConfig,
    ) -> None:
        self._sync_store = sync_store
        self._client = client
        self._config = config

    def ov_user(self, user_id: str) -> str:
        return "u_" + _hmac_hex(self._config.user_id_salt, user_id)[:32]

    def stream_shard(self, user_id: str, thread_id: str) -> int:
        digest = _hmac_hex(self._config.user_id_salt, f"{user_id}:{thread_id}")
        return int(digest, 16) % self._config.capture_stream.shard_count

    def session_id(self, user_id: str, thread_id: str, incarnation: str) -> str:
        digest = _hmac_hex(
            self._config.user_id_salt,
            f"{user_id}:{thread_id}:{incarnation}",
        )
        return "mf-s-" + digest[:32]

    async def resolve_capture_context(self, user_id: str, thread_id: str) -> SessionRef:
        existing = await self._sync_store.get(user_id, thread_id)
        if existing is not None:
            if existing.state == "deleted":
                raise SessionTombstoned(f"thread is tombstoned: {user_id}/{thread_id}")
            return self._ref_from_row(existing)

        incarnation = uuid.uuid4().hex
        ov_user = self.ov_user(user_id)
        shard = self.stream_shard(user_id, thread_id)
        ov_session_id = self.session_id(user_id, thread_id, incarnation)
        row = await self._sync_store.get_or_create(
            user_id=user_id,
            thread_id=thread_id,
            ov_user=ov_user,
            ov_session_id=ov_session_id,
            session_incarnation=incarnation,
            stream_shard=shard,
        )
        return self._ref_from_row(row)

    async def ensure_session(self, ref: SessionRef) -> SessionRef:
        try:
            response = await self._client.get_session(ref.ov_session_id, auto_create=False)
        except SessionNotFound:
            try:
                response = await self._client.create_session(
                    ref.ov_session_id,
                    options=self._config.session_create_options(),
                )
            except SessionAlreadyExists:
                response = await self._client.get_session(ref.ov_session_id, auto_create=False)
        except SessionAlreadyExists:
            response = await self._client.get_session(ref.ov_session_id, auto_create=False)

        self._validate_memory_policy(response)
        await self._ensure_auto_policy(ref, response)
        return ref

    async def delete_session(self, user_id: str, thread_id: str) -> None:
        row = await self._sync_store.get(user_id, thread_id)
        if row is not None:
            await self._client.delete_session(row.ov_session_id)
        await self._sync_store.mark_deleted(user_id, thread_id)

    async def recover_after_not_found(self, user_id: str, thread_id: str) -> SessionRef:
        row = await self._sync_store.get(user_id, thread_id)
        if row is None:
            return await self.resolve_capture_context(user_id, thread_id)
        row = await self._sync_store.bump_epoch(user_id, thread_id)
        return self._ref_from_row(row)

    def _ref_from_row(self, row: Any) -> SessionRef:
        return SessionRef(
            user_id=row.user_id,
            thread_id=row.thread_id,
            ov_user=row.ov_user,
            ov_session_id=row.ov_session_id,
            stream_shard=row.stream_shard,
            session_epoch=row.session_epoch,
            session_incarnation=row.session_incarnation,
        )

    def _validate_memory_policy(self, response: dict[str, Any]) -> None:
        expected = self._config.session.memory_policy()
        actual = response.get("memory_policy")
        if not isinstance(actual, dict):
            raise SessionPolicyMismatch("session has no effective memory_policy")

        if self._enabled(actual.get("self")) is not expected["self"]["enabled"]:
            raise SessionPolicyMismatch("session self memory policy mismatch")
        if self._enabled(actual.get("peer")) is not expected["peer"]["enabled"]:
            raise SessionPolicyMismatch("session peer memory policy mismatch")
        if self._enabled(actual.get("working_memory")) is not expected["working_memory"]["enabled"]:
            raise SessionPolicyMismatch("session working_memory policy mismatch")
        actual_types = actual.get("memory_types")
        if actual_types is not None and set(actual_types) != set(expected["memory_types"]):
            raise SessionPolicyMismatch("session memory_types mismatch")

    async def _ensure_auto_policy(self, ref: SessionRef, response: dict[str, Any]) -> None:
        expected = self._config.session_create_options()["auto_commit_policy"]
        actual = response.get("auto_commit_policy")
        if actual == expected:
            return
        updated = await self._client.update_session_config(
            ref.ov_session_id,
            options={"auto_commit_policy": expected},
        )
        if updated.get("auto_commit_policy") not in (None, expected):
            raise SessionPolicyMismatch("session auto_commit_policy mismatch")

    @staticmethod
    def _enabled(value: Any) -> bool | None:
        if isinstance(value, dict):
            return bool(value.get("enabled"))
        if isinstance(value, bool):
            return value
        return None


__all__ = [
    "SessionAlreadyExists",
    "SessionClient",
    "SessionManager",
    "SessionNotFound",
    "SessionPolicyMismatch",
    "SessionTombstoned",
]
