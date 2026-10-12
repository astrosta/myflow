"""M7 §3.4：扫描的两个容易忽略的规则 + 名字/分类覆盖。"""

from __future__ import annotations

import os

import pytest

from agentkit.skills.storage import load_skills, validate_skill_file_path
from agentkit.skills.types import Skill, SkillCategory, SkillPathError
from fakes.skill_tree import skill_text, skills_config, symlinks_available, write_skill


def _names(root, enabled_map=None):
    skills = load_skills(skills_config(root), enabled_map=enabled_map or {})
    return sorted((skill.name, skill.category.value, skill.enabled) for skill in skills)


def test_a_nested_skill_md_is_not_a_skill(tmp_path):
    write_skill(tmp_path, "public", "pdf-processing")
    nested = tmp_path / "skills" / "public" / "pdf-processing" / "examples" / "demo"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(skill_text("demo"), encoding="utf-8")

    assert _names(tmp_path) == [("pdf-processing", "public", True)]


def test_custom_overrides_public(tmp_path):
    write_skill(tmp_path, "public", "csv-tools", description="public 版")
    write_skill(tmp_path, "custom", "csv-tools", description="custom 版")

    skills = load_skills(skills_config(tmp_path), enabled_map={})

    assert len(skills) == 1
    assert skills[0].category is SkillCategory.CUSTOM
    assert skills[0].description == "custom 版"


def test_a_first_level_symlinked_skill_directory_is_usable(tmp_path):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks are not permitted on this host")
    outside = tmp_path / "ops-skills" / "pdf-processing"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text(skill_text("pdf-processing"), encoding="utf-8")
    category = tmp_path / "skills" / "public"
    category.mkdir(parents=True)
    os.symlink(outside, category / "pdf-processing", target_is_directory=True)

    assert _names(tmp_path) == [("pdf-processing", "public", True)]


def test_a_symlinked_skill_file_is_refused(tmp_path):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks are not permitted on this host")
    write_skill(tmp_path, "public", "real")
    real_file = tmp_path / "skills" / "public" / "real" / "SKILL.md"
    escaped = tmp_path / "skills" / "public" / "evil"
    escaped.mkdir(parents=True)
    os.symlink(real_file, escaped / "SKILL.md")

    assert _names(tmp_path) == [("real", "public", True)]


def test_a_deeper_escape_is_refused(tmp_path):
    """SKILL.md 必须落在（解析后的）技能目录里。"""
    skill = Skill(
        name="escapee",
        description="",
        license=None,
        skill_dir=tmp_path / "skills" / "public" / "escapee",
        skill_file=tmp_path / "elsewhere" / "SKILL.md",
        relative_path=os.path.relpath(tmp_path / "skills" / "public" / "escapee", tmp_path / "skills"),
        category=SkillCategory.PUBLIC,
    )

    with pytest.raises(SkillPathError):
        validate_skill_file_path(skill)


def test_validate_refuses_a_missing_file(tmp_path):
    skill = Skill(
        name="ghost",
        description="",
        license=None,
        skill_dir=tmp_path,
        skill_file=tmp_path / "SKILL.md",
        relative_path=tmp_path.name,
        category=SkillCategory.PUBLIC,
    )

    with pytest.raises(SkillPathError):
        validate_skill_file_path(skill)


def test_chinese_frontmatter_is_read_as_utf8(tmp_path):
    write_skill(tmp_path, "public", "pdf-processing", description="处理 PDF 文档，支持中文")

    skills = load_skills(skills_config(tmp_path), enabled_map={})

    assert skills[0].description == "处理 PDF 文档，支持中文"


def test_frontmatter_name_wins_over_the_directory_name(tmp_path):
    write_skill(tmp_path, "public", "real-name", dir_name="My_Skill")

    assert _names(tmp_path) == [("real-name", "public", True)]


def test_an_illegal_directory_name_is_skipped(tmp_path):
    write_skill(tmp_path, "public", "bad", dir_name="bad name")
    write_skill(tmp_path, "public", "good")

    assert _names(tmp_path) == [("good", "public", True)]


def test_one_broken_skill_does_not_take_down_the_rest(tmp_path):
    write_skill(tmp_path, "public", "no-frontmatter", text="# 没有 frontmatter\n")
    write_skill(tmp_path, "public", "BadName")
    write_skill(tmp_path, "public", "good")

    assert _names(tmp_path) == [("good", "public", True)]


def test_directory_without_skill_md_is_ignored(tmp_path):
    (tmp_path / "skills" / "public" / "empty").mkdir(parents=True)

    assert _names(tmp_path) == []


def test_enabled_defaults_to_true_and_comes_from_the_map(tmp_path):
    write_skill(tmp_path, "public", "one")
    write_skill(tmp_path, "public", "two")

    assert _names(tmp_path) == [("one", "public", True), ("two", "public", True)]
    assert _names(tmp_path, {"one": False}) == [("one", "public", False), ("two", "public", True)]


def test_every_call_rescans_the_disk(tmp_path):
    write_skill(tmp_path, "public", "one")
    assert [skill.name for skill in load_skills(skills_config(tmp_path), enabled_map={})] == ["one"]

    write_skill(tmp_path, "custom", "two")

    assert sorted(skill.name for skill in load_skills(skills_config(tmp_path), enabled_map={})) == [
        "one",
        "two",
    ]


def test_relative_paths_and_container_paths(tmp_path):
    write_skill(tmp_path, "public", "pdf-processing")

    skill = load_skills(
        skills_config(tmp_path, container_path="/mnt/skills"), enabled_map={}
    )[0]

    assert skill.relative_path.as_posix() == "pdf-processing"
    assert skill.container_dir == "/mnt/skills/public/pdf-processing"
    assert skill.container_file == "/mnt/skills/public/pdf-processing/SKILL.md"
    assert str(tmp_path) not in skill.container_dir  # 宿主路径绝不进容器路径
