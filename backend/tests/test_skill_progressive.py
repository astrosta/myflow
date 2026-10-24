"""M7 §3.5：技能的两级加载 —— L0 索引（名字/描述）与正文（按需）。

**没有 L1 元数据层**：索引已经给了"有没有、多大、要不要密钥、目录在哪"，
剩下的信息只在正文里，所以只有"读正文"这一级。
"""

from __future__ import annotations

from agentkit.skills.prompt import build_skills_section
from agentkit.skills.storage import load_skills
from agentkit.tools.registry import get_available_tools
from agentkit.config import AppConfig
from fakes.skill_tree import skills_config, write_skill

BODY_MARKER = "SECRET-BODY-MARKER"


def _skills(tmp_path):
    write_skill(
        tmp_path,
        "public",
        "pdf-processing",
        description="处理 PDF 文档",
        extra_frontmatter='allowed-tools: [read_file, bash]\nargument-hint: "<file>"\nlicense: MIT',
        body=f"# 说明\n{BODY_MARKER}\n",
    )
    write_skill(tmp_path, "public", "csv-tools", description="处理 CSV")
    return load_skills(skills_config(tmp_path), enabled_map={})


def test_l0_holds_no_body_and_no_allowed_tools(tmp_path):
    _skills(tmp_path)
    section = build_skills_section(config=skills_config(tmp_path))

    assert "pdf-processing" in section
    assert BODY_MARKER not in section
    assert "allowed-tools" not in section
    assert "argument-hint" not in section
    assert "MIT" not in section


def test_l0_is_the_only_place_the_index_appears(tmp_path):
    """索引之外没有第二个"元数据工具"：技能相关工具只剩按需加载那一个。"""
    _skills(tmp_path)

    names = {
        tool.name
        for tool in get_available_tools(
            include_mcp=False, app_config=AppConfig(skills=skills_config(tmp_path))
        )
    }

    assert "search_and_load_skills" in names
    assert "describe_skill" not in names
