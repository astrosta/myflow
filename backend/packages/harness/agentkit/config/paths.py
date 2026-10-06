"""Host paths for per-user thread data. Layout matches the local sandbox."""

from __future__ import annotations

from pathlib import Path

from agentkit.config.runtime_paths import runtime_home
from agentkit.utils.ids import validate_thread_id, validate_user_id

_DIR_MODE = 0o777


class Paths:
    """`{base}/users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}`."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @property
    def base_dir(self) -> Path:
        return self._base_dir if self._base_dir is not None else runtime_home()

    def thread_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        user = validate_user_id(user_id or "default")
        return self.base_dir / "users" / user / "threads" / validate_thread_id(thread_id)

    def sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        return self.thread_dir(thread_id, user_id=user_id) / "user-data"

    def sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        return self.sandbox_user_data_dir(thread_id, user_id=user_id) / "workspace"

    def sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        return self.sandbox_user_data_dir(thread_id, user_id=user_id) / "uploads"

    def sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        return self.sandbox_user_data_dir(thread_id, user_id=user_id) / "outputs"

    def ensure_thread_dirs(self, thread_id: str, *, user_id: str | None = None) -> None:
        """Create the three sandbox directories. chmod after mkdir so umask cannot shrink them."""
        for directory in (
            self.sandbox_work_dir(thread_id, user_id=user_id),
            self.sandbox_uploads_dir(thread_id, user_id=user_id),
            self.sandbox_outputs_dir(thread_id, user_id=user_id),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(_DIR_MODE)


_paths: Paths | None = None


def get_paths() -> Paths:
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths
