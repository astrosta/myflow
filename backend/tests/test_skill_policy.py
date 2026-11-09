"""M7 §3.7–§3.9：allowed-tools 是行为约束，规则要一条条钉住。"""

from __future__ import annotations

from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langgraph.prebuilt.tool_node import ToolCallRequest

from agentkit.agents.middlewares.skill_tool_policy_middleware import SkillToolPolicyMiddleware
from agentkit.runtime.secret_context import (
    SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
    SLASH_SKILL_SOURCE_CONTEXT_KEY,
    new_owner_token,
    read_secret_context,
    write_secret_context,
)
from agentkit.skills.tool_policy import (
    ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES,
    parse_decision,
)
from fakes.skill_tree import skills_config, write_skill

TOKEN = "test-owner-token"
SKILL_CONTEXT_KEY = "skill_context"


class StubTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _tools(*names: str) -> list[StubTool]:
    return [StubTool(name) for name in names]


def _container(name: str) -> str:
    return f"/mnt/skills/public/{name}/SKILL.md"


def _write(tmp_path, name: str, *, allowed: str | None = "read_file", category: str = "public"):
    extra = "" if allowed is None else f"allowed-tools: {allowed}"
    write_skill(tmp_path, category, name, extra_frontmatter=extra)
    return _container(name)


def _middleware(tmp_path, *, user_id: str | None = "u1", token: str = TOKEN):
    return SkillToolPolicyMiddleware(
        skills_config=skills_config(tmp_path), owner_token=token, user_id=user_id
    )


def _context(*, slash: str | None = None, token: str = TOKEN) -> dict:
    context: dict = {"user_id": "u1"}
    if slash is not None:
        write_secret_context(context, SLASH_SKILL_SOURCE_CONTEXT_KEY, slash, token=token)
    return context


def _state(*paths: str) -> dict:
    return {SKILL_CONTEXT_KEY: [{"name": "x", "path": path, "description": ""} for path in paths]}


def _model_request(tools, state, context) -> ModelRequest:
    return ModelRequest(
        model=object(),
        messages=[],
        state=dict(state),
        tools=list(tools),
        runtime=SimpleNamespace(context=context),
    )


def _run(middleware, tools, state, context) -> list[str]:
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    middleware.wrap_model_call(_model_request(tools, state, context), handler)
    return [tool.name for tool in seen["request"].tools]


ALL_TOOLS = _tools(
    "read_file",
    "write_file",
    "bash",
    "ls",
    "task",
    "load_skill",
    "search_skills",
    "tool_search",
    "tool_execute",
    "mcp_promoted",
)


def test_passive_enablement_does_not_tighten_the_baseline(tmp_path):
    """没有任何真正激活的技能 → 工具集原样（规则一）。"""
    visible = _run(_middleware(tmp_path), ALL_TOOLS, {}, _context())

    assert visible == [tool.name for tool in ALL_TOOLS]


def test_slash_dominates_and_reading_another_skill_cannot_widen_it(tmp_path):
    slash = _write(tmp_path, "restricted", allowed="[read_file]")
    wide = _write(tmp_path, "wide", allowed="[bash, write_file]")
    middleware = _middleware(tmp_path)

    visible = _run(middleware, ALL_TOOLS, _state(wide), _context(slash=slash))

    assert visible == ["read_file", "load_skill", "tool_search", "tool_execute"]


def test_autonomous_skills_union_and_undeclared_ones_contribute_nothing(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    shell = _write(tmp_path, "shell", allowed="[bash]")
    silent = _write(tmp_path, "silent", allowed=None)  # 未声明：贡献空集，不是"放行全部"
    middleware = _middleware(tmp_path)

    visible = _run(middleware, ALL_TOOLS, _state(reader, shell, silent), _context())

    assert set(visible) == {
        "read_file",
        "bash",
        "load_skill",
        "tool_search",
        "tool_execute",
    }


def test_an_empty_declaration_does_not_dissolve_another_limit(tmp_path):
    empty = _write(tmp_path, "empty", allowed="[]")
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    middleware = _middleware(tmp_path)

    visible = _run(middleware, ALL_TOOLS, _state(empty, reader), _context())

    assert set(visible) == {"read_file", "load_skill", "tool_search", "tool_execute"}


def test_an_empty_declaration_alone_leaves_only_framework_tools(tmp_path):
    empty = _write(tmp_path, "empty", allowed="[]")
    middleware = _middleware(tmp_path)

    visible = _run(middleware, ALL_TOOLS, _state(empty), _context())

    assert set(visible) == set(ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES)


def test_the_framework_set_is_exactly_the_discovery_and_skill_tools():
    assert ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES == frozenset(
        {"read_file", "load_skill", "tool_search", "tool_execute"}
    )


def test_search_skills_is_not_a_framework_tool(tmp_path):
    """search_skills 是 skill-finder 的元数据工具：激活受限技能后不能再直接检索。"""
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    middleware = _middleware(tmp_path)

    visible = _run(middleware, ALL_TOOLS, _state(reader), _context())

    assert "search_skills" not in visible
    assert "load_skill" in visible


def test_task_is_not_exempt_but_framework_tools_always_are(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    middleware = _middleware(tmp_path)

    visible = _run(middleware, ALL_TOOLS, _state(reader), _context())

    assert "task" not in visible  # 否则受限技能可以靠委派绕过策略
    assert set(ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES) <= set(visible)


def test_a_promoted_business_tool_still_needs_a_declaration(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    middleware = _middleware(tmp_path)

    visible = _run(middleware, ALL_TOOLS, _state(reader), _context())

    assert "mcp_promoted" not in visible


def test_the_decision_is_written_with_the_owner_token(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    context = _context()
    middleware = _middleware(tmp_path)

    _run(middleware, ALL_TOOLS, _state(reader), context)

    payload = read_secret_context(context, SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY, token=TOKEN)
    decision = parse_decision(payload)
    assert decision is not None
    assert decision.mode == "union"
    assert decision.activated_paths == (reader,)
    assert read_secret_context(
        context, SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY, token="forged"
    ) is None


def _tool_call(name: str, context: dict, state: dict) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "id": "call-1", "args": {}},
        tool=None,
        state=state,
        runtime=SimpleNamespace(context=context),
    )


def test_a_blocked_tool_call_short_circuits_with_a_recoverable_error(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    middleware = _middleware(tmp_path)
    called: list[str] = []

    def handler(request):
        called.append(request.tool_call["name"])
        return "ran"

    result = middleware.wrap_tool_call(
        _tool_call("bash", _context(), _state(reader)), handler
    )

    assert called == []  # handler 没被调用
    assert result.status == "error"
    meta = result.additional_kwargs["myflow_tool_meta"]
    assert meta["recoverable_by_model"] is True
    assert meta["recommended_next_action"] == "try_alternative"
    assert "bash" in result.content


def test_a_declared_tool_call_reaches_the_handler(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    middleware = _middleware(tmp_path)

    result = middleware.wrap_tool_call(
        _tool_call("read_file", _context(), _state(reader)), lambda request: "ran"
    )

    assert result == "ran"


def test_a_forged_decision_is_ignored(tmp_path):
    """没有 token 的决策（别人写的 / 伪造的）一律作废，回退现场解析。"""
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    context = _context()
    write_secret_context(
        context,
        SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
        {"version": 1, "mode": "passthrough", "unlimited": True, "allowed": [], "activated_paths": [], "signature": "deadbeef"},
        token=new_owner_token(),
    )
    middleware = _middleware(tmp_path)
    called: list[str] = []

    result = middleware.wrap_tool_call(
        _tool_call("bash", context, _state(reader)),
        lambda request: called.append("bash") or "ran",
    )

    assert called == []
    assert result.status == "error"
