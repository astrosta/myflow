import os
from pathlib import Path

import pytest

from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX
from agentkit.config.runtime_paths import resolve_path
from agentkit.config.skills_config import BuiltinSkillsConfig, SkillsConfig
from agentkit.skills.builtin import builtin_names, clear_builtin_cache, load_builtin_skills
from agentkit.skills.domain import SkillScope
from agentkit.skills.types import SkillCategory
from fakes.skill_tree import skills_config, write_skill


@pytest.fixture(autouse=True)
def _clear_builtin_cache():
    clear_builtin_cache()
    yield
    clear_builtin_cache()


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


def test_builtin_loader_sets_scope_and_container_path(tmp_path: Path):
    write_skill(tmp_path, "builtin", "demo", description="Demo skill")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})

    skills = load_builtin_skills(config)

    assert [skill.name for skill in skills] == ["demo"]
    assert skills[0].scope is SkillScope.BUILTIN
    assert skills[0].category is SkillCategory.BUILTIN
    assert skills[0].enabled is True
    assert skills[0].container_file == "/mnt/skills/builtin/demo/SKILL.md"
    assert builtin_names(config) == frozenset({"demo"})


def test_builtin_loader_is_disabled_by_config(tmp_path: Path):
    write_skill(tmp_path, "builtin", "demo")
    config = skills_config(tmp_path, builtin={"enabled": False, "path": str(tmp_path / "skills" / "builtin")})
    assert load_builtin_skills(config) == ()


def test_builtin_loader_warns_once_for_a_missing_root(tmp_path: Path, caplog):
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "missing")})
    with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
        assert load_builtin_skills(config) == ()
        assert load_builtin_skills(config) == ()
    assert sum("built-in skills directory" in record.message for record in caplog.records) == 1


def test_missing_root_recovers_after_the_directory_appears(tmp_path: Path):
    config = skills_config(
        tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")}
    )
    assert load_builtin_skills(config) == ()
    write_skill(tmp_path, "builtin", "demo")
    assert [skill.name for skill in load_builtin_skills(config)] == ["demo"]


def test_builtin_cache_invalidates_when_skill_md_changes(tmp_path: Path):
    write_skill(tmp_path, "builtin", "demo", description="first")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
    first = load_builtin_skills(config)
    skill_file = tmp_path / "skills" / "builtin" / "demo" / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo\ndescription: second\n---\n# second\n", encoding="utf-8"
    )
    future = 2_000_000_000_000_000_000
    os.utime(skill_file, ns=(future, future))

    second = load_builtin_skills(config)

    assert first[0].description == "first"
    assert second[0].description == "second"
