"""测试用的技能树与配置。"""

from __future__ import annotations

import os
from pathlib import Path

from agentkit.config.skills_config import SkillsConfig

SKILL_TEMPLATE = """---
name: {name}
description: {description}{extra}
---

{body}
"""


def skill_text(
    name: str,
    *,
    description: str = "Does a thing",
    extra_frontmatter: str = "",
    body: str = "# Instructions\n",
) -> str:
    extra = f"\n{extra_frontmatter}" if extra_frontmatter else ""
    return SKILL_TEMPLATE.format(name=name, description=description, extra=extra, body=body)


def write_skill(
    root: Path,
    category: str,
    name: str,
    *,
    description: str = "Does a thing",
    extra_frontmatter: str = "",
    body: str = "# Instructions\n",
    dir_name: str | None = None,
    text: str | None = None,
) -> Path:
    """在 root/skills/<category>/<dir_name or name>/SKILL.md 落一个技能。"""
    skill_dir = root / "skills" / category / (dir_name or name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = text if text is not None else skill_text(
        name, description=description, extra_frontmatter=extra_frontmatter, body=body
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def skills_config(root: Path, **overrides) -> SkillsConfig:
    values: dict = {"path": str(root / "skills")}
    values.update(overrides)
    return SkillsConfig(**values)


def symlinks_available(root: Path) -> bool:
    """Windows 没开开发者模式时 os.symlink 会直接报错；这类用例就地跳过。"""
    target = root / "_symlink_probe_target"
    link = root / "_symlink_probe_link"
    try:
        target.mkdir(exist_ok=True)
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    finally:
        try:
            link.unlink()
        except OSError:
            pass
    return True

APP_CONFIG_TEMPLATE = """config_version: 1
agents:
  default_model: fake
models:
  - name: fake
    use: langchain_core.language_models.fake_chat_models.FakeListChatModel
    model: fake
skills:
  path: {skills_path}
"""


def write_app_config(root: Path, *, skills_path: Path | None = None, **skills_overrides) -> Path:
    """写一份最小的 config.yaml：需要 AppConfig.skills 的代码（网关目录、模型索引）离不开它。"""
    import yaml

    path = root / "config.yaml"
    data = yaml.safe_load(
        APP_CONFIG_TEMPLATE.format(skills_path=str(skills_path or (root / "skills")))
    )
    data["skills"].update(skills_overrides)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path
