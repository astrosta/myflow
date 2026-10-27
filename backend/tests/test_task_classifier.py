"""Task classifier middleware contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from agentkit.agents.middlewares.task_classifier import (
    TaskBoundaryResult,
    TaskClassifierMiddleware,
    append_completed_run,
)
from agentkit.agents.middlewares.assembly import build_middlewares
from agentkit.agents.middlewares.memory_middleware import MemoryMiddleware
from agentkit.config import AppConfig
from agentkit.config.memory_config import MemoryConfig


class FakeClassifier:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def classify(self, value):
        self.calls.append(value)
        if self.error:
            raise self.error
        return self.result


def _runtime(**context):
    return SimpleNamespace(context={"user_id": "u1", "thread_id": "t1", **context})


def _state(task_context=None, text="帮我看下部署"):
    return {
        "messages": [HumanMessage(id="u1", content=text)],
        "task_context": task_context,
    }


def _task_context(**overrides):
    value = {
        "task_id": "task-1",
        "task_goal": "原目标",
        "task_goal_source": "classifier",
        "status": "open",
        "started_at": "2026-10-26T00:00:00Z",
        "completed_runs": [],
    }
    value.update(overrides)
    return value


def test_empty_goal_forces_new_task_and_uses_model_goal():
    classifier = FakeClassifier(TaskBoundaryResult(decision="NEW_TASK", task_goal="修复部署脚本"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)

    update = asyncio.run(middleware.abefore_agent(_state(), _runtime()))

    context = update["task_context"]
    assert context["task_id"]
    assert context["task_id"] != "task-1"
    assert context["task_goal"] == "修复部署脚本"
    assert context["task_goal_source"] == "classifier"
    assert context["boundary_decision"] == "NEW_TASK"
    assert context["boundary_pending"] is True
    assert classifier.calls


def test_continue_does_not_rewrite_goal():
    classifier = FakeClassifier(TaskBoundaryResult(decision="CONTINUE", task_goal="另一个目标"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)

    update = asyncio.run(middleware.abefore_agent(_state(_task_context()), _runtime()))

    context = update["task_context"]
    assert context["task_id"] == "task-1"
    assert context["task_goal"] == "原目标"
    assert context["task_goal_source"] == "classifier"
    assert context["boundary_pending"] is False
    assert context["boundary_uncertain"] is False


def test_classifier_error_with_non_empty_goal_is_uncertain():
    classifier = FakeClassifier(error=TimeoutError())
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)

    update = asyncio.run(middleware.abefore_agent(_state(_task_context()), _runtime()))

    context = update["task_context"]
    assert context["task_id"] == "task-1"
    assert context["task_goal"] == "原目标"
    assert context["boundary_decision"] == "UNCERTAIN"
    assert context["boundary_uncertain"] is True
    assert context["boundary_pending"] is False


def test_empty_goal_with_classifier_error_uses_fallback():
    classifier = FakeClassifier(error=TimeoutError())
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)

    update = asyncio.run(middleware.abefore_agent(_state(text="帮我修复部署"), _runtime()))

    context = update["task_context"]
    assert context["task_id"]
    assert context["task_goal"] == "帮我修复部署"
    assert context["task_goal_source"] == "fallback"
    assert context["boundary_decision"] == "NEW_TASK"
    assert context["boundary_pending"] is True


def test_classifier_input_uses_only_completed_run_pairs():
    classifier = FakeClassifier(TaskBoundaryResult(decision="CONTINUE"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)
    state = _state(
        _task_context(
            completed_runs=[
                {"run_id": "r1", "human_message": "旧 run", "final_ai_message": "旧 run 最终回复"}
            ]
        ),
        text="新 run",
    )

    asyncio.run(middleware.abefore_agent(state, _runtime()))

    assert classifier.calls[0]["current_human_message"] == "新 run"
    assert classifier.calls[0]["completed_runs"] == [
        {"run_id": "r1", "human_message": "旧 run", "final_ai_message": "旧 run 最终回复"}
    ]


def test_append_completed_run_keeps_only_pairs_and_bounds():
    context = {"task_id": "task-1", "completed_runs": []}
    for index in range(3):
        context = append_completed_run(
            context,
            {
                "run_id": f"r{index}",
                "human_message": f"h{index}",
                "final_ai_message": f"a{index}",
            },
            max_history_runs=2,
        )

    assert [pair["run_id"] for pair in context["completed_runs"]] == ["r1", "r2"]
    assert all(
        set(pair) == {"run_id", "human_message", "final_ai_message"}
        for pair in context["completed_runs"]
    )


def test_subagent_does_not_run_classifier():
    classifier = FakeClassifier(TaskBoundaryResult(decision="NEW_TASK", task_goal="不该发生"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)

    assert (
        asyncio.run(
            middleware.abefore_agent(
                _state(), _runtime(subagent_name="researcher")
            )
        )
        is None
    )
    assert classifier.calls == []


def test_memory_disabled_does_not_run_classifier():
    classifier = FakeClassifier(TaskBoundaryResult(decision="NEW_TASK", task_goal="不该发生"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=False), classifier=classifier)

    assert asyncio.run(middleware.abefore_agent(_state(), _runtime())) is None
    assert classifier.calls == []


def test_assembly_places_classifier_before_memory_when_enabled():
    app_config = AppConfig.model_validate(
        {
            "models": [{"name": "flash", "use": "langchain_openai:ChatOpenAI"}],
            "memory": {"enabled": True},
        }
    )
    chain = build_middlewares({}, "flash", app_config=app_config, user_id="u1")
    names = [type(item).__name__ for item in chain]
    assert "TaskClassifierMiddleware" in names
    assert names.index("TaskClassifierMiddleware") < names.index("MemoryMiddleware")


def test_assembly_omits_classifier_when_memory_disabled():
    app_config = AppConfig.model_validate(
        {
            "models": [{"name": "flash", "use": "langchain_openai:ChatOpenAI"}],
            "memory": {"enabled": False},
        }
    )
    chain = build_middlewares({}, "flash", app_config=app_config, user_id="u1")
    assert all(type(item).__name__ != "TaskClassifierMiddleware" for item in chain)
    assert any(isinstance(item, MemoryMiddleware) for item in chain)
