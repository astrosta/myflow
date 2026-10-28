"""共享技能目录扫描器（M7 §3.4 / §3.1）：包边界、非法目录名、符号链接规则。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentkit.skills.domain import SkillScope
from agentkit.skills.scan import scan_skill_category
from agentkit.skills.types import SkillCategory
from fakes.skill_tree import symlinks_available, write_skill


def test_scan_skill_category_stops_at_package_boundary(tmp_path: Path):
    write_skill(tmp_path, "builtin", "one", description="one")
    write_skill(tmp_path, "builtin", "one", dir_name="one/examples")

    skills = scan_skill_category(
        tmp_path / "skills" / "builtin",
        category=SkillCategory.BUILTIN,
        container_path="/mnt/skills",
        scope=SkillScope.BUILTIN,
        enabled=True,
        allow_symlink_dirs=False,
    )

    assert [skill.name for skill in skills] == ["one"]
    assert skills[0].skill_file == tmp_path / "skills" / "builtin" / "one" / "SKILL.md"


def test_scan_skill_category_rejects_a_bad_directory_name(tmp_path: Path):
    write_skill(tmp_path, "builtin", "bad name", dir_name="bad name")
    skills = scan_skill_category(
        tmp_path / "skills" / "builtin",
        category=SkillCategory.BUILTIN,
        container_path="/mnt/skills",
    )
    assert skills == []


def test_scan_skill_category_rejects_symlinked_dirs_when_disallowed(tmp_path: Path):
    if not symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable")
    target = tmp_path / "outside"
    target.mkdir()
    (target / "SKILL.md").write_text(
        "---\nname: linked\ndescription: linked\n---\n# body\n",
        encoding="utf-8",
    )
    root = tmp_path / "skills" / "builtin"
    root.mkdir(parents=True)
    os.symlink(target, root / "linked", target_is_directory=True)

    assert scan_skill_category(
        root,
        category=SkillCategory.BUILTIN,
        container_path="/mnt/skills",
        allow_symlink_dirs=False,
    ) == []


def test_scan_skill_category_ignores_files_and_dotfiles(tmp_path: Path, caplog):
    root = tmp_path / "skills" / "builtin"
    root.mkdir(parents=True)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    (root / "README.md").write_text("not a skill", encoding="utf-8")

    with caplog.at_level("WARNING", logger="agentkit.skills.scan"):
        assert scan_skill_category(
            root,
            category=SkillCategory.BUILTIN,
            container_path="/mnt/skills",
        ) == []

    assert not caplog.records, "占位文件不是非法技能目录，不应触发 warning"


def test_scan_skill_category_skips_malformed_yaml_frontmatter(tmp_path: Path):
    write_skill(tmp_path, "builtin", "good", description="good")
    write_skill(
        tmp_path,
        "builtin",
        "broken",
        text="---\nname: broken\ndescription: [unclosed\n---\n# body\n",
    )

    skills = scan_skill_category(
        tmp_path / "skills" / "builtin",
        category=SkillCategory.BUILTIN,
        container_path="/mnt/skills",
    )

    assert [skill.name for skill in skills] == ["good"]


def test_scan_skill_category_skips_non_utf8_skill_file(tmp_path: Path):
    write_skill(tmp_path, "builtin", "good", description="good")
    broken = write_skill(tmp_path, "builtin", "broken", description="broken")
    (broken / "SKILL.md").write_bytes(
        b"---\nname: broken\ndescription: broken\n---\n# body\n\xff\n"
    )

    skills = scan_skill_category(
        tmp_path / "skills" / "builtin",
        category=SkillCategory.BUILTIN,
        container_path="/mnt/skills",
    )

    assert [skill.name for skill in skills] == ["good"]
