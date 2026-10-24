"""Local provider: mapping table, per-thread cache, LRU cap. Not a security boundary."""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from agentkit.config import get_app_config
from agentkit.config.runtime_paths import resolve_path, runtime_home
from agentkit.sandbox.local.local_sandbox import LocalSandbox
from agentkit.sandbox.sandbox_provider import SandboxProvider
from agentkit.sandbox.security import PathMapping, is_host_bash_allowed, sandbox_flag
from agentkit.utils import validate_user_id

_DEFAULT_USER_ID = "default"
_MAX_CACHED_THREADS = 256
_USER_DATA = "/mnt/user-data"
_PUBLIC_SKILLS = "/mnt/skills/public"
_PRIVATE_SKILLS = "/mnt/skills/private"


class LocalSandboxProvider(SandboxProvider):
    """In-process sandbox. ``reset`` drops cache entries; ``shutdown`` also kills processes.

    ``reset`` does not delete files and does not promise to release a sandbox another
    caller still holds. Remote providers must not destroy containers inside ``reset``.
    The thread cache cap is a local detail: evicting a remote sandbox must release it.
    """

    def __init__(self, *, cache_dir: str | Path | None = None, **kwargs: object) -> None:
        del kwargs
        self._lock = threading.Lock()
        self._thread_sandboxes: OrderedDict[tuple[str, str], LocalSandbox] = OrderedDict()
        self._max_cached_threads = _MAX_CACHED_THREADS
        self._generic: LocalSandbox | None = None
        self._skills_cache_dir = (
            resolve_path(cache_dir) if cache_dir is not None else _skills_cache_dir()
        )
        self._static_mappings = _static_mappings(self._skills_cache_dir)

    @property
    def supports_agent_skill_isolation(self) -> bool:
        """False while host bash can bypass path mappings."""
        return not is_host_bash_allowed()

    @property
    def supports_per_user_skill_mounts(self) -> bool:
        """This provider builds the private mount from the sandbox's owner id."""
        return True

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        user = validate_user_id(user_id or _DEFAULT_USER_ID)
        if thread_id is None:
            with self._lock:
                if self._generic is None:
                    self._generic = self._build("local", user, thread_id=None)
                return self._generic.id

        validate_user_id(thread_id)
        key = (user, thread_id)
        with self._lock:
            existing = self._thread_sandboxes.get(key)
            if existing is not None:
                self._thread_sandboxes.move_to_end(key)
                return existing.id
            while len(self._thread_sandboxes) >= self._max_cached_threads:
                self._thread_sandboxes.popitem(last=False)
            sandbox = self._build(_sandbox_id_for_thread(thread_id, user), user, thread_id)
            self._thread_sandboxes[key] = sandbox
            return sandbox.id

    def get(self, sandbox_id: str) -> LocalSandbox | None:
        """Pure memory lookup. No health check and no filesystem access."""
        if sandbox_id == "local":
            with self._lock:
                return self._generic
        parsed = _key_from_sandbox_id(sandbox_id)
        if parsed is None:
            return None
        with self._lock:
            sandbox = self._thread_sandboxes.get(parsed)
            if sandbox is not None:
                self._thread_sandboxes.move_to_end(parsed)
            return sandbox

    def release(self, sandbox_id: str) -> None:
        if sandbox_id == "local":
            with self._lock:
                sandbox = self._generic
                self._generic = None
            if sandbox is not None:
                sandbox.shutdown()
            return
        parsed = _key_from_sandbox_id(sandbox_id)
        if parsed is None:
            return
        with self._lock:
            sandbox = self._thread_sandboxes.pop(parsed, None)
        if sandbox is not None:
            sandbox.shutdown()

    def reset(self) -> None:
        """Drop cached instances so the next ``acquire`` rebuilds them. Files stay."""
        with self._lock:
            self._thread_sandboxes.clear()
            self._generic = None

    def shutdown(self) -> None:
        """Release every cached sandbox and kill its process trees."""
        with self._lock:
            sandboxes = list(self._thread_sandboxes.values())
            self._thread_sandboxes.clear()
            generic = self._generic
            self._generic = None
        for sandbox in sandboxes:
            sandbox.shutdown()
        if generic is not None:
            generic.shutdown()

    def _build(self, sandbox_id: str, user_id: str, thread_id: str | None) -> LocalSandbox:
        if thread_id is None:
            root = runtime_home() / "users" / user_id / "local" / "user-data"
        else:
            root = runtime_home() / "users" / user_id / "threads" / thread_id / "user-data"
        for name in ("workspace", "uploads", "outputs"):
            (root / name).mkdir(parents=True, exist_ok=True)
        mappings = [
            *self._static_mappings,
            # Public skills are one process-wide read-only tree.  Private skills
            # use the same container path for every user, but each sandbox binds
            # it to that user's own active tree.  Never add a mount for the cache
            # root or for active/users itself.
            PathMapping(
                _PRIVATE_SKILLS,
                str(self._skills_cache_dir / "active" / "users" / user_id),
                read_only=True,
            ),
            PathMapping(_USER_DATA, str(root)),
            PathMapping(f"{_USER_DATA}/workspace", str(root / "workspace")),
            PathMapping(f"{_USER_DATA}/uploads", str(root / "uploads")),
            PathMapping(f"{_USER_DATA}/outputs", str(root / "outputs")),
        ]
        timeout = float(sandbox_flag("bash_command_timeout", 600) or 600)
        return LocalSandbox(sandbox_id, mappings, command_timeout=timeout)


def _sandbox_id_for_thread(thread_id: str, user_id: str) -> str:
    return f"local:{user_id}:{thread_id}"


def _key_from_sandbox_id(sandbox_id: str) -> tuple[str, str] | None:
    """Decode ``(user_id, thread_id)``. Malformed ids return ``None``."""
    if not sandbox_id.startswith("local:"):
        return None
    user_id, sep, thread_id = sandbox_id[len("local:") :].partition(":")
    if not sep or not user_id or not thread_id:
        return None
    return (user_id, thread_id)


def _skills_cache_dir() -> Path:
    """Host cache that SkillContentStore materializes active trees under."""
    try:
        raw = get_app_config().skills.git.cache_dir
    except Exception:  # unit tests may not have a readable app config
        return runtime_home() / "skills"
    return resolve_path(raw)


def _static_mappings(skills_cache_dir: Path) -> list[PathMapping]:
    """Process-wide mounts only. Never bind a user id here."""
    mappings: list[PathMapping] = [
        PathMapping(
            _PUBLIC_SKILLS,
            str(skills_cache_dir / "active" / "public"),
            read_only=True,
        ),
    ]
    raw = sandbox_flag("mounts", []) or []
    if isinstance(raw, list):
        cache_root = skills_cache_dir.resolve()
        for item in raw:
            if not isinstance(item, dict):
                continue
            container = item.get("container_path")
            local = item.get("local_path")
            if not container or not local:
                continue
            container_text = str(container).rstrip("/")
            local_path = Path(str(local)).resolve()
            # Operator mounts must never reintroduce the process-wide skill
            # cache (or its active/users subtree) that this provider replaced
            # with one public mount plus a per-sandbox private mount.
            if container_text == "/mnt/skills" or container_text.startswith("/mnt/skills/"):
                continue
            if local_path == cache_root or local_path.is_relative_to(cache_root):
                continue
            mappings.append(
                PathMapping(
                    container_text,
                    str(local_path),
                    read_only=bool(item.get("read_only", False)),
                )
            )
    return mappings
