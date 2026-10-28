import os
from pathlib import Path

import pytest

from agentkit.config.app_config import AppConfig
from agentkit.config.skills_config import SkillsConfig
from agentkit.sandbox.local import local_sandbox_provider as provider_module
from agentkit.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from agentkit.sandbox.security import assert_writable, find_path_mapping, resolve_sandbox_path
from fakes.skill_tree import symlinks_available, write_skill


def test_builtin_tree_is_mounted_read_only(tmp_path: Path, monkeypatch):
    builtin_dir = tmp_path / "skills" / "builtin"
    write_skill(tmp_path, "builtin", "demo", body="body")
    config = AppConfig(skills=SkillsConfig(source="git", builtin={"path": str(builtin_dir)}))
    monkeypatch.setattr(provider_module, "get_app_config", lambda: config)
    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-1", user_id="u1"))

    mapping = find_path_mapping("/mnt/skills/builtin", sandbox.path_mappings)
    assert mapping is not None
    assert mapping.local_path == str(builtin_dir.resolve())
    assert mapping.read_only is True
    assert resolve_sandbox_path("/mnt/skills/builtin/demo/SKILL.md", sandbox.path_mappings) == (builtin_dir / "demo" / "SKILL.md").resolve()
    with pytest.raises(PermissionError):
        assert_writable("/mnt/skills/builtin/demo/SKILL.md", sandbox.path_mappings)


def test_operator_mounts_cannot_shadow_the_builtin_tree(tmp_path: Path, monkeypatch):
    builtin_dir = tmp_path / "skills" / "builtin"
    write_skill(tmp_path, "builtin", "demo", body="body")
    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()

    def fake_flag(name: str, default, config=None):
        if name == "mounts":
            return [
                {"container_path": "/mnt/skills/builtin", "local_path": str(builtin_dir)},
                {"container_path": "/elsewhere", "local_path": str(builtin_dir)},
                {"container_path": "/safe", "local_path": str(safe_dir)},
            ]
        return default

    config = AppConfig(
        skills=SkillsConfig(source="git", builtin={"path": str(builtin_dir)})
    )
    monkeypatch.setattr(provider_module, "get_app_config", lambda: config)
    monkeypatch.setattr(provider_module, "sandbox_flag", fake_flag)
    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-1", user_id="u1"))

    builtin_mappings = [
        mapping
        for mapping in sandbox.path_mappings
        if mapping.container_path == "/mnt/skills/builtin"
    ]
    assert len(builtin_mappings) == 1
    assert builtin_mappings[0].local_path == str(builtin_dir.resolve())
    assert builtin_mappings[0].read_only is True

    containers = {mapping.container_path for mapping in sandbox.path_mappings}
    assert "/safe" in containers
    assert "/elsewhere" not in containers


def test_filesystem_source_keeps_builtin_mount_separate_from_the_tree(
    tmp_path: Path, monkeypatch
):
    outside_root = tmp_path / "builtin-ext"
    builtin_dir = outside_root / "skills" / "builtin"
    write_skill(outside_root, "builtin", "demo", body="body")
    tree = tmp_path / "skills"
    (tree / "public" / "demo").mkdir(parents=True)

    config = AppConfig(
        skills=SkillsConfig(
            source="filesystem",
            path=str(tree),
            builtin={"path": str(builtin_dir)},
        )
    )
    monkeypatch.setattr(provider_module, "get_app_config", lambda: config)
    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-1", user_id="u1"))

    tree_mapping = find_path_mapping("/mnt/skills", sandbox.path_mappings)
    assert tree_mapping is not None
    assert tree_mapping.local_path == str(tree.resolve())

    builtin_mapping = find_path_mapping("/mnt/skills/builtin", sandbox.path_mappings)
    assert builtin_mapping is not None
    assert builtin_mapping.local_path == str(builtin_dir.resolve())
    assert builtin_mapping.read_only is True

    resolved = resolve_sandbox_path(
        "/mnt/skills/builtin/demo/SKILL.md", sandbox.path_mappings
    )
    assert resolved == (builtin_dir / "demo" / "SKILL.md").resolve()
    assert not resolved.is_relative_to(tree.resolve())


def test_symlinked_builtin_root_is_not_mounted(tmp_path: Path, monkeypatch):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable")
    target_parent = tmp_path / "outside"
    write_skill(target_parent, "builtin", "demo", body="body")
    target = target_parent / "skills" / "builtin"
    builtin_dir = tmp_path / "skills" / "builtin"
    builtin_dir.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, builtin_dir, target_is_directory=True)

    config = AppConfig(
        skills=SkillsConfig(source="git", builtin={"path": str(builtin_dir)})
    )
    monkeypatch.setattr(provider_module, "get_app_config", lambda: config)
    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-1", user_id="u1"))

    assert find_path_mapping("/mnt/skills/builtin", sandbox.path_mappings) is None
    assert all(
        mapping.local_path != str(target.resolve()) for mapping in sandbox.path_mappings
    )


def test_unusable_builtin_root_is_not_mounted(tmp_path: Path, monkeypatch):
    builtin_dir = tmp_path / "skills" / "builtin"
    write_skill(tmp_path, "builtin", "demo", body="body")
    config = AppConfig(
        skills=SkillsConfig(source="git", builtin={"path": str(builtin_dir)})
    )

    def boom(_config):
        raise ValueError("unsafe built-in root")

    monkeypatch.setattr(provider_module, "get_app_config", lambda: config)
    monkeypatch.setattr(provider_module, "resolve_builtin_root", boom)
    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-1", user_id="u1"))

    assert find_path_mapping("/mnt/skills/builtin", sandbox.path_mappings) is None
    assert all(
        mapping.local_path != str(builtin_dir.resolve())
        for mapping in sandbox.path_mappings
    )


def test_disabled_builtin_skills_are_not_mounted(tmp_path: Path, monkeypatch):
    builtin_dir = tmp_path / "skills" / "builtin"
    write_skill(tmp_path, "builtin", "demo", body="body")
    config = AppConfig(
        skills=SkillsConfig(
            source="git",
            builtin={"enabled": False, "path": str(builtin_dir)},
        )
    )
    monkeypatch.setattr(provider_module, "get_app_config", lambda: config)
    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-1", user_id="u1"))

    assert find_path_mapping("/mnt/skills/builtin", sandbox.path_mappings) is None
    assert all(
        mapping.local_path != str(builtin_dir.resolve())
        for mapping in sandbox.path_mappings
    )
