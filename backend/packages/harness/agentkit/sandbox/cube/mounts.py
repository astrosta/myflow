"""CubeSandbox mount planning.

The planning itself lives in agentkit.sandbox.host_mounts, because the AIO
Docker provider needs the same list. This module keeps the CubeSandbox-facing
signatures: the host_mount defaults to sandbox.cube.host_mount and every field
name in an error message points at that section.
"""

from __future__ import annotations

from agentkit.config.app_config import AppConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import CubeSandboxConfig
from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.host_mounts import (
    HostRoots,
    MountEntry,
    build_host_mounts as _build_host_mounts,
    mounts_hash,
    resolve_host_roots as _resolve_host_roots,
    to_host_path,
    validate_mount_sources as _validate_mount_sources,
)

_FIELD_PREFIX = "sandbox.cube.host_mount"


def resolve_host_roots(cube: CubeSandboxConfig) -> HostRoots:
    """Resolve sandbox.cube.host_mount with same-host defaults."""

    return _resolve_host_roots(cube.host_mount, field_prefix=_FIELD_PREFIX)


def build_host_mounts(
    *,
    app_config: AppConfig,
    user_id: str,
    thread_id: str | None,
    paths: Paths | None = None,
) -> tuple[MountEntry, ...]:
    """Build the complete host-mount list for one CubeSandbox user/thread."""

    cube = app_config.sandbox.cube
    if cube is None:
        raise SandboxRuntimeError(
            "sandbox.cube is required to build CubeSandbox host mounts"
        )
    return _build_host_mounts(
        app_config=app_config,
        host_mount=cube.host_mount,
        user_id=user_id,
        thread_id=thread_id,
        paths=paths,
        field_prefix=_FIELD_PREFIX,
    )


def validate_mount_sources(app_config: AppConfig, roots: HostRoots) -> None:
    """Validate sandbox.cube.host_mount sources without creating directories."""

    _validate_mount_sources(app_config, roots, field_prefix=_FIELD_PREFIX)


__all__ = [
    "HostRoots",
    "MountEntry",
    "build_host_mounts",
    "mounts_hash",
    "resolve_host_roots",
    "to_host_path",
    "validate_mount_sources",
]
