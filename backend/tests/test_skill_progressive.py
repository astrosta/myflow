"""M7 §3.5：三级渐进加载 —— L0 索引、L1 元数据、L2 正文。"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool

from agentkit.skills.catalog import DEFAULT_CATALOG_LIMIT, search_skills
from agentkit.skills.describe import describe_payload, describe_skills
from agentkit.skills.prompt import build_skills_section
from agentkit.skills.storage import load_skills
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


def test_l1_returns_metadata_without_the_body(tmp_path):
    skills = _skills(tmp_path)
    pdf = next(skill for skill in skills if skill.name == "pdf-processing")

    payload = describe_payload(pdf)

    assert payload["description"] == "处理 PDF 文档"
    assert payload["allowed_tools"] == ["read_file", "bash"]
    assert payload["argument_hint"] == "<file>"
    assert payload["license"] == "MIT"
    assert payload["dir"] == "/mnt/skills/public/pdf-processing"
    assert payload["category"] == "public"
    assert BODY_MARKER not in json.dumps(payload, ensure_ascii=False)
    assert "body" not in payload


def test_required_secrets_keep_the_optional_flag(tmp_path):
    write_skill(
        tmp_path,
        "public",
        "csv-tools",
        extra_frontmatter="required-secrets: [{name: CSV_API_KEY, optional: true}]",
    )
    skill = load_skills(skills_config(tmp_path), enabled_map={})[0]

    assert describe_payload(skill)["required_secrets"] == [
        {"name": "CSV_API_KEY", "optional": True}
    ]


def test_catalog_select_has_no_cap(tmp_path):
    skills = _skills(tmp_path)

    picked = search_skills(skills, "select:csv-tools,pdf-processing", limit=1)

    assert [skill.name for skill in picked] == ["csv-tools", "pdf-processing"]


def test_catalog_prefix_and_free_text_are_capped(tmp_path):
    for index in range(8):
        write_skill(tmp_path, "public", f"csv-{index}")
    skills = load_skills(skills_config(tmp_path), enabled_map={})

    assert len(search_skills(skills, "+csv-")) == DEFAULT_CATALOG_LIMIT
    assert len(search_skills(skills, "csv")) == DEFAULT_CATALOG_LIMIT
    assert [skill.name for skill in search_skills(skills, "+csv-1")] == ["csv-1"]


def test_free_text_matches_name_and_description(tmp_path):
    skills = _skills(tmp_path)

    assert [skill.name for skill in search_skills(skills, "CSV")] == ["csv-tools"]
    assert [skill.name for skill in search_skills(skills, "PDF")] == ["pdf-processing"]


def test_an_illegal_regex_falls_back_to_a_substring(tmp_path):
    write_skill(tmp_path, "public", "csv-tools", description="C++ (beta) 导出")
    skills = load_skills(skills_config(tmp_path), enabled_map={})

    # "C++ (b" 不是合法 regex：退化成字面子串匹配，仍然命中，也不 500
    assert [skill.name for skill in search_skills(skills, "C++ (b")] == ["csv-tools"]
    assert search_skills(skills, ")))(") == []


def test_describe_skills_returns_payloads(tmp_path):
    skills = _skills(tmp_path)

    payloads = describe_skills(skills, "select:csv-tools")

    assert [item["name"] for item in payloads] == ["csv-tools"]


def test_describe_skill_is_a_framework_tool_with_a_schema(tmp_path):
    from agentkit.tools.builtin.describe_skill import describe_skill

    assert isinstance(describe_skill, BaseTool)
    assert describe_skill.name == "describe_skill"
    # 模型看到的是 tool_call_schema：runtime 是注入参数，不进 schema
    assert "query" in describe_skill.tool_call_schema.model_fields
    assert "runtime" not in describe_skill.tool_call_schema.model_fields
