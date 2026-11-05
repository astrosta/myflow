"""Plan host bind mounts for one user/thread, provider-neutral.

Two providers share this planning code: CubeSandbox (bind mounts declared in
sandbox metadata) and the AIO Docker provider (docker --mount). Both run the
sandbox on a host that must see the Gateway's project root, possibly under a
different path: local_root is what the Gateway process sees, host_root is what
the sandbox runtime sees (Cubelet's namespace, or the Docker daemon's).

Every source is resolved before it is translated. A source that escapes
local_root through '..' or a symlink is rejected instead of becoming a
read-only bind mount of an unrelated host directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from agentkit.config.app_config import AppConfig
from agentkit.config.paths import Paths, get_paths
from agentkit.config.runtime_paths import project_root, resolve_path
from agentkit.config.skills_config import SkillsConfig
from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.skills.builtin import resolve_builtin_root
from agentkit.utils import validate_user_id

logger = logging.getLogger(__name__)

_USER_DATA_CONTAINER = "/mnt/user-data"
_PRIVATE_SKILLS_DIR = "private"
_BUILTIN_SKILLS_DIR = "builtin"
_SANDBOX_DIR_NAMES = ("workspace", "uploads", "outputs")
_DEFAULT_FIELD_PREFIX = "host_mount"


class HostMountSpec(Protocol):
    """The two settings host mount planning needs from a provider config."""

    local_root: str | None
    host_root: str | None


@dataclass(frozen=True, slots=True)
class HostRoots:
    """Gateway-side and runtime-side names for the same shared project root."""

    local_root: Path
    host_root: PurePosixPath


@dataclass(frozen=True, slots=True)
class MountEntry:
    """One host mount: a container path backed by a runtime-visible host path."""

    container_path: str
    host_path: str
    read_only: bool = False

    def as_payload(self) -> dict[str, object]:
        """CubeSandbox's camelCase host-mount wire shape.

        The AIO provider only uses this through mounts_hash. Keeping one stable
        shape means a fingerprint is comparable across providers and across a
        config change that swaps one provider for the other.
        """
        return {
            "hostPath": self.host_path,
            "mountPath": self.container_path,
            "readOnly": self.read_only,
        }


def resolve_host_roots(
    host_mount: HostMountSpec, *, field_prefix: str = _DEFAULT_FIELD_PREFIX
) -> HostRoots:
    """Resolve a provider's host_mount section with same-host defaults.

    local_root defaults to project_root(); host_root defaults to the local root.
    A relative or filesystem-root host_root is rejected here so no caller can
    accidentally turn a project mount into a host-root mount.
    """

    configured_local = host_mount.local_root
    local_root = Path(configured_local) if configured_local else project_root()
    # The default is a POSIX path by contract: a host mount is a Linux bind mount
    # source. as_posix() keeps an already-POSIX local_root byte-identical and
    # refuses a Windows-shaped one at _parse_host_root, instead of silently
    # handing the daemon a path it cannot resolve.
    host_root_text = host_mount.host_root or local_root.as_posix()
    return HostRoots(
        local_root=local_root,
        host_root=_parse_host_root(host_root_text, f"{field_prefix}.host_root"),
    )


def to_host_path(local: Path, roots: HostRoots) -> str:
    """Translate a Gateway-local path to the runtime-visible host path."""

    local_root = roots.local_root.resolve()
    resolved = Path(local).resolve()
    if not resolved.is_relative_to(local_root):
        raise SandboxRuntimeError(
            f"sandbox mount source {resolved} is outside "
            f"host_mount.local_root {local_root}"
        )
    relative = resolved.relative_to(local_root)
    return str(roots.host_root.joinpath(*relative.parts))


def build_host_mounts(
    *,
    app_config: AppConfig,
    host_mount: HostMountSpec,
    user_id: str,
    thread_id: str | None,
    paths: Paths | None = None,
    field_prefix: str = _DEFAULT_FIELD_PREFIX,
) -> tuple[MountEntry, ...]:
    """Build the complete host-mount list for one user/thread.

    Thread data is created before the list is returned. The git private-skill
    tree is created empty if the catalog has not materialized it yet; the builtin
    tree is never created here (a missing builtin directory is a skipped mount,
    not a reason to write into the project).
    """

    roots = resolve_host_roots(host_mount, field_prefix=field_prefix)
    validate_mount_sources(app_config, roots, field_prefix=field_prefix)

    user = validate_user_id(user_id)
    resolved_paths = paths if paths is not None else get_paths()
    user_data = _ensure_user_data(resolved_paths, thread_id, user)
    skills = app_config.skills
    skills_container = _parse_container_path(skills.container_path, "skills.container_path")

    entries: list[MountEntry] = [
        MountEntry(
            container_path=_USER_DATA_CONTAINER,
            host_path=to_host_path(user_data, roots),
            read_only=False,
        )
    ]

    if skills.source == "git":
        private_root = resolve_path(skills.git.cache_dir) / "active" / "users" / user
        private_root.mkdir(parents=True, exist_ok=True)
        entries.append(
            MountEntry(
                container_path=f"{skills_container}/{_PRIVATE_SKILLS_DIR}",
                host_path=to_host_path(private_root, roots),
                read_only=True,
            )
        )

    filesystem_root_mounted = False
    if skills.source == "filesystem":
        skills_root = skills.root_dir()
        if skills_root.is_dir():
            entries.append(
                MountEntry(
                    container_path=skills_container,
                    host_path=to_host_path(skills_root, roots),
                    read_only=True,
                )
            )
            filesystem_root_mounted = True
        else:
            logger.warning(
                "filesystem skills root is missing; skipping skills mount: %s",
                skills_root,
            )

    builtin_root = _builtin_root(skills)
    if builtin_root is not None:
        if not builtin_root.is_dir():
            logger.warning(
                "built-in skills directory is missing; skipping builtin mount: %s",
                builtin_root,
            )
        elif filesystem_root_mounted and builtin_root.is_relative_to(skills.root_dir()):
            # The filesystem source mounts this tree as part of <container>.
            pass
        else:
            entries.append(
                MountEntry(
                    container_path=f"{skills_container}/{_BUILTIN_SKILLS_DIR}",
                    host_path=to_host_path(builtin_root, roots),
                    read_only=True,
                )
            )

    entries.extend(
        _operator_mounts(
            app_config,
            roots,
            skills_container=skills_container,
            protected_sources=_protected_sources(skills),
        )
    )

    # Parent mounts must be listed before nested mounts. The stable sort keeps
    # the construction order for siblings (user-data first, then skills).
    entries.sort(key=_container_depth)
    return tuple(entries)


def mounts_hash(mounts: Sequence[MountEntry]) -> str:
    """Stable SHA-256 fingerprint of the host-mount list (16 hex chars)."""

    payloads = sorted(
        json.dumps(entry.as_payload(), sort_keys=True, separators=(",", ":"))
        for entry in mounts
    )
    digest = hashlib.sha256("\n".join(payloads).encode("utf-8")).hexdigest()
    return digest[:16]


def validate_mount_sources(
    app_config: AppConfig, roots: HostRoots, *, field_prefix: str = _DEFAULT_FIELD_PREFIX
) -> None:
    """Validate roots and every static mount source without creating directories.

    This is a startup check: it must be safe to call before any user logs in, so
    it deliberately never creates the per-user private-skill tree or user-data
    directory. Only build_host_mounts performs those writes.
    """

    _validate_local_root(roots.local_root, f"{field_prefix}.local_root")
    _validate_host_root(roots.host_root, f"{field_prefix}.host_root")

    skills = app_config.skills
    _parse_container_path(skills.container_path, "skills.container_path")

    if skills.source == "git":
        _require_under_root(
            resolve_path(skills.git.cache_dir),
            roots.local_root,
            "skills.git.cache_dir",
        )
    else:
        _require_under_root(
            skills.root_dir(),
            roots.local_root,
            "skills.path",
        )

    builtin_root = _builtin_root(skills)
    if builtin_root is not None:
        _require_under_root(builtin_root, roots.local_root, "skills.builtin.path")

    for index, item in enumerate(app_config.sandbox.mounts):
        container, local = _operator_mount_fields(item, index)
        _parse_container_path(container, f"sandbox.mounts[{index}].container_path")
        to_host_path(local, roots)


def _ensure_user_data(paths: Paths, thread_id: str | None, user_id: str) -> Path:
    """Create the thread (or generic) data tree and return its user-data root."""

    if thread_id is None:
        user_data = paths.base_dir / "users" / user_id / "local" / "user-data"
        for name in _SANDBOX_DIR_NAMES:
            (user_data / name).mkdir(parents=True, exist_ok=True)
        return user_data

    paths.ensure_thread_dirs(thread_id, user_id=user_id)
    return paths.sandbox_user_data_dir(thread_id, user_id=user_id)


def _operator_mounts(
    app_config: AppConfig,
    roots: HostRoots,
    *,
    skills_container: str,
    protected_sources: Sequence[Path],
) -> list[MountEntry]:
    entries: list[MountEntry] = []
    seen_containers: set[str] = set()
    protected = tuple(path.resolve() for path in protected_sources)

    for index, item in enumerate(app_config.sandbox.mounts):
        container, local = _operator_mount_fields(item, index)
        container = _parse_container_path(
            container, f"sandbox.mounts[{index}].container_path"
        )

        if _is_at_or_under(container, _USER_DATA_CONTAINER):
            logger.warning(
                "operator mount %s overlaps %s; skipping",
                container,
                _USER_DATA_CONTAINER,
            )
            continue
        if _is_at_or_under(container, skills_container):
            logger.warning(
                "operator mount %s overlaps the skills container %s; skipping",
                container,
                skills_container,
            )
            continue
        if container in seen_containers:
            logger.warning("duplicate operator mount %s; keeping the first", container)
            continue

        resolved = local.resolve()
        if any(resolved == root or resolved.is_relative_to(root) for root in protected):
            logger.warning(
                "operator mount %s uses a protected skills source %s; skipping",
                container,
                resolved,
            )
            continue

        host_path = to_host_path(local, roots)
        entries.append(
            MountEntry(
                container_path=container,
                host_path=host_path,
                read_only=bool(item.get("read_only", False)),
            )
        )
        seen_containers.add(container)

    return entries


def _operator_mount_fields(item: object, index: int) -> tuple[str, Path]:
    if not isinstance(item, dict):
        raise SandboxRuntimeError(f"sandbox.mounts[{index}] must be a mapping")
    container = item.get("container_path")
    local = item.get("local_path")
    if not container or not local:
        raise SandboxRuntimeError(
            f"sandbox.mounts[{index}] requires container_path and local_path"
        )
    return str(container), Path(str(local))


def _protected_sources(skills: SkillsConfig) -> tuple[Path, ...]:
    """Skills sources an operator mount must never re-expose process-wide."""

    sources = [resolve_path(skills.git.cache_dir), skills.root_dir()]
    builtin_root = _builtin_root(skills)
    if builtin_root is not None:
        sources.append(builtin_root)
    return tuple(sources)


def _builtin_root(skills: SkillsConfig) -> Path | None:
    if not skills.builtin.enabled:
        return None
    try:
        return resolve_builtin_root(skills)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxRuntimeError(f"invalid skills.builtin.path: {exc}") from exc


def _parse_container_path(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise SandboxRuntimeError(f"{field} must not be blank")
    path = PurePosixPath(text)
    if not path.is_absolute():
        raise SandboxRuntimeError(f"{field} must be an absolute POSIX path: {text}")
    if path.parent == path:
        raise SandboxRuntimeError(f"{field} must not be the filesystem root: {text}")
    if any(part == ".." for part in path.parts):
        raise SandboxRuntimeError(f"{field} must not contain '..': {text}")
    return str(path)


def _parse_host_root(value: str, field: str) -> PurePosixPath:
    text = str(value).strip()
    if not text:
        raise SandboxRuntimeError(f"{field} must not be blank")
    path = PurePosixPath(text)
    if not path.is_absolute():
        raise SandboxRuntimeError(f"{field} must be an absolute POSIX path: {text}")
    if path.parent == path:
        raise SandboxRuntimeError(f"{field} must not be the filesystem root: {text}")
    if any(part == ".." for part in path.parts):
        raise SandboxRuntimeError(f"{field} must not contain '..': {text}")
    return path


def _validate_local_root(local_root: Path, field: str) -> None:
    if not local_root.is_absolute():
        raise SandboxRuntimeError(f"{field} must be absolute: {local_root}")
    if not local_root.exists():
        raise SandboxRuntimeError(f"{field} does not exist: {local_root}")
    if not local_root.is_dir():
        raise SandboxRuntimeError(f"{field} is not a directory: {local_root}")


def _validate_host_root(host_root: PurePosixPath, field: str) -> None:
    # Re-parse instead of trusting a hand-constructed HostRoots value.
    _parse_host_root(str(host_root), field)


def _require_under_root(source: Path, local_root: Path, field: str) -> None:
    resolved_root = local_root.resolve()
    resolved_source = source.resolve()
    if not resolved_source.is_relative_to(resolved_root):
        raise SandboxRuntimeError(
            f"{field} source {resolved_source} is outside "
            f"host_mount.local_root {resolved_root}"
        )


def _is_at_or_under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _container_depth(entry: MountEntry) -> int:
    return len(PurePosixPath(entry.container_path).parts)


__all__ = [
    "HostMountSpec",
    "HostRoots",
    "MountEntry",
    "build_host_mounts",
    "mounts_hash",
    "resolve_host_roots",
    "to_host_path",
    "validate_mount_sources",
]
