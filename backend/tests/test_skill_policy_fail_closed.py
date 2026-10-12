"""M7 §3.9 的失败语义：加载不出来就只剩框架工具，伪造的斜杠源路径当无。"""

from __future__ import annotations

from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest

from agentkit.agents.middlewares.skill_tool_policy_middleware import SkillToolPolicyMiddleware
from agentkit.runtime.secret_context import (
    SLASH_SKILL_SOURCE_CONTEXT_KEY,
    new_owner_token,
    write_secret_context,
)
from agentkit.skills.tool_policy import ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES
from fakes.skill_tree import skills_config, write_skill

TOKEN = "owner-token"


class StubTool:
    def __init__(self, name: str) -> None:
        self.name = name


ALL_TOOLS = [
    StubTool(name)
    for name in ("read_file", "write_file", "bash", "task", "describe_skill", "tool_search")
]


def _visible(middleware, state, context) -> list[str]:
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    middleware.wrap_model_call(
        ModelRequest(
            model=object(),
            messages=[],
            state=dict(state),
            tools=list(ALL_TOOLS),
            runtime=SimpleNamespace(context=context),
        ),
        handler,
    )
    return [tool.name for tool in seen["request"].tools]


def test_a_registry_failure_fails_closed_to_framework_tools(tmp_path, monkeypatch):
    path = write_skill(tmp_path, "public", "reader", extra_frontmatter="allowed-tools: [read_file]")
    context: dict = {"user_id": "u1"}
    write_secret_context(context, SLASH_SKILL_SOURCE_CONTEXT_KEY, "/mnt/skills/public/reader/SKILL.md", token=TOKEN)
    middleware = SkillToolPolicyMiddleware(skills_config=skills_config(tmp_path), owner_token=TOKEN)

    def boom(*args, **kwargs):
        raise RuntimeError("skill tree is unreadable")

    monkeypatch.setattr("agentkit.skills.storage.load_skills", boom)

    visible = _visible(middleware, {}, context)

    assert set(visible) == set(ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES)


def test_an_activation_set_that_resolves_to_nothing_fails_closed(tmp_path):
    write_skill(tmp_path, "public", "reader", extra_frontmatter="allowed-tools: [read_file]")
    middleware = SkillToolPolicyMiddleware(skills_config=skills_config(tmp_path), owner_token=TOKEN)
    stale = {  # 技能被删掉了，路径还留在 state 里
        "skill_context": [{"name": "ghost", "path": "/mnt/skills/public/ghost/SKILL.md", "description": ""}]
    }

    visible = _visible(middleware, stale, {"user_id": "u1"})

    assert set(visible) == set(ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES)


def test_a_slash_path_written_without_the_token_is_treated_as_absent(tmp_path):
    """runtime.context 会被调用方合并：没有 token 的伪造路径必须当无。"""
    write_skill(tmp_path, "public", "reader", extra_frontmatter="allowed-tools: [read_file]")
    context: dict = {"user_id": "u1"}
    write_secret_context(
        context,
        SLASH_SKILL_SOURCE_CONTEXT_KEY,
        "/mnt/skills/public/reader/SKILL.md",
        token=new_owner_token(),
    )
    middleware = SkillToolPolicyMiddleware(skills_config=skills_config(tmp_path), owner_token=TOKEN)

    visible = _visible(middleware, {}, context)

    assert visible == [tool.name for tool in ALL_TOOLS]  # 没激活任何技能 → 基线不变
