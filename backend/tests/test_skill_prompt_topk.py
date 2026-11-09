"""系统提示词的技能索引：私有技能超过 prompt_top_k 时只列最常用的那几个。

选择规则（设计 §12）：
- prompt_top_k=0：私有技能不列，且不读 Redis；
- 私有技能数 <= prompt_top_k（N>0）：全列，不读 Redis；
- 超了：先按 Redis ZSET 的 top-K，缺的用 PostgreSQL 的 load_count 兜底，再按名字稳定排序；
- 内置技能永远在索引里，且不占配额。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.skills.domain import SkillScope
from agentkit.skills.prompt import build_skills_section, clear_skill_prompt_cache
from agentkit.skills.types import Skill, SkillCategory
from fakes.skill_tree import skills_config


class FakeRegistry:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = list(skills)

    def visible(self) -> list[Skill]:
        return list(self._skills)


def _skill(
    name: str,
    *,
    skill_id: str | None = None,
    load_count: int = 0,
    scope: SkillScope = SkillScope.PRIVATE,
    category: SkillCategory = SkillCategory.CUSTOM,
) -> Skill:
    directory = Path("/catalog") / name
    return Skill(
        name=name,
        description=f"{name} 的说明",
        license=None,
        skill_dir=directory,
        skill_file=directory / "SKILL.md",
        relative_path=Path(name),
        category=category,
        scope=scope,
        skill_id=skill_id or f"id-{name}",
        owner_user_id="u1" if scope is SkillScope.PRIVATE else None,
        version_id="ver-1" if scope is SkillScope.PRIVATE else None,
        load_count=load_count,
    )


def _listed(section: str) -> list[str]:
    return [
        line.split(" ", 2)[1]
        for line in section.splitlines()
        if line.startswith("- ")
    ]


def _render(
    tmp_path,
    skills: list[Skill],
    *,
    top_ids: tuple[str, ...] | None,
    monkeypatch: pytest.MonkeyPatch,
    prompt_top_k: int = 3,
    reads_usage: bool = True,
) -> list[str]:
    seen: list[str | None] = []

    def fake_top_skill_ids(user_id: str | None):
        seen.append(user_id)
        return top_ids

    monkeypatch.setattr("agentkit.skills.prompt.top_skill_ids", fake_top_skill_ids)
    section = build_skills_section(
        config=skills_config(tmp_path, usage={"prompt_top_k": prompt_top_k}),
        registry=FakeRegistry(skills),
        user_id="u1",
    )
    expected = ["u1"] if reads_usage else []
    assert seen == expected, "超过 prompt_top_k 时才按当前用户读 top-K"
    return _listed(section)


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_skill_prompt_cache()
    yield
    clear_skill_prompt_cache()


def test_a_short_list_needs_no_usage_data(tmp_path, monkeypatch) -> None:
    def forbidden(_user_id):
        raise AssertionError("私有技能没超过 prompt_top_k 时不该读使用次数")

    monkeypatch.setattr("agentkit.skills.prompt.top_skill_ids", forbidden)
    section = build_skills_section(
        config=skills_config(tmp_path, usage={"prompt_top_k": 3}),
        registry=FakeRegistry([_skill("a"), _skill("b"), _skill("c")]),
        user_id="u1",
    )

    assert _listed(section) == ["a", "b", "c"]


def test_zero_lists_only_non_private_skills_and_skips_usage(tmp_path, monkeypatch) -> None:
    def forbidden(_user_id):
        raise AssertionError("prompt_top_k=0 时不该读使用次数")

    monkeypatch.setattr("agentkit.skills.prompt.top_skill_ids", forbidden)
    builtin = _skill(
        "demo",
        skill_id="ignored-builtin-id",
        scope=SkillScope.BUILTIN,
        category=SkillCategory.BUILTIN,
    )
    section = build_skills_section(
        config=skills_config(tmp_path, usage={"prompt_top_k": 0}),
        registry=FakeRegistry([builtin, _skill("a"), _skill("b")]),
        user_id="u1",
    )

    assert _listed(section) == ["demo"]
    assert "私有技能不列在本索引里" in section


def test_zero_is_a_valid_prompt_top_k(tmp_path) -> None:
    config = skills_config(tmp_path, usage={"prompt_top_k": 0})

    assert config.usage.prompt_top_k == 0


def test_over_the_limit_lists_the_redis_top_k_first(tmp_path, monkeypatch) -> None:
    skills = [_skill(name) for name in ("a", "b", "c", "d", "e")]

    listed = _render(
        tmp_path, skills, top_ids=("id-e", "id-b"), monkeypatch=monkeypatch
    )

    # Redis 的两条必进；还差一条，用 load_count 兜底（全 0 → 名字序 → a）。
    # 索引本身按 (category, name) 排序，这里只比集合。
    assert set(listed) == {"a", "b", "e"}


def test_a_cold_cache_falls_back_to_the_database_count(tmp_path, monkeypatch) -> None:
    skills = [
        _skill("a", load_count=1),
        _skill("b", load_count=9),
        _skill("c", load_count=4),
        _skill("d", load_count=7),
    ]

    listed = _render(tmp_path, skills, top_ids=None, monkeypatch=monkeypatch)

    assert set(listed) == {"b", "d", "c"}


def test_equal_counts_break_ties_by_name(tmp_path, monkeypatch) -> None:
    skills = [
        _skill("zeta", load_count=2),
        _skill("alpha", load_count=2),
        _skill("mike", load_count=2),
        _skill("beta", load_count=2),
    ]

    listed = _render(tmp_path, skills, top_ids=None, monkeypatch=monkeypatch)

    assert set(listed) == {"alpha", "beta", "mike"}


def test_redis_ids_that_no_longer_exist_are_skipped(tmp_path, monkeypatch) -> None:
    skills = [_skill("a"), _skill("b"), _skill("c"), _skill("d")]

    listed = _render(
        tmp_path,
        skills,
        top_ids=("id-deleted", "id-c", "id-deleted"),
        monkeypatch=monkeypatch,
    )

    assert set(listed) == {"a", "b", "c"}


def test_builtins_are_always_listed_and_do_not_use_the_quota(tmp_path, monkeypatch) -> None:
    builtin = _skill(
        "demo",
        skill_id="builtin-demo",
        scope=SkillScope.BUILTIN,
        category=SkillCategory.BUILTIN,
    )
    skills = [builtin, *(_skill(name) for name in ("a", "b", "c", "d"))]

    listed = _render(
        tmp_path, skills, top_ids=("id-d",), monkeypatch=monkeypatch, prompt_top_k=2
    )

    assert set(listed) == {"demo", "d", "a"}  # 1 内置 + 2 私有


def test_the_top_k_setting_is_read_from_config(tmp_path, monkeypatch) -> None:
    skills = [_skill(name) for name in ("a", "b", "c", "d")]

    listed = _render(
        tmp_path,
        skills,
        top_ids=("id-a", "id-b", "id-c", "id-d"),
        monkeypatch=monkeypatch,
        prompt_top_k=4,
        reads_usage=False,
    )

    assert set(listed) == {"a", "b", "c", "d"}
