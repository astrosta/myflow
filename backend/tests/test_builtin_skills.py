from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX
from agentkit.config.runtime_paths import resolve_path
from agentkit.config.skills_config import BuiltinSkillsConfig, SkillsConfig
from agentkit.skills.domain import SkillScope
from agentkit.skills.types import SkillCategory


def test_builtin_config_defaults_and_enum_values():
    config = SkillsConfig()
    assert config.builtin.enabled is True
    assert config.builtin.path == "skills/builtin"
    assert config.builtin.root_dir() == resolve_path("skills/builtin")
    assert SkillScope.BUILTIN == "builtin"
    assert SkillCategory.BUILTIN == "builtin"


def test_builtin_fields_are_startup_only():
    paths = ("skills.builtin.enabled", "skills.builtin.path")
    assert set(paths) <= STARTUP_ONLY_FIELDS.keys()

    for path in paths:
        description = BuiltinSkillsConfig.model_fields[path.rsplit(".", 1)[-1]].description or ""
        assert description.startswith(f"{STARTUP_ONLY_PREFIX} {STARTUP_ONLY_FIELDS[path]}")
