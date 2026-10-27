"""Constrained task-boundary classifier middleware.

The classifier answers one question: does the new user message continue the
current task, start a new one, or is the boundary uncertain?  It may also
propose a goal for a new task.  It never writes memory, never creates a task
id, and never closes a session; those are host-side responsibilities.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field

from agentkit.agents.memory.canonical.operator_model import resolve_operator_model
from agentkit.agents.memory.types import looks_like_secret
from agentkit.agents.middlewares.message_utils import is_real_user_message
from agentkit.agents.thread_state import RunPair, TaskContext
from agentkit.config.memory_config import MemoryConfig
from agentkit.models import create_chat_model
from agentkit.privacy import PIIRedactor
from agentkit.utils.custom_events import emit_custom_event
from agentkit.utils.messages import message_content_to_text

logger = logging.getLogger(__name__)

_MAX_GOAL_CHARS = 500
_FALLBACK_GOAL = "未命名任务"

_SOCIAL_TOKENS = tuple(
    sorted(
        {
            "你好",
            "您好",
            "大家好",
            "嗨",
            "哈喽",
            "哈啰",
            "早上好",
            "早安",
            "中午好",
            "下午好",
            "晚上好",
            "晚安",
            "在吗",
            "在么",
            "在不在",
            "好久不见",
            "谢谢",
            "谢谢你",
            "多谢",
            "感谢",
            "辛苦了",
            "好的",
            "好",
            "嗯",
            "嗯嗯",
            "哦",
            "收到",
            "明白了",
            "知道了",
            "可以",
            "行",
            "没问题",
            "再见",
            "拜拜",
            "回头见",
            "哈哈",
            "哈哈哈",
            "嘿嘿",
            "呵呵",
            "最近怎么样",
            "最近如何",
            "过得怎么样",
            "hi",
            "hello",
            "hey",
            "good morning",
            "good afternoon",
            "good evening",
            "good night",
            "thanks",
            "thank you",
            "thx",
            "ok",
            "okay",
            "got it",
            "sure",
            "bye",
            "goodbye",
            "see you",
            "how are you",
            "how are you doing",
            "how's it going",
            "what's up",
            "nice to meet you",
            "long time no see",
        },
        key=len,
        reverse=True,
    )
)
_SEPARATOR = r"[\s,，.。!！?？~～…、;；:：\-—–_=+*#@/\\|'\"“”‘’()（）\[\]{}<>《》]*"


def _tokens_pattern(tokens: tuple[str, ...]) -> re.Pattern[str]:
    body = "|".join(re.escape(token) for token in tokens)
    return re.compile(rf"^(?:{body})(?:{_SEPARATOR}(?:{body}))*$", re.IGNORECASE)


_SOCIAL_RE = _tokens_pattern(_SOCIAL_TOKENS)
_CONTINUATION_TOKENS = tuple(
    sorted(
        {
            "继续",
            "继续吧",
            "继续做",
            "继续干",
            "继续执行",
            "继续任务",
            "继续下一步",
            "接着",
            "接着做",
            "接着来",
            "接着干",
            "接着下一步",
            "接着往下",
            "接着往下做",
            "下一步",
            "然后呢",
            "然后",
            "往下",
            "往下做",
            "往下走",
            "go on",
            "continue",
            "keep going",
            "proceed",
            "carry on",
            "go ahead",
            "next",
            "next step",
            "resume",
            "keep it up",
        },
        key=len,
        reverse=True,
    )
)
_CONTINUATION_RE = _tokens_pattern(_CONTINUATION_TOKENS)
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]+")

_SYSTEM_PROMPT = """You classify whether a new user message continues the current task or starts a new one.
Return only the structured result. Use the current task goal and the completed run pairs as context.
Do not invent tool output, system messages, or hidden messages.

Rules:
- If task_goal is empty, the decision is NEW_TASK and you must propose a short task_goal.
- CONTINUE means the new message is a follow-up to the current task.
- NEW_TASK means the new message addresses a materially different goal.
- UNCERTAIN means the context is insufficient or genuinely ambiguous.
- A task_goal must be a short, standalone goal, at most 500 characters, without secrets or credentials.
- Do not return task_id or any storage identifiers.
"""


class TaskBoundaryResult(BaseModel):
    """The only shape the classifier model may return."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["CONTINUE", "NEW_TASK", "UNCERTAIN"]
    task_goal: str | None = Field(default=None, max_length=_MAX_GOAL_CHARS)


class TaskClassifierInput(TypedDict):
    current_human_message: str
    task_goal: str
    completed_runs: list[RunPair]


class TaskClassifier:
    """Small model adapter; kept separate so the middleware stays testable."""

    def __init__(self, model: Any, *, timeout_seconds: float = 8.0) -> None:
        self._model = model
        self._timeout = float(timeout_seconds)

    async def classify(self, value: TaskClassifierInput) -> TaskBoundaryResult:
        payload = {
            "current_human_message": value["current_human_message"],
            "task_goal": value["task_goal"],
            "completed_runs": [
                {
                    "run_id": pair["run_id"],
                    "human_message": pair["human_message"],
                    "final_ai_message": pair["final_ai_message"],
                }
                for pair in value["completed_runs"]
            ],
        }
        structured = self._model.with_structured_output(TaskBoundaryResult)
        result = await asyncio.wait_for(
            structured.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ]
            ),
            timeout=self._timeout,
        )
        if isinstance(result, TaskBoundaryResult):
            return result
        return TaskBoundaryResult.model_validate(result)


class TaskClassifierMiddleware(AgentMiddleware):
    """Run the task classifier once per lead turn and update task_context."""

    def __init__(
        self,
        *,
        config: MemoryConfig,
        app_config: Any = None,
        classifier: Any = None,
        user_id: str | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._app_config = app_config
        self._classifier = classifier
        self._default_classifier: TaskClassifier | None = None
        self._user_id = user_id

    async def abefore_agent(self, state: Mapping, runtime: Runtime) -> dict[str, Any] | None:
        if not self._config.enabled:
            return None
        if not self._config.task_classifier.enabled:
            return None

        context = _context(runtime)
        if _text(context.get("subagent_name")) is not None:
            return None
        user_id = _text(context.get("user_id")) or self._user_id
        if not user_id:
            return None

        messages = list(state.get("messages") or [])
        current = _latest_real_user(messages)
        if current is None:
            return None
        current_id = _message_id(current)
        current_text = _redact(message_content_to_text(getattr(current, "content", "")))
        if not current_text:
            current_text = _FALLBACK_GOAL

        existing = _task_context(state.get("task_context"))
        current_goal = _text(existing.get("task_goal")) or ""
        completed = _bounded_runs(
            existing.get("completed_runs"),
            self._config.task_classifier.max_history_runs,
        )

        if current_id and existing.get("last_classified_message_id") == current_id:
            return None

        if is_social_message(current_text) or is_continuation_prompt(current_text):
            return _reuse_existing_task(
                existing,
                current_id,
                policy=self._config.task_classifier.uncertain_policy,
            )

        current_text, current_goal, completed = _fit_input(
            current_text,
            current_goal,
            completed,
            self._config.task_classifier.max_input_tokens,
        )
        model_result: TaskBoundaryResult | None = None
        model_failed = False
        if self._config.task_classifier.enabled:
            try:
                model_result = await self._run_classifier(
                    TaskClassifierInput(
                        current_human_message=current_text,
                        task_goal=current_goal,
                        completed_runs=completed,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - classifier failure must degrade
                model_failed = True
                logger.warning("task classifier failed (%s)", type(exc).__name__)

        decision = _decision(current_goal, model_result, model_failed)
        goal, source = _goal_for_decision(
            decision,
            current_goal,
            model_result.task_goal if model_result is not None else None,
            fallback=current_text,
        )

        now = datetime.now(UTC).isoformat()
        task_context: TaskContext = dict(existing)
        task_context["last_classified_message_id"] = current_id or ""
        task_context["boundary_decision"] = decision
        task_context["boundary_uncertain"] = decision == "UNCERTAIN"

        if decision == "NEW_TASK":
            previous = _text(existing.get("task_id"))
            task_context.update(
                {
                    "task_id": str(uuid.uuid4()),
                    "task_goal": goal,
                    "task_goal_source": source,
                    "status": "open",
                    "started_at": now,
                    "completed_runs": [],
                    "boundary_pending": True,
                    "previous_task_id": previous,
                }
            )
        else:
            if not _text(existing.get("task_id")):
                task_context.update(
                    {
                        "task_id": str(uuid.uuid4()),
                        "started_at": now,
                        "status": "open",
                        "completed_runs": completed,
                    }
                )
            if not current_goal:
                task_context.update(
                    {
                        "task_goal": goal,
                        "task_goal_source": source,
                    }
                )
            task_context["boundary_pending"] = False

        _emit_task_event(
            decision=decision,
            task_goal_source=source,
            previous_task_id=_text(existing.get("task_id")),
            task_id=_text(task_context.get("task_id")),
            policy=self._config.task_classifier.uncertain_policy,
        )
        return {"task_context": task_context}

    async def _run_classifier(self, payload: TaskClassifierInput) -> TaskBoundaryResult:
        if self._classifier is not None:
            return await self._classifier.classify(payload)
        if self._default_classifier is None:
            self._default_classifier = TaskClassifier(
                _build_model(self._config, self._app_config),
                timeout_seconds=self._config.task_classifier.timeout_seconds,
            )
        return await self._default_classifier.classify(payload)


def append_completed_run(
    context: TaskContext,
    pair: RunPair,
    *,
    max_history_runs: int,
) -> TaskContext:
    """Return a copy with one bounded run pair appended.

    Only the three classifier fields are retained; callers cannot smuggle
    ToolMessage content or intermediate AI messages into task_context.
    """
    value = dict(context or {})
    runs = list(value.get("completed_runs") or [])
    runs.append(
        {
            "run_id": str(pair.get("run_id") or ""),
            "human_message": str(pair.get("human_message") or ""),
            "final_ai_message": str(pair.get("final_ai_message") or ""),
        }
    )
    if max_history_runs >= 0:
        runs = runs[-max_history_runs:] if max_history_runs else []
    value["completed_runs"] = runs
    return value  # type: ignore[return-value]


def clear_completed_runs(context: TaskContext) -> TaskContext:
    """Reset run history for a newly started task."""
    value = dict(context or {})
    value["completed_runs"] = []
    return value  # type: ignore[return-value]


def _decision(
    current_goal: str,
    result: TaskBoundaryResult | None,
    failed: bool,
) -> Literal["CONTINUE", "NEW_TASK", "UNCERTAIN"]:
    if not current_goal:
        return "NEW_TASK"
    if failed or result is None:
        return "UNCERTAIN"
    if result.decision == "NEW_TASK" and not _valid_goal(result.task_goal):
        return "UNCERTAIN"
    return result.decision


def _goal_for_decision(
    decision: str,
    current_goal: str,
    model_goal: str | None,
    *,
    fallback: str,
) -> tuple[str, Literal["classifier", "fallback"]]:
    if decision != "NEW_TASK":
        return current_goal, "classifier"
    normalized = _normalize_goal(model_goal)
    if normalized is not None:
        return normalized, "classifier"
    return _normalize_goal(fallback) or _FALLBACK_GOAL, "fallback"


def _valid_goal(value: str | None) -> bool:
    return _normalize_goal(value) is not None


def is_social_message(value: str) -> bool:
    """True only for a pure greeting/small-talk turn.

    A greeting plus a real request ("你好，帮我修部署") is deliberately not
    social: the request must still reach the task classifier.
    """
    cleaned = _EMOJI_RE.sub("", str(value or "")).strip()
    return bool(cleaned) and bool(_SOCIAL_RE.fullmatch(cleaned))


def is_continuation_prompt(value: str) -> bool:
    """True only for a pure "continue" cue such as 继续 / 接着做 / go on."""
    cleaned = _EMOJI_RE.sub("", str(value or "")).strip()
    return bool(cleaned) and bool(_CONTINUATION_RE.fullmatch(cleaned))


def _reuse_existing_task(
    existing: TaskContext,
    current_id: str | None,
    *,
    policy: str,
) -> dict[str, Any] | None:
    task_id = _text(existing.get("task_id"))
    if task_id is None:
        # A greeting/continuation must not create a task or a goal.
        return None
    context: TaskContext = dict(existing)
    context["boundary_decision"] = "CONTINUE"
    context["boundary_uncertain"] = False
    context["boundary_pending"] = False
    context["last_classified_message_id"] = current_id or ""
    _emit_task_event(
        decision="CONTINUE",
        task_goal_source=str(existing.get("task_goal_source") or "classifier"),
        previous_task_id=task_id,
        task_id=task_id,
        policy=policy,
    )
    return {"task_context": context}


def _normalize_goal(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split()).strip()
    if not compact or len(compact) > _MAX_GOAL_CHARS or looks_like_secret(compact):
        return None
    return compact


def _fit_input(
    current: str,
    goal: str,
    runs: list[RunPair],
    max_input_tokens: int,
) -> tuple[str, str, list[RunPair]]:
    """Approximate the token budget with a 4-characters-per-token ceiling."""
    budget = max(1, int(max_input_tokens)) * 4
    current = current[:budget]
    goal = goal[:budget]
    used = len(current) + len(goal)
    selected: list[RunPair] = []
    for pair in reversed(runs):
        size = len(pair["human_message"]) + len(pair["final_ai_message"])
        if used + size > budget:
            break
        selected.append(pair)
        used += size
    return current, goal, list(reversed(selected))


def _bounded_runs(value: Any, limit: int) -> list[RunPair]:
    if not isinstance(value, list) or limit <= 0:
        return []
    runs: list[RunPair] = []
    for item in value[-limit:]:
        if not isinstance(item, Mapping):
            continue
        runs.append(
            {
                "run_id": str(item.get("run_id") or ""),
                "human_message": str(item.get("human_message") or ""),
                "final_ai_message": str(item.get("final_ai_message") or ""),
            }
        )
    return runs


def _task_context(value: Any) -> TaskContext:
    if isinstance(value, Mapping):
        return dict(value)  # type: ignore[return-value]
    return {}  # type: ignore[return-value]


def _latest_real_user(messages: list[Any]) -> HumanMessage | None:
    for message in reversed(messages):
        if is_real_user_message(message):
            return message
    return None


def _message_id(message: Any) -> str | None:
    value = getattr(message, "id", None)
    return value if isinstance(value, str) and value else None


def _redact(value: str) -> str:
    return PIIRedactor().redact_for_memory(str(value or "")).strip()


def _build_model(config: MemoryConfig, app_config: Any) -> Any:
    if app_config is None:
        raise RuntimeError("task classifier requires app_config")
    if config.task_classifier.model == "agent_default":
        return resolve_operator_model(app_config)
    return create_chat_model(
        name=config.task_classifier.model,
        thinking_enabled=False,
        attach_tracing=False,
        app_config=app_config,
    )


def _context(runtime: Any) -> dict[str, Any]:
    context = getattr(runtime, "context", None)
    return dict(context) if isinstance(context, Mapping) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _emit_task_event(
    *,
    decision: str,
    task_goal_source: str,
    previous_task_id: str | None,
    task_id: str | None,
    policy: str,
) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        return
    try:
        emit_custom_event(
            {
                "type": "task:classified",
                "decision": decision,
                "task_goal_source": task_goal_source,
                "previous_task_id": previous_task_id,
                "task_id": task_id,
                "uncertain_policy": policy,
            },
            writer=writer,
        )
    except Exception:
        logger.debug("task:classified event failed", exc_info=True)
