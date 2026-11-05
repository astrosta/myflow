"""Shared host-mount planning, exercised through the OpenSandbox provider shape.

OpenSandbox still uses real host mounts and its own field prefix. These tests
pin that the shared module works without an E2B config and names the right
section in an error.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.paths import Paths
from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.host_mounts import (
    build_host_mounts,
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


def _config(tmp_path: Path) -> AppConfig:
    """An AppConfig with no sandbox.e2b: the OpenSandbox provider must not need one."""

    return AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
        sandbox=SandboxConfig(),
        skills=_skills(tmp_path),
    )


def _host_mount(tmp_path: Path, **overrides: str) -> SimpleNamespace:
    values = {"local_root": str(tmp_path), "host_root": "/opt/myflow"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_planning_does_not_need_an_e2b_section(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.sandbox.e2b is None

    mounts = build_host_mounts(
        app_config=config,
        host_mount=_host_mount(tmp_path),
        user_id="u1",
        thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
        field_prefix="sandbox.opensandbox.host_mount",
    )

    by_container = {entry.container_path: entry for entry in mounts}
    assert by_container["/mnt/user-data"].host_path == (
        "/opt/myflow/.myflow/users/u1/threads/t1/user-data"
    )
    assert by_container["/mnt/user-data"].read_only is False
    assert by_container["/mnt/skills/private"].read_only is True


def test_host_root_defaults_to_local_root() -> None:
    roots = resolve_host_roots(
        SimpleNamespace(local_root="/opt/myflow", host_root=None),
        field_prefix="sandbox.opensandbox.host_mount",
    )
    assert roots.host_root == PurePosixPath("/opt/myflow")


def test_error_message_names_the_provider_section(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(
        SandboxRuntimeError, match="sandbox.opensandbox.host_mount.host_root"
    ):
        build_host_mounts(
            app_config=config,
            host_mount=_host_mount(tmp_path, host_root="relative/root"),
            user_id="u1",
            thread_id="t1",
            paths=Paths(tmp_path / ".myflow"),
            field_prefix="sandbox.opensandbox.host_mount",
        )


def test_source_outside_local_root_is_rejected(tmp_path: Path) -> None:
    roots = resolve_host_roots(_host_mount(tmp_path))
    outside = tmp_path.parent / "outside-osb"
    outside.mkdir(exist_ok=True)

    with pytest.raises(SandboxRuntimeError, match="outside"):
        to_host_path(outside, roots)
