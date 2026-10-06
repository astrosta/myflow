"""Thread metadata repository. Callers depend on this type, not on a backend."""

import abc

from agentkit.runtime.user_context import AUTO, MissingUserContextError, resolve_user_id


class ThreadOwnershipConflictError(Exception):
    """create found this thread_id owned by a different user."""


class ThreadMetaStore(abc.ABC):
    """Returned by make_thread_meta_store. Business code does not inspect the backend."""

    @abc.abstractmethod
    async def create(
        self,
        thread_id,
        *,
        assistant_id=None,
        user_id=AUTO,
        display_name=None,
        metadata=None,
    ) -> dict: ...

    @abc.abstractmethod
    async def get(self, thread_id, *, user_id=AUTO) -> dict | None: ...

    @abc.abstractmethod
    async def search(
        self,
        *,
        metadata=None,
        status=None,
        limit: int = 100,
        offset: int = 0,
        user_id=AUTO,
    ) -> list[dict]: ...

    async def search_page(
        self,
        *,
        user_id: str | None,
        metadata=None,
        status: str | None = None,
        limit: int = 51,
        cursor: str | None = None,
        order: str = "desc",
    ) -> list[dict]:
        """Keyset page. Each row carries a non-persistent ``cursor`` key. Subclasses override this."""
        raise NotImplementedError

    @abc.abstractmethod
    async def update_display_name(self, thread_id, display_name, *, user_id=AUTO) -> None: ...

    @abc.abstractmethod
    async def update_status(self, thread_id, status, *, user_id=AUTO) -> None: ...

    @abc.abstractmethod
    async def update_metadata(self, thread_id, metadata, *, touch: bool = True, user_id=AUTO) -> None: ...

    @abc.abstractmethod
    async def delete(self, thread_id, *, user_id=AUTO) -> None: ...

    @abc.abstractmethod
    async def check_access(self, thread_id, user_id: str, *, require_existing: bool = False) -> bool: ...


def resolve_thread_user(user_id, method: str):
    """AUTO reads the current user. Explicit None stays a global query."""
    return resolve_user_id(user_id, method_name=f"ThreadMetaStore.{method}")


def require_thread_owner(user_id: str | None) -> str:
    """create always records a user. Explicit None is not a license to store NULL."""
    if user_id is None:
        raise MissingUserContextError(
            "ThreadMetaStore.create() refuses to store a null user_id; "
            "background writers must pass record.user_id explicitly"
        )
    return user_id
