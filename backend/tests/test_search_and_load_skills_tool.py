"""search_and_load_skills：按任务意图检索并加载技能正文（M7 §3.5）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime

from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.config import AppConfig, set_app_config
from agentkit.config.skills_config import SkillSearchConfig, SkillsConfig
from agentkit.skills.domain import SkillScope
from agentkit.skills.loader import BODY_TRUNCATION_MARKER
from agentkit.skills.search import (
    SkillSearchHit,
    SkillSearchOutcome,
    configure_skill_runtime,
    reset_skill_runtime,
)
from agentkit.skills.tool_policy import ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES
from agentkit.skills.types import Skill, SkillCategory
from agentkit.tools.builtin.search_and_load_skills import (
    EMPTY_RESULT_MESSAGE,
    MAX_DOMAIN_KEYWORDS,
    TOOL_NAME,
    USAGE_NOTES,
    _clean_keywords,
    search_and_load_skills,
)
from agentkit.tools.registry import get_available_tools
from fakes.skill_tree import write_skill

BODY_MARKER = "先查 pg_stat_activity，再清连接池"
SKILL = "pg-multitenant"
OTHER = "csv-tools"


@pytest.fixture(autouse=True)
def _restore_app_config():
    """`set_app_config` 是进程级注入：用完必须还原，否则污染后面的用例。"""
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


@pytest.fixture(autouse=True)
def _reset_the_search_runtime():
    """技能检索服务也是进程级注入：每个用例从"没有运行时"开始，结束后再清一次。"""
    reset_skill_runtime()
    yield
    reset_skill_runtime()


class FakeSearchService:
    """只记录调用、只返回预设结果：工具不该自己构造真实服务。"""

    def __init__(self, outcome: SkillSearchOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


def catalog_skill(
    *,
    name: str = SKILL,
    scope: SkillScope = SkillScope.PRIVATE,
    owner: str | None = "u1",
    skill_id: str = "skill-1",
    version_id: str = "version-1",
    release_id: str | None = None,
) -> Skill:
    return Skill(
        name=name,
        description="PostgreSQL 多租户与连接池",
        license=None,
        skill_dir=Path("/catalog") / scope.value / name,
        skill_file=Path("/catalog") / scope.value / name / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=True,
        skill_id=skill_id,
        scope=scope,
        owner_user_id=owner,
        version_id=version_id,
        skill_version="v1",
        release_id=release_id,
    )


def catalog_outcome(
    *,
    body: str = f"# 排查\n{BODY_MARKER}。\n",
    scope: str = "private",
    release_id: str | None = None,
    warnings: tuple[str, ...] = (),
    candidates: int = 1,
) -> SkillSearchOutcome:
    return SkillSearchOutcome(
        hits=(
            SkillSearchHit(
                skill=catalog_skill(scope=SkillScope(scope), release_id=release_id),
                body=body,
                score=0.87,
                scope=scope,
                version_id="version-1",
                release_id=release_id,
            ),
        ),
        candidates=candidates,
        warnings=warnings,
    )


def _tree(tmp_path) -> SkillsConfig:
    write_skill(
        tmp_path,
        "public",
        SKILL,
        description="PostgreSQL 多租户与连接池",
        body=f"# 排查\n{BODY_MARKER}。\n" + "补充说明 " * 200,
    )
    write_skill(
        tmp_path,
        "public",
        OTHER,
        description="CSV 导出",
        body="# 导出\n另存为 UTF-8。\n",
    )
    return SkillsConfig(path=str(tmp_path / "skills"), builtin={"enabled": False})


def _install(skills: SkillsConfig) -> AppConfig:
    config = AppConfig(skills=skills)
    set_app_config(config)
    return config


def _runtime(user_id: str | None = "u1") -> ToolRuntime:
    context = {"user_id": user_id} if user_id else {}
    return ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id="call-1",
        store=None,
    )


def _call(**args) -> dict:
    payload = {
        "task_query": args.pop("task_query", "PostgreSQL 多租户 search_path 串库排查"),
        "domain_keywords": args.pop("domain_keywords", ["search_path"]),
        "runtime": args.pop("runtime", _runtime()),
        **args,
    }
    result = asyncio.run(
        search_and_load_skills.ainvoke(
            {"name": TOOL_NAME, "args": payload, "id": "call-1", "type": "tool_call"}
        )
    )
    # 传 dict 的调用形状回来的是 ToolMessage；正文在 .content 里
    return json.loads(result.content if hasattr(result, "content") else result)


def test_the_loaded_skill_comes_back_with_its_body(tmp_path):
    _install(_tree(tmp_path))

    payload = _call()

    assert [item["name"] for item in payload["skills"]] == [SKILL]
    skill = payload["skills"][0]
    assert skill["body"].startswith("# 排查")
    assert BODY_MARKER in skill["body"]
    # 路径是**容器**路径：模型还能用它 read_file 读被截断的后半段
    assert skill["path"] == f"/mnt/skills/public/{SKILL}/SKILL.md"
    assert skill["dir"] == f"/mnt/skills/public/{SKILL}"
    assert skill["category"] == "public"
    assert payload["retrieval"]["channels"] == ["keyword"]
    # frontmatter 不重复出现在正文里（正文已经是一段可读的 SOP）
    assert "description:" not in skill["body"]


def test_the_body_budget_is_applied_and_flagged(tmp_path):
    skills = _tree(tmp_path)
    skills.search = SkillSearchConfig(max_body_chars=200)
    _install(skills)

    skill = _call()["skills"][0]

    assert skill["body_truncated"] is True
    assert len(skill["body"]) <= 200
    assert "已截断" in skill["body"]


def test_a_blank_query_is_refused_with_guidance(tmp_path):
    _install(_tree(tmp_path))

    payload = _call(task_query="   ")

    assert payload["skills"] == []
    assert "task_query" in payload["message"][0]


def test_an_empty_skill_tree_tells_the_model_not_to_retry(tmp_path):
    (_install(SkillsConfig(path=str(tmp_path / "missing"), builtin={"enabled": False})))

    payload = _call()

    assert payload["skills"] == []
    assert "不要用本工具重复尝试" in payload["message"][1]


def test_a_disabled_search_tool_explains_instead_of_loading(tmp_path):
    skills = _tree(tmp_path)
    skills.search = SkillSearchConfig(enabled=False)
    _install(skills)

    payload = _call()

    assert payload["skills"] == []
    assert "已关闭" in payload["message"][0]


def test_skills_switched_off_by_the_user_are_not_loaded(tmp_path, monkeypatch):
    from agentkit.config.skills_prefs import write_user_skill_pref

    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
    _install(_tree(tmp_path))
    write_user_skill_pref("u1", SKILL, False)

    assert _call(runtime=_runtime("u1"))["skills"] == []
    assert [item["name"] for item in _call(runtime=_runtime("u2"))["skills"]] == [SKILL]


def test_the_tool_runs_inside_a_real_agent_loop(tmp_path):
    """`.ainvoke` 只是模拟：这里走真正的 `create_agent` → 工具节点，验证 runtime 注入没被
    `args_schema` 挡掉（模型只给业务字段，框架自己补 runtime）。"""
    from typing import Any

    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _ToolCallingFake(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ANN401 - 框架签名就是 Any
            return self

    _install(_tree(tmp_path))
    configure_skill_runtime(FakeSearchService(catalog_outcome()))
    model = _ToolCallingFake(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": TOOL_NAME,
                            "args": {
                                "task_query": "PostgreSQL 多租户 search_path 串库",
                                "domain_keywords": ["pg_stat_activity"],
                            },
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="加载完成"),
            ]
        )
    )
    agent = create_agent(model=model, tools=[search_and_load_skills])

    result = asyncio.run(
        agent.ainvoke({"messages": [{"role": "user", "content": "排查串库"}]})
    )

    tool_message = next(
        message for message in result["messages"] if type(message).__name__ == "ToolMessage"
    )
    payload = json.loads(tool_message.content)
    assert [item["name"] for item in payload["skills"]] == [SKILL]
    assert payload["skills"][0]["path"] == f"/mnt/skills/private/{SKILL}/SKILL.md"
    assert payload["skills"][0]["scope"] == "private"


def test_the_tool_is_registered_only_when_the_config_allows_it(tmp_path):
    skills = _tree(tmp_path)
    config = _install(skills)

    names = {tool.name for tool in get_available_tools(include_mcp=False, app_config=config)}
    assert TOOL_NAME in names
    assert TOOL_NAME in ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES

    skills.search = SkillSearchConfig(enabled=False)
    disabled = {
        tool.name
        for tool in get_available_tools(include_mcp=False, app_config=AppConfig(skills=skills))
    }
    assert TOOL_NAME not in disabled


def test_the_system_prompt_documents_the_blocks_but_not_the_routing(tmp_path):
    """提示词只说"你在看什么"（索引 vs 已加载），怎么说"什么时候去读"是工具 description 的事。

    两件事的边界很清楚：**格式说明**只有一份（跟着索引一起缓存），**路由策略**也只有一份
    （在 tool description 里）。重复任何一边都是两套契约。
    """
    prompt = apply_prompt_template(app_config=AppConfig(skills=_tree(tmp_path)))

    # 索引在，且带一句"这是索引、不是正文"；根目录与命名约定只写一次
    assert "<available_skills>" in prompt
    assert "不含正文" in prompt
    assert "技能根目录 `/mnt/skills`" in prompt
    assert "目录名就是技能名" in prompt
    assert "<active_skills>" in prompt  # 指向已加载技能的正文所在
    assert f"- {SKILL} [public]: PostgreSQL 多租户与连接池" in prompt
    assert BODY_MARKER not in prompt  # 正文本身不进静态提示词
    # 路由策略不写进系统提示词
    assert "search_and_load_skills" not in prompt
    assert "skill_routing_protocol" not in prompt


def test_dirty_keywords_are_cleaned_and_capped():
    cleaned = _clean_keywords(["pgvector", " pgvector ", "", 42, None, "HNSW", *["k" + str(i) for i in range(10)]])

    assert cleaned[0] == "pgvector"
    assert cleaned.count("pgvector") == 1
    assert all(isinstance(item, str) and item for item in cleaned)
    assert len(cleaned) == MAX_DOMAIN_KEYWORDS


def test_a_non_string_keyword_is_rejected_by_the_schema(tmp_path):
    """脏参数在进工具之前就被 args_schema 挡掉：模型收到一条校验错误，而不是一次静默失配。"""
    _install(_tree(tmp_path))

    with pytest.raises(Exception) as caught:
        _call(domain_keywords=["search_path", {"bad": True}])

    assert "domain_keywords" in str(caught.value)


def test_a_catalog_hit_returns_scope_version_and_release(tmp_path):
    _install(_tree(tmp_path))
    service = FakeSearchService(catalog_outcome())
    configure_skill_runtime(service)

    payload = _call()

    [skill] = payload["skills"]
    assert skill["name"] == SKILL
    assert skill["scope"] == "private"
    assert skill["version_id"] == "version-1"
    assert skill["release_id"] is None
    assert skill["path"] == f"/mnt/skills/private/{SKILL}/SKILL.md"
    assert skill["dir"] == f"/mnt/skills/private/{SKILL}"
    assert skill["body_truncated"] is False
    assert BODY_MARKER in skill["body"]
    assert skill["channels"] == ["embedding"]
    assert payload["retrieval"]["channels"] == ["embedding"]
    assert payload["retrieval"]["candidates"] == 1
    assert payload["retrieval"]["warnings"] == []
    assert payload["message"] == list(USAGE_NOTES)
    [called] = service.calls
    assert called["user_id"] == "u1"
    assert called["task_query"] == "PostgreSQL 多租户 search_path 串库排查"
    assert called["domain_keywords"] == ["search_path"]
    assert called["available_skills"] is None


def test_a_public_catalog_hit_carries_its_release_id(tmp_path):
    _install(_tree(tmp_path))
    outcome = SkillSearchOutcome(
        hits=(
            SkillSearchHit(
                skill=catalog_skill(
                    scope=SkillScope.PUBLIC, owner=None, release_id="release-1"
                ),
                body="# 公开正文\n",
                score=0.9,
                scope="public",
                version_id="version-1",
                release_id="release-1",
            ),
        ),
        candidates=1,
        warnings=(),
    )
    configure_skill_runtime(FakeSearchService(outcome))

    [skill] = _call()["skills"]

    assert skill["scope"] == "public"
    assert skill["release_id"] == "release-1"
    assert skill["path"] == f"/mnt/skills/public/{SKILL}/SKILL.md"


def test_the_catalog_runtime_replaces_the_filesystem_tree(tmp_path):
    _install(_tree(tmp_path))
    outcome = SkillSearchOutcome(
        hits=(
            SkillSearchHit(
                skill=catalog_skill(name="catalog-only"),
                body="# 目录正文\n",
                score=0.5,
                scope="private",
                version_id="version-1",
                release_id=None,
            ),
        ),
        candidates=1,
        warnings=(),
    )
    configure_skill_runtime(FakeSearchService(outcome))

    payload = _call()

    assert [item["name"] for item in payload["skills"]] == ["catalog-only"]
    assert BODY_MARKER not in payload["skills"][0]["body"]


def test_a_lexical_fallback_is_reported_to_the_model(tmp_path):
    _install(_tree(tmp_path))
    configure_skill_runtime(
        FakeSearchService(
            catalog_outcome(warnings=("vector_unavailable", "lexical_fallback"))
        )
    )

    payload = _call()

    assert payload["retrieval"]["channels"] == ["keyword"]
    assert payload["retrieval"]["warnings"] == ["vector_unavailable", "lexical_fallback"]


def test_a_truncated_body_is_flagged(tmp_path):
    _install(_tree(tmp_path))
    configure_skill_runtime(
        FakeSearchService(catalog_outcome(body="x" * 50 + BODY_TRUNCATION_MARKER))
    )

    [skill] = _call()["skills"]

    assert skill["body_truncated"] is True


def test_an_empty_catalog_result_tells_the_model_not_to_retry(tmp_path):
    _install(_tree(tmp_path))
    configure_skill_runtime(
        FakeSearchService(SkillSearchOutcome(hits=(), candidates=0, warnings=()))
    )

    payload = _call()

    assert payload["skills"] == []
    assert payload["retrieval"]["candidates"] == 0
    assert payload["message"] == list(EMPTY_RESULT_MESSAGE)


def test_the_user_preferences_are_forwarded_to_the_service(tmp_path, monkeypatch):
    from agentkit.config.skills_prefs import write_user_skill_pref

    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
    _install(_tree(tmp_path))
    write_user_skill_pref("u1", SKILL, False)
    service = FakeSearchService(catalog_outcome())
    configure_skill_runtime(service)

    _call()

    assert service.calls[0]["prefs"] == {SKILL: False}


def test_git_mode_without_a_runtime_fails_closed(tmp_path):
    skills = _tree(tmp_path)
    skills.source = "git"
    _install(skills)

    payload = _call()

    assert payload["skills"] == []
    assert "不可用" in payload["message"][0]


def test_an_unavailable_catalog_is_not_replaced_by_the_filesystem_tree(tmp_path):
    from agentkit.skills.search import SkillSearchUnavailable

    class BrokenSearch:
        async def search(self, **_kwargs):
            raise SkillSearchUnavailable("catalog is down")

    _install(_tree(tmp_path))
    configure_skill_runtime(BrokenSearch())

    payload = _call()

    assert payload["skills"] == []
    assert "不可用" in payload["message"][0]
    assert "不要用本工具重复尝试" in payload["message"][1]
