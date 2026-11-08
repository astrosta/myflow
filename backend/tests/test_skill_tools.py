"""Split skill tools: search_skills returns metadata; load_skills returns bodies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime

from agentkit.config import AppConfig, set_app_config
from agentkit.config.skills_config import SkillsConfig
from agentkit.skills.domain import SkillScope
from agentkit.skills.search import (
    SkillSearchHit,
    SkillSearchOutcome,
    configure_skill_runtime,
    reset_skill_runtime,
)
from agentkit.skills.types import Skill, SkillCategory
from agentkit.tools.builtin.load_skills import NOT_FOUND_MESSAGE, load_skills
from agentkit.tools.builtin.search_skills import UNAVAILABLE_MESSAGE, search_skills
from agentkit.tools.registry import get_available_tools
from fakes.skill_tree import skills_config, write_skill

BODY = "# 排查\n先查 pg_stat_activity，再清连接池。\n"
SKILL = "pg-multitenant"


@pytest.fixture(autouse=True)
def _restore_config():
    import agentkit.config.app_config as app_config_module

    saved = (
        app_config_module._app_config,
        app_config_module._app_config_path,
        app_config_module._app_config_signature,
        app_config_module._app_config_is_custom,
    )
    yield
    (
        app_config_module._app_config,
        app_config_module._app_config_path,
        app_config_module._app_config_signature,
        app_config_module._app_config_is_custom,
    ) = saved
    reset_skill_runtime()


def _install(skills: SkillsConfig) -> AppConfig:
    config = AppConfig(skills=skills)
    set_app_config(config)
    return config


def _runtime(user_id: str | None = "u1") -> ToolRuntime:
    return ToolRuntime(
        state={},
        context={"user_id": user_id} if user_id else {},
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id="call-1",
        store=None,
    )


def _invoke(tool, args: dict) -> dict:
    result = asyncio.run(
        tool.ainvoke({"name": tool.name, "args": args, "id": "call-1", "type": "tool_call"})
    )
    return json.loads(result.content if hasattr(result, "content") else result)


class FakeSearchService:
    def __init__(self, outcome: SkillSearchOutcome, loaded: SkillSearchHit | None = None):
        self.outcome = outcome
        self.loaded = loaded
        self.search_calls: list[dict] = []
        self.load_calls: list[dict] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.outcome

    async def load_skill(self, **kwargs):
        self.load_calls.append(kwargs)
        return self.loaded


def _skill(name: str = SKILL, *, enabled: bool = True) -> Skill:
    return Skill(
        name=name,
        description="PostgreSQL 多租户与连接池",
        license=None,
        skill_dir=Path("/catalog/private") / name,
        skill_file=Path("/catalog/private") / name / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=enabled,
        skill_id="skill-1",
        scope=SkillScope.PRIVATE,
        owner_user_id="u1",
        version_id="ver-1",
        skill_version="v1",
    )


def _hit(*, enabled: bool = True) -> SkillSearchHit:
    return SkillSearchHit(
        skill=_skill(enabled=enabled),
        body=BODY,
        score=0.9,
        scope="private",
        version_id="ver-1",
    )


def _outcome() -> SkillSearchOutcome:
    return SkillSearchOutcome(hits=(_hit(),), candidates=1, warnings=())


def test_search_skills_returns_only_metadata(tmp_path) -> None:
    _install(skills_config(tmp_path))
    service = FakeSearchService(_outcome())
    configure_skill_runtime(service)

    payload = _invoke(
        search_skills,
        {
            "task_query": "PostgreSQL 多租户 search_path 串库排查",
            "domain_keywords": ["search_path"],
            "runtime": _runtime(),
        },
    )

    assert payload["skills"] == [
        {
            "skill_id": "skill-1",
            "name": SKILL,
            "description": "PostgreSQL 多租户与连接池",
        }
    ]
    assert "body" not in json.dumps(payload)
    assert "path" not in payload["skills"][0]
    assert service.search_calls[0]["user_id"] == "u1"
    assert service.load_calls == []


def test_load_skills_returns_the_full_catalog_body(tmp_path) -> None:
    _install(skills_config(tmp_path))
    service = FakeSearchService(_outcome(), loaded=_hit())
    configure_skill_runtime(service)

    payload = _invoke(load_skills, {"skill_id": "skill-1", "runtime": _runtime()})

    assert payload["skill"]["skill_id"] == "skill-1"
    assert payload["skill"]["name"] == SKILL
    assert payload["skill"]["path"] == str(_skill().container_file)
    assert payload["skill"]["body"] == BODY
    assert service.load_calls == [{"user_id": "u1", "skill_id": "skill-1"}]
    assert service.search_calls == []


def test_load_skills_does_not_return_a_disabled_or_unknown_skill(tmp_path) -> None:
    _install(skills_config(tmp_path))
    service = FakeSearchService(_outcome(), loaded=None)
    configure_skill_runtime(service)

    payload = _invoke(load_skills, {"skill_id": "missing", "runtime": _runtime()})

    assert payload["skill"] is None
    assert payload["message"] == list(NOT_FOUND_MESSAGE)


def test_load_skills_is_available_when_search_is_disabled(tmp_path) -> None:
    config = _install(skills_config(tmp_path, search={"enabled": False}))

    names = {tool.name for tool in get_available_tools(app_config=config)}

    assert "load_skills" in names
    assert "search_skills" not in names


def test_filesystem_search_fails_closed_without_a_milvus_runtime(tmp_path) -> None:
    skills = skills_config(tmp_path)
    write_skill(tmp_path, "public", SKILL, description="多租户排查", body=BODY)
    _install(skills)
    reset_skill_runtime()

    searched = _invoke(
        search_skills,
        {
            "task_query": "pg_stat_activity",
            "domain_keywords": [],
            "runtime": _runtime(),
        },
    )
    assert searched["skills"] == []
    assert searched["message"] == list(UNAVAILABLE_MESSAGE)

    # load_skills is by id and still works without the search index.
    loaded = _invoke(load_skills, {"skill_id": SKILL, "runtime": _runtime()})
    assert loaded["skill"]["skill_id"] == SKILL
    assert loaded["skill"]["body"].startswith("# 排查")
