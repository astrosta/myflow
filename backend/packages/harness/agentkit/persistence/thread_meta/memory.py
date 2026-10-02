"""In-process thread metadata. Used when the session factory is None."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from agentkit.persistence.base import iso_utc
from agentkit.persistence.json_compat import MISSING, python_value_matches, validate_metadata_filter
from agentkit.persistence.thread_meta.base import (
    ThreadMetaStore,
    ThreadOwnershipConflictError,
    require_thread_owner,
    resolve_thread_user,
)
from agentkit.persistence.thread_meta.sql import strip_reserved_metadata
from agentkit.runtime.user_context import AUTO


class MemoryThreadMetaStore(ThreadMetaStore):
    """Process-local store selected by make_thread_meta_store(None)."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def _owned(self, thread_id: str, user_id: str | None) -> dict | None:
        row = self._rows.get(thread_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return row

    async def create(
        self,
        thread_id,
        *,
        assistant_id=None,
        user_id=AUTO,
        display_name=None,
        metadata=None,
    ) -> dict:
        owner = require_thread_owner(resolve_thread_user(user_id, "create"))
        existing = self._rows.get(thread_id)
        if existing is not None:
            if existing["user_id"] != owner:
                raise ThreadOwnershipConflictError(f"thread {thread_id} is owned by another user")
            return deepcopy(existing)
        now = iso_utc(datetime.now(UTC))
        self._rows[thread_id] = {
            "thread_id": thread_id,
            "incarnation": uuid4().hex,
            "assistant_id": assistant_id,
            "user_id": owner,
            "display_name": display_name or None,
            "status": "idle",
            "metadata": strip_reserved_metadata(metadata),
            "created_at": now,
            "updated_at": now,
        }
        return deepcopy(self._rows[thread_id])

    async def get(self, thread_id, *, user_id=AUTO) -> dict | None:
        row = self._owned(thread_id, resolve_thread_user(user_id, "get"))
        return deepcopy(row) if row is not None else None

    async def search(
        self,
        *,
        metadata=None,
        status=None,
        limit: int = 100,
        offset: int = 0,
        user_id=AUTO,
    ) -> list[dict]:
        owner = resolve_thread_user(user_id, "search")
        filters = metadata or {}
        for key, expected in filters.items():
            validate_metadata_filter(key, expected)
        matched = []
        for row in self._rows.values():
            if owner is not None and row["user_id"] != owner:
                continue
            if status is not None and row["status"] != status:
                continue
            if not _metadata_matches(row["metadata"], filters):
                continue
            matched.append(row)
        matched.sort(key=lambda row: (row["updated_at"], row["thread_id"]), reverse=True)
        return [deepcopy(row) for row in matched[offset : offset + limit]]

    async def update_display_name(self, thread_id, display_name, *, user_id=AUTO) -> None:
        row = self._owned(thread_id, resolve_thread_user(user_id, "update_display_name"))
        if row is None:
            return
        row["display_name"] = display_name or None
        row["updated_at"] = iso_utc(datetime.now(UTC))

    async def update_status(self, thread_id, status, *, user_id=AUTO) -> None:
        row = self._owned(thread_id, resolve_thread_user(user_id, "update_status"))
        if row is None:
            return
        row["status"] = status
        row["updated_at"] = iso_utc(datetime.now(UTC))

    async def update_metadata(self, thread_id, metadata, *, touch: bool = True, user_id=AUTO) -> None:
        row = self._owned(thread_id, resolve_thread_user(user_id, "update_metadata"))
        if row is None:
            return
        row["metadata"] = {**row["metadata"], **strip_reserved_metadata(metadata)}
        if touch:
            row["updated_at"] = iso_utc(datetime.now(UTC))

    async def delete(self, thread_id, *, user_id=AUTO) -> None:
        row = self._owned(thread_id, resolve_thread_user(user_id, "delete"))
        if row is not None:
            del self._rows[thread_id]

    async def check_access(self, thread_id, user_id: str, *, require_existing: bool = False) -> bool:
        row = self._rows.get(thread_id)
        if row is None:
            return not require_existing
        return row["user_id"] == user_id


def _metadata_matches(metadata: dict, filters: dict) -> bool:
    for key, expected in filters.items():
        actual = metadata[key] if key in metadata else MISSING
        if not python_value_matches(actual, expected):
            return False
    return True
