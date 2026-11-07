"""Task 9: the cf__ namespace still obeys the active skill's allowed-tools.

The two ContextForge builtins are framework tools, but that only means the model
can *discover* them.  A tool_search card and a tool_execute argument are inner
tool names: they must be checked against the decision SkillToolPolicyMiddleware
wrote for the current model call.  A missing decision fails closed rather than
falling back to "no skill is active, therefore everything is allowed".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from agentkit.agents.middlewares.skill_tool_policy_middleware import SkillToolPolicyMiddleware
from fakes.skill_tree import skills_config, write_skill

TOKEN = "contextforge-policy-token"
SKILL_CONTEXT_KEY = "skill_context"


class StubTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _container(name: str) -> str:
    return f"/mnt/skills/public/{name}/SKILL.md"


def _write(tmp_path, name: str, *, allowed: str):
    write_skill(tmp_path, "public", name, extra_frontmatter=f"allowed-tools: {allowed}")
    return _container(name)


def _middleware(tmp_path):
    return SkillToolPolicyMiddleware(
        skills_config=skills_config(tmp_path), owner_token=TOKEN, user_id="u1"
    )


def _context() -> dict:
    return {"user_id": "u1"}


def _state(*paths: str) -> dict:
    return {SKILL_CONTEXT_KEY: [{"name": "x", "path": path, "description": ""} for path in paths]}


def _prime(middleware, context: dict, state: dict) -> None:
    """Run one model-call hook so the policy stores its decision in context."""
    tools = [
        StubTool(name)
        for name in (
            "read_file",
            "bash",
            "tool_search",
            "tool_execute",
            "load_skills",
        )
    ]

    def handler(request: ModelRequest):
        return request

    middleware.wrap_model_call(
        ModelRequest(
            model=object(),
            messages=[],
            state=dict(state),
            tools=tools,
            runtime=SimpleNamespace(context=context),
        ),
        handler,
    )


def _tool_call(name: str, context: dict, state: dict, args: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "id": "call-1", "args": args or {}},
        tool=None,
        state=state,
        runtime=SimpleNamespace(context=context),
    )


def _card(call_name: str) -> dict:
    return {
        "tool_id": f"id-{call_name}",
        "tool_name": call_name.removeprefix("cf__"),
        "call_name": call_name,
        "description": "author data",
        "tags": [],
        "parameters": [],
        "input_schema": {"type": "object"},
        "schema_truncated": False,
        "score": 0.5,
    }


def _search_payload(*call_names: str) -> dict:
    return {
        "tools": [_card(call_name) for call_name in call_names],
        "retrieval": {"index": "milvus", "channels": ["embedding"], "candidates": 2},
        "message": "Call tool_execute(tool_name, arguments) to run one of these tools.",
    }


def _search_command(*call_names: str) -> Command:
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(_search_payload(*call_names)),
                    tool_call_id="call-1",
                    name="tool_search",
                )
            ],
            "retrieved_tool_names": list(call_names),
        }
    )


def test_a_restricted_skill_cannot_execute_a_disallowed_cf_tool(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    state = _state(reader)
    context = _context()
    middleware = _middleware(tmp_path)
    _prime(middleware, context, state)
    called: list[str] = []

    result = middleware.wrap_tool_call(
        _tool_call("tool_execute", context, state, {"tool_name": "cf__read_file"}),
        lambda request: called.append(request.tool_call["name"]) or "ran",
    )

    assert called == []
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "cf__read_file" in result.content


def test_a_declared_cf_tool_reaches_tool_execute(tmp_path):
    searcher = _write(tmp_path, "searcher", allowed="[cf__github_search]")
    state = _state(searcher)
    context = _context()
    middleware = _middleware(tmp_path)
    _prime(middleware, context, state)
    called: list[str] = []

    result = middleware.wrap_tool_call(
        _tool_call("tool_execute", context, state, {"tool_name": "cf__github_search"}),
        lambda request: called.append("ran") or "ran",
    )

    assert result == "ran"
    assert called == ["ran"]


def test_tool_search_filters_command_cards_and_retrieved_names(tmp_path):
    searcher = _write(tmp_path, "searcher", allowed="[cf__github_search]")
    state = _state(searcher)
    context = _context()
    middleware = _middleware(tmp_path)
    _prime(middleware, context, state)

    result = middleware.wrap_tool_call(
        _tool_call("tool_search", context, state, {"task_query": "search code"}),
        lambda request: _search_command("cf__github_search", "cf__read_file"),
    )

    assert isinstance(result, Command)
    message = result.update["messages"][0]
    payload = json.loads(message.content)
    assert [tool["call_name"] for tool in payload["tools"]] == ["cf__github_search"]
    assert result.update["retrieved_tool_names"] == ["cf__github_search"]


def test_tool_search_filters_a_plain_tool_message_summary(tmp_path):
    searcher = _write(tmp_path, "searcher", allowed="[cf__github_search]")
    state = _state(searcher)
    context = _context()
    middleware = _middleware(tmp_path)
    _prime(middleware, context, state)

    result = middleware.wrap_tool_call(
        _tool_call("tool_search", context, state, {"task_query": "search code"}),
        lambda request: ToolMessage(
            content=json.dumps(_search_payload("cf__github_search", "cf__read_file")),
            tool_call_id="call-1",
            name="tool_search",
        ),
    )

    assert isinstance(result, ToolMessage)
    payload = json.loads(result.content)
    assert [tool["call_name"] for tool in payload["tools"]] == ["cf__github_search"]


def test_a_missing_decision_fails_closed_for_tool_execute(tmp_path):
    context = _context()
    middleware = _middleware(tmp_path)
    called: list[str] = []

    result = middleware.wrap_tool_call(
        _tool_call("tool_execute", context, {}, {"tool_name": "cf__read_file"}),
        lambda request: called.append("ran") or "ran",
    )

    assert called == []
    assert result.status == "error"
    assert "cf__read_file" in result.content


def test_a_missing_decision_fails_closed_for_tool_search(tmp_path):
    context = _context()
    middleware = _middleware(tmp_path)

    result = middleware.wrap_tool_call(
        _tool_call("tool_search", context, {}, {"task_query": "search code"}),
        lambda request: _search_command("cf__github_search"),
    )

    assert isinstance(result, Command)
    payload = json.loads(result.update["messages"][0].content)
    assert payload["tools"] == []
    assert result.update["retrieved_tool_names"] == []


def test_ordinary_tool_calls_are_not_inner_checked(tmp_path):
    reader = _write(tmp_path, "reader", allowed="[read_file]")
    state = _state(reader)
    context = _context()
    middleware = _middleware(tmp_path)
    _prime(middleware, context, state)

    result = middleware.wrap_tool_call(
        _tool_call("read_file", context, state, {"tool_name": "cf__read_file"}),
        lambda request: "ran",
    )

    assert result == "ran"
