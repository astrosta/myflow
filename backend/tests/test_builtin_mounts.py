from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit.config.app_config import AppConfig
from agentkit.config.skills_config import SkillsConfig
from agentkit.sandbox.local import local_sandbox_provider as provider_module
from agentkit.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from agentkit.sandbox.security import assert_writable, find_path_mapping, resolve_sandbox_path
from fakes.skill_tree import write_skill


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
