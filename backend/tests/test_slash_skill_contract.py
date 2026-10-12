"""M7 §3.5：Python 解析器与 contracts/slash_skill_contract.json 双向钉死。"""

from __future__ import annotations

import re

from agentkit.skills.parser import SKILL_NAME_RE
from agentkit.skills.slash import (
    RESERVED_SLASH_SKILL_NAMES,
    SlashSkillMatch,
    contract_path,
    load_slash_skill_contract,
    parse_slash_skill,
    slash_skill_contract,
)

SAMPLES = [
    "/pdf-processing 把这个文件转成文本",
    "/pdf-processing",
    "/pdf-processing\targ",
    "/csv-tools select:a,b",
    "/new",
    "/new thread",
    "/models",
    "/agent",
    "/pdf-processingX",
    " /pdf-processing",
    "hello /pdf-processing",
    "/PDF-Processing",
    "/pdf--processing",
    "/",
    "",
    "//pdf",
]


def test_the_shared_json_is_the_python_contract():
    assert load_slash_skill_contract() == slash_skill_contract()
    assert contract_path().is_file()


def test_the_json_regex_parses_every_sample_like_the_python_regex():
    contract = load_slash_skill_contract()
    pattern = re.compile(contract["command_pattern"])
    reserved = set(contract["reserved_names"])

    for sample in SAMPLES:
        match = pattern.match(sample)
        expected = (
            None
            if match is None or match.group(1) in reserved
            else SlashSkillMatch(name=match.group(1), argument=sample[match.end() :].strip())
        )
        assert parse_slash_skill(sample) == expected, sample


def test_reserved_names_match_the_json():
    contract = load_slash_skill_contract()

    assert sorted(RESERVED_SLASH_SKILL_NAMES) == contract["reserved_names"]


def test_the_name_pattern_is_the_frontmatter_name_pattern():
    contract = load_slash_skill_contract()

    assert SKILL_NAME_RE.pattern == contract["name_pattern"]


def test_leading_whitespace_is_not_a_slash_activation():
    assert parse_slash_skill(" /pdf-processing") is None
    assert parse_slash_skill("\t/pdf-processing") is None


def test_a_missing_separator_is_not_a_slash_activation():
    assert parse_slash_skill("/pdf-processingX") is None
    assert parse_slash_skill("/pdf-processing!") is None


def test_a_reserved_command_is_never_a_skill():
    for name in sorted(RESERVED_SLASH_SKILL_NAMES):
        assert parse_slash_skill(f"/{name}") is None
        assert parse_slash_skill(f"/{name} rest") is None


def test_a_valid_command_keeps_the_rest_as_the_argument():
    match = parse_slash_skill("/pdf-processing extract this file")

    assert match == SlashSkillMatch(name="pdf-processing", argument="extract this file")
    assert parse_slash_skill("/pdf-processing").argument == ""
