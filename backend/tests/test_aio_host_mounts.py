"""Shared host-mount planning, exercised through a non-Cube provider shape.

The AIO Docker provider mounts the same directories CubeSandbox does, but it has
no sandbox.cube section and its own field prefix. These tests pin that the
shared module works without a Cube config and still names the right section in
an error message.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import (
    CubeSandboxConfig,
    CubeSandboxHostMountConfig,
    CubeSandboxLeaseConfig,
)
from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.host_mounts import (
    build_host_mounts,
    mounts_hash,
    resolve_host_roots,
    to_host_path,
)


def _skills(tmp_path: Path, *, source: str = "git") -> SkillsConfig:
    skills = SkillsConfig(
        source=source,
        path=str(tmp_path / "skills"),
        git={"cache_dir": str(tmp_path / "cache")},
        builtin={"path": str(tmp_path / "skills" / "builtin")},
    )
    (tmp_path / "skills" / "builtin").mkdir(parents=True, exist_ok=True)
    return skills


def _aio_config(tmp_path: Path) -> AppConfig:
    """An AppConfig with no sandbox.cube: the AIO provider must not need one."""

    return AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
        sandbox=SandboxConfig(),
        skills=_skills(tmp_path),
    )


def _aio_host_mount(tmp_path: Path, **overrides: str) -> SimpleNamespace:
    values = {"local_root": str(tmp_path), "host_root": "/opt/myflow"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_planning_does_not_need_a_cube_section(tmp_path: Path) -> None:
    config = _aio_config(tmp_path)
    assert config.sandbox.cube is None

    mounts = build_host_mounts(
        app_config=config,
        host_mount=_aio_host_mount(tmp_path),
        user_id="u1",
        thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
        field_prefix="sandbox.aio.host_mount",
    )

    by_container = {entry.container_path: entry for entry in mounts}
    assert by_container["/mnt/user-data"].host_path == (
        "/opt/myflow/.myflow/users/u1/threads/t1/user-data"
    )
    assert by_container["/mnt/user-data"].read_only is False
    assert by_container["/mnt/skills/private"].read_only is True
    assert (tmp_path / ".myflow" / "users" / "u1" / "threads" / "t1" / "user-data").is_dir()


def test_host_root_defaults_to_local_root() -> None:
    """A same-host deployment only writes local_root; host_root follows it.

    The roots are POSIX paths by contract (a Linux bind mount), so this stays a
    literal instead of tmp_path: a Windows temp directory is not a valid
    host_root and is rejected on purpose.
    """

    roots = resolve_host_roots(
        SimpleNamespace(local_root="/opt/myflow", host_root=None),
        field_prefix="sandbox.aio.host_mount",
    )
    assert roots.host_root == PurePosixPath("/opt/myflow")


def test_error_message_names_the_provider_section(tmp_path: Path) -> None:
    config = _aio_config(tmp_path)

    with pytest.raises(SandboxRuntimeError, match="sandbox.aio.host_mount.host_root"):
        build_host_mounts(
            app_config=config,
            host_mount=_aio_host_mount(tmp_path, host_root="relative/root"),
            user_id="u1",
            thread_id="t1",
            paths=Paths(tmp_path / ".myflow"),
            field_prefix="sandbox.aio.host_mount",
        )


def test_source_outside_local_root_is_rejected(tmp_path: Path) -> None:
    roots = resolve_host_roots(_aio_host_mount(tmp_path))
    outside = tmp_path.parent / "outside-aio"
    outside.mkdir(exist_ok=True)

    with pytest.raises(SandboxRuntimeError, match="outside"):
        to_host_path(outside, roots)


def test_mounts_hash_is_shared_across_providers(tmp_path: Path) -> None:
    """A provider swap with the same mounts keeps the same fingerprint."""

    config = _aio_config(tmp_path)
    paths = Paths(tmp_path / ".myflow")
    spec = _aio_host_mount(tmp_path)

    from_aio = build_host_mounts(
        app_config=config,
        host_mount=spec,
        user_id="u1",
        thread_id="t1",
        paths=paths,
    )
    config.sandbox.cube = CubeSandboxConfig(
        template_id="tpl",
        host_mount=CubeSandboxHostMountConfig(
            local_root=str(tmp_path), host_root="/opt/myflow"
        ),
        lease=CubeSandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    )
    from_cube = build_host_mounts(
        app_config=config,
        host_mount=spec,
        user_id="u1",
        thread_id="t1",
        paths=paths,
    )

    assert mounts_hash(from_aio) == mounts_hash(from_cube)


def test_cube_wrapper_still_names_the_cube_section(tmp_path: Path) -> None:
    """The CubeSandbox shim keeps its own field prefix."""

    from agentkit.sandbox.cube.mounts import build_host_mounts as build_cube_mounts

    config = _aio_config(tmp_path)
    config.sandbox.cube = CubeSandboxConfig(
        template_id="tpl",
        host_mount=CubeSandboxHostMountConfig(
            local_root=str(tmp_path), host_root="relative/root"
        ),
        lease=CubeSandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    )

    with pytest.raises(SandboxRuntimeError, match="sandbox.cube.host_mount.host_root"):
        build_cube_mounts(
            app_config=config,
            user_id="u1",
            thread_id="t1",
            paths=Paths(tmp_path / ".myflow"),
        )
