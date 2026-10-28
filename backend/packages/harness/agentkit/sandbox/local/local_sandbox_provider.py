"""Local provider: mapping table, per-thread cache, LRU cap. Not a security boundary."""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from agentkit.config import get_app_config
from agentkit.config.runtime_paths import resolve_path, runtime_home
from agentkit.config.skills_config import DEFAULT_SKILLS_CONTAINER_PATH
from agentkit.sandbox.local.local_sandbox import LocalSandbox
from agentkit.sandbox.sandbox_provider import SandboxProvider
from agentkit.sandbox.security import PathMapping, is_host_bash_allowed, sandbox_flag
from agentkit.skills.builtin import resolve_builtin_root
from agentkit.utils import validate_user_id

_DEFAULT_USER_ID = "default"
_MAX_CACHED_THREADS = 256
_USER_DATA = "/mnt/user-data"


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
        self._skills_root = _skills_container_root()
        self._skills_source = _skills_source()
        self._public_skills = f"{self._skills_root}/public"
        self._private_skills = f"{self._skills_root}/private"
        self._builtin_skills_dir = _builtin_skills_dir()
        self._static_mappings = _static_mappings(
            self._skills_cache_dir,
            container_root=self._skills_root,
            source=self._skills_source,
            builtin_dir=self._builtin_skills_dir,
        )

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
        mappings = [*self._static_mappings, *self._private_skill_mappings(user_id)]
        mappings += [
            PathMapping(_USER_DATA, str(root)),
            PathMapping(f"{_USER_DATA}/workspace", str(root / "workspace")),
            PathMapping(f"{_USER_DATA}/uploads", str(root / "uploads")),
            PathMapping(f"{_USER_DATA}/outputs", str(root / "outputs")),
        ]
        timeout = float(sandbox_flag("bash_command_timeout", 600) or 600)
        return LocalSandbox(sandbox_id, mappings, command_timeout=timeout)

    def _private_skill_mappings(self, user_id: str) -> list[PathMapping]:
        """Per-sandbox private skill mount: only the git catalog has one."""

        if self._skills_source != "git":
            # filesystem 来源没有私有 active 树，整棵技能树已经由公共映射挂上。
            return []
        # 私有树对每个用户都是同一个容器路径，但每个沙箱绑到该用户自己的
        # active 树上。绝不额外挂缓存根或 active/users 本身。
        return [
            PathMapping(
                self._private_skills,
                str(self._skills_cache_dir / "active" / "users" / user_id),
                read_only=True,
            )
        ]


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


def _skills_container_root() -> str:
    """Configured container root; the default is only a fallback for unreadable config."""
    try:
        raw = get_app_config().skills.container_path
    except Exception:  # unit tests may not have a readable app config
        return DEFAULT_SKILLS_CONTAINER_PATH
    return raw.rstrip("/") or DEFAULT_SKILLS_CONTAINER_PATH


def _skills_source() -> str:
    """目录来源（git | filesystem）。

    配置读不出来时按 git 处理：这是 M11 之前的状态，也是这个 provider 的
    private 挂载能力所属的来源。真正跑 filesystem 来源的部署一定有可读配置，
    那种情况下这里会如实返回 filesystem，挂载点随之变成整棵技能树。
    """
    try:
        return str(get_app_config().skills.source)
    except Exception:  # noqa: BLE001 - 单元测试可能没有可读的 app config
        return "git"


def _skills_tree_root() -> Path:
    """filesystem 来源下真正要挂的宿主技能树根（skills.path），不是 active 缓存。"""
    try:
        return get_app_config().skills.root_dir()
    except Exception:  # noqa: BLE001 - 同上
        return runtime_home() / "skills"


def _builtin_skills_dir() -> Path | None:
    """Configured built-in tree, or None when disabled, unreadable, or unsafe.

    Uses the same resolver as the loader so an empty/relative-root/symlink/junction
    path never becomes a read-only mount of the project or host filesystem.
    """
    try:
        config = get_app_config().skills
        if not config.builtin.enabled:
            return None
        return resolve_builtin_root(config)
    except (OSError, RuntimeError, ValueError):
        return None
    except Exception:  # noqa: BLE001 - 单元测试可能没有可读的 app config
        return None


def _static_mappings(
    skills_cache_dir: Path,
    *,
    container_root: str = DEFAULT_SKILLS_CONTAINER_PATH,
    source: str = "git",
    builtin_dir: Path | None = None,
) -> list[PathMapping]:
    """Process-wide mounts only. Never bind a user id here.

    git 来源挂 active 公共树（私有树按用户挂，见 _build）。filesystem 来源没有
    runtime，也就没有 active 树：它必须挂**技能树根本身**，否则提示词里写的
    {container}/<category>/<name>/SKILL.md 在沙箱里根本不存在。内置技能树在两种
    来源下都额外挂到 {root}/builtin；目录不存在也挂，配置变化要重启进程。
    """
    root = container_root.rstrip("/") or "/"
    mappings: list[PathMapping] = (
        [
            PathMapping(
                f"{root}/public",
                str(skills_cache_dir / "active" / "public"),
                read_only=True,
            )
        ]
        if source == "git"
        else [PathMapping(root, str(_skills_tree_root()), read_only=True)]
    )
    if builtin_dir is not None:
        mappings.append(
            PathMapping(f"{root}/builtin", str(builtin_dir), read_only=True)
        )
    protected = [skills_cache_dir.resolve()]
    if source != "git":
        protected.append(_skills_tree_root().resolve())
    if builtin_dir is not None:
        protected.append(builtin_dir.resolve())
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
            if container_text == root or container_text.startswith(f"{root}/"):
                continue
            if any(
                local_path == protected_path
                or local_path.is_relative_to(protected_path)
                for protected_path in protected
            ):
                continue
            mappings.append(
                PathMapping(
                    container_text,
                    str(local_path),
                    read_only=bool(item.get("read_only", False)),
                )
            )
    return mappings
