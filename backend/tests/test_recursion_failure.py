"""技能检索空结果要劝退模型：它反复换措辞重问会撞 recursion_limit。"""

from __future__ import annotations

import json

import pytest
from langchain.tools import ToolRuntime
from types import SimpleNamespace

from agentkit.runtime.runs.worker import _public_error


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        state={},
        context={"thread_id": "t1"},
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id="call-1",
        store=None,
    )


def test_an_empty_skill_search_tells_the_model_to_stop(monkeypatch) -> None:
    import asyncio

    from agentkit.tools.builtin import search_skills as module

    class _EmptyRegistry:
        def __init__(self, **_kwargs) -> None:
            pass

        def visible(self, *, include_builtin: bool = True):
            return []

    from agentkit.config.skills_config import SkillSearchConfig

    monkeypatch.setattr(
        module,
        "safe_app_config",
        lambda: SimpleNamespace(skills=SimpleNamespace(search=SkillSearchConfig())),
    )
    monkeypatch.setattr(module, "SkillRegistry", _EmptyRegistry)

    result = asyncio.run(
        module.search_skills.ainvoke(
            {
                "name": "search_skills",
                "args": {
                    "task_query": "PostgreSQL 多租户 search_path 串库",
                    "domain_keywords": [],
                    "runtime": _runtime(),
                },
                "id": "call-1",
                "type": "tool_call",
            }
        )
    )
    payload = json.loads(result.content if hasattr(result, "content") else result)

    assert payload["skills"] == []
    # 这句是护栏：模型看到"别重复尝试"就不会换措辞一遍遍重试
    assert any("不要用本工具重复尝试" in line for line in payload["message"])


def test_recursion_error_is_translated_into_something_actionable() -> None:
    from langgraph.errors import GraphRecursionError

    exc = GraphRecursionError(
        "Recursion limit of 100 reached without hitting a stop condition. "
        "You can increase the limit by setting the `recursion_limit` config key."
    )

    message = _public_error(exc)

    assert message.startswith("GraphRecursionError: ")
    assert "100 步" in message
    assert "没有收敛" in message
    # 原始英文提示带文档链接，对用户没意义
    assert "docs.langchain.com" not in message
