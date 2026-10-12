"""M7 §3.3：allowed-tools 与 required-secrets 的解析。"""

from __future__ import annotations

import pytest

from agentkit.skills.parser import (
    SkillParseError,
    parse_allowed_tools,
    parse_required_secrets,
    parse_skill_text,
)
from fakes.skill_tree import skill_text


def test_transportable_aliases_become_runtime_names():
    assert parse_allowed_tools("Bash Read Write Edit Glob Grep WebFetch WebSearch") == (
        "bash",
        "read_file",
        "write_file",
        "str_replace",
        "glob",
        "grep",
        "web_fetch",
        "web_search",
    )


def test_lowercase_and_unknown_tokens_are_kept_verbatim():
    assert parse_allowed_tools(["read_file", "MyTool"]) == ("read_file", "MyTool")


def test_none_and_empty_list_are_different():
    assert parse_allowed_tools(None) is None  # 未声明
    assert parse_allowed_tools([]) == ()  # 声明了空列表
    assert parse_allowed_tools("   ") == ()


def test_parameter_scoped_entries_are_preserved_and_never_alias_mapped():
    """策略不检查参数：Bash(git *) 原样保留，因此永远匹配不上，等于不生效。"""
    assert parse_allowed_tools("Bash(git *) Read") == ("Bash(git *)", "read_file")


def test_quotes_spaces_and_brackets_survive_tokenizing():
    assert parse_allowed_tools('MyTool(a b "c d") Read') == ('MyTool(a b "c d")', "read_file")
    # 引号里的右括号是字面量，不能把 depth 算错
    assert parse_allowed_tools('MyTool("a ) b") Read') == ('MyTool("a ) b")', "read_file")


def test_unclosed_brackets_and_quotes_raise():
    for raw in ("Bash(git *", "Read 'unclosed", "Write(ok) )"):
        with pytest.raises(SkillParseError):
            parse_allowed_tools(raw)


def test_a_non_string_non_list_value_is_an_error():
    with pytest.raises(SkillParseError):
        parse_allowed_tools({"Bash": True})


def test_malformed_required_secrets_are_dropped_not_fatal():
    parsed = parse_required_secrets(
        ["GOOD_ONE", {"name": "GOOD_TWO", "optional": True}, "bad-name", 7, {"optional": True}]
    )

    assert [(item.name, item.optional) for item in parsed] == [
        ("GOOD_ONE", False),
        ("GOOD_TWO", True),
    ]


def test_required_secrets_accepts_a_single_mapping():
    parsed = parse_required_secrets({"name": "API_KEY", "optional": True})

    assert [(item.name, item.optional) for item in parsed] == [("API_KEY", True)]


def test_frontmatter_is_parsed_and_body_kept_whole():
    parsed = parse_skill_text(
        skill_text(
            "pdf-processing",
            description="处理 PDF 文档",
            extra_frontmatter="allowed-tools: [read_file, bash]\nargument-hint: \"<file>\"\nlicense: MIT",
            body="# 正文\n内容\n",
        )
    )

    assert parsed.name == "pdf-processing"
    assert parsed.description == "处理 PDF 文档"
    assert parsed.allowed_tools == ("read_file", "bash")
    assert parsed.argument_hint == "<file>"
    assert parsed.license == "MIT"
    assert parsed.body.strip().startswith("# 正文")


def test_secrets_autonomous_only_explicit_false_turns_it_off():
    assert parse_skill_text(skill_text("a")).secrets_autonomous is True
    assert (
        parse_skill_text(skill_text("a", extra_frontmatter="secrets-autonomous: false")).secrets_autonomous
        is False
    )
    # 畸形值 fail-closed（§3.3）
    assert (
        parse_skill_text(skill_text("a", extra_frontmatter='secrets-autonomous: "maybe"')).secrets_autonomous
        is False
    )


def test_a_yaml_boolean_name_is_rejected_with_a_hint():
    """name: off 会被 YAML 读成 False —— 报错要能指向真正的原因。"""
    with pytest.raises(SkillParseError, match="quote bare on/off/yes/no"):
        parse_skill_text(skill_text("off"))


def test_missing_frontmatter_and_bad_names_raise():
    with pytest.raises(SkillParseError, match="missing frontmatter"):
        parse_skill_text("# no frontmatter\n")
    with pytest.raises(SkillParseError, match="bad skill name"):
        parse_skill_text(skill_text("Bad Name"))
