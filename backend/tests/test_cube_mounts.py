from pathlib import Path

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.sandbox_config import (
    CubeSandboxConfig,
    CubeSandboxHostMountConfig,
    CubeSandboxLeaseConfig,
)
from agentkit.config.paths import Paths
from agentkit.sandbox.cube.mounts import build_host_mounts, mounts_hash


def _config(tmp_path: Path, *, source: str = "git") -> AppConfig:
    skills = SkillsConfig(
        source=source,
        path=str(tmp_path / "skills"),
        git={"cache_dir": str(tmp_path / "cache")},
        builtin={"path": str(tmp_path / "skills" / "builtin")},
    )
    (tmp_path / "skills" / "builtin").mkdir(parents=True)
    cube = CubeSandboxConfig(
        template_id="tpl",
        host_mount=CubeSandboxHostMountConfig(
            local_root=str(tmp_path), host_root="/data/shared/myflow"
        ),
        lease=CubeSandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    )
    return AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
        sandbox=SandboxConfig(cube=cube), skills=skills,
    )


def test_same_host_user_data_and_builtin_mounts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    by_container = {m.container_path: m for m in mounts}
    assert by_container["/mnt/user-data"].host_path == "/data/shared/myflow/.myflow/users/u1/threads/t1/user-data"
    assert by_container["/mnt/user-data"].read_only is False
    assert by_container["/mnt/skills/builtin"].read_only is True


def test_git_private_tree_is_created_and_read_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    private = next(m for m in mounts if m.container_path == "/mnt/skills/private")
    assert private.read_only is True
    assert private.host_path == "/data/shared/myflow/cache/active/users/u1"
    assert (tmp_path / "cache" / "active" / "users" / "u1").is_dir()


def test_filesystem_builtin_inside_tree_is_not_double_mounted(tmp_path: Path) -> None:
    config = _config(tmp_path, source="filesystem")
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    assert [m.container_path for m in mounts].count("/mnt/skills/builtin") == 0


def test_operator_mount_cannot_shadow_user_data_or_skills(tmp_path: Path) -> None:
    config = _config(tmp_path)
    safe = tmp_path / "safe"
    safe.mkdir()
    config.sandbox.mounts = [
        {"container_path": "/mnt/user-data", "local_path": str(safe)},
        {"container_path": "/mnt/skills/builtin", "local_path": str(safe)},
        {"container_path": "/safe", "local_path": str(safe), "read_only": True},
    ]
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    safe_mount = next(m for m in mounts if m.container_path == "/safe")
    assert safe_mount.read_only is True


def test_mount_source_outside_local_root_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    config.sandbox.mounts = [{"container_path": "/safe", "local_path": str(outside)}]
    with pytest.raises(Exception, match="outside"):
        build_host_mounts(
            app_config=config, user_id="u1", thread_id="t1",
            paths=Paths(tmp_path / ".myflow"),
        )


def test_mounts_hash_is_order_independent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mounts = list(build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    ))
    assert mounts_hash(mounts) == mounts_hash(list(reversed(mounts)))


def test_mount_payload_uses_cube_host_mount_field_names() -> None:
    from agentkit.sandbox.cube.mounts import MountEntry

    entry = MountEntry("/mnt/user-data", "/host/user-data", False)
    assert entry.as_payload() == {
        "hostPath": "/host/user-data",
        "mountPath": "/mnt/user-data",
        "readOnly": False,
    }


@pytest.mark.parametrize("host_root", ["/", "relative/root", "/data/../escape"])
def test_invalid_host_root_is_rejected(tmp_path: Path, host_root: str) -> None:
    from agentkit.sandbox.exceptions import SandboxRuntimeError

    config = _config(tmp_path)
    config.sandbox.cube.host_mount.host_root = host_root
    with pytest.raises(SandboxRuntimeError):
        build_host_mounts(
            app_config=config, user_id="u1", thread_id="t1",
            paths=Paths(tmp_path / ".myflow"),
        )


def test_generic_thread_uses_local_user_data_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id=None,
        paths=Paths(tmp_path / ".myflow"),
    )
    user_data = next(m for m in mounts if m.container_path == "/mnt/user-data")
    assert user_data.host_path == "/data/shared/myflow/.myflow/users/u1/local/user-data"
    assert (tmp_path / ".myflow" / "users" / "u1" / "local" / "user-data").is_dir()


def test_validate_mount_sources_does_not_create_per_user_dirs(tmp_path: Path) -> None:
    from agentkit.sandbox.cube.mounts import resolve_host_roots, validate_mount_sources

    config = _config(tmp_path)
    roots = resolve_host_roots(config.sandbox.cube)
    validate_mount_sources(config, roots)
    assert not (tmp_path / ".myflow").exists()
    assert not (tmp_path / "cache" / "active").exists()


def test_operator_mount_on_skills_cache_root_is_skipped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cache = tmp_path / "cache"
    config.sandbox.mounts = [{"container_path": "/cache", "local_path": str(cache)}]
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    assert not any(m.container_path == "/cache" for m in mounts)
