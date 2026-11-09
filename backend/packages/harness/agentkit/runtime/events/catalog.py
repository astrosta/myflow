"""Authority for event_type and category. Producers import these objects."""

from dataclasses import dataclass

from agentkit.runtime.events.constants import (
    RUN_EVENT_CATEGORY_MAX_LENGTH,
    RUN_EVENT_TYPE_MAX_LENGTH,
)


# §4.3 的取值集合：list_messages / list_events 靠它分区，后加意味着历史行没有正确的值。
RUN_EVENT_CATEGORIES = frozenset({
    "message",
    "trace",
    "outputs",
    "error",
    "subagent",
    "context",
    "workspace",
    "retrieval",
})


@dataclass(frozen=True, slots=True)
class RunEventDefinition:
    event_type: str
    category: str

    def __post_init__(self) -> None:
        if len(self.event_type) > RUN_EVENT_TYPE_MAX_LENGTH:
            raise ValueError(
                f"event_type {self.event_type!r} exceeds {RUN_EVENT_TYPE_MAX_LENGTH} characters"
            )
        if len(self.category) > RUN_EVENT_CATEGORY_MAX_LENGTH:
            raise ValueError(
                f"category {self.category!r} exceeds {RUN_EVENT_CATEGORY_MAX_LENGTH} characters"
            )


RUN_START_EVENT = RunEventDefinition("run.start", "trace")
RUN_END_EVENT = RunEventDefinition("run.end", "outputs")
RUN_ERROR_EVENT = RunEventDefinition("run.error", "error")
LLM_HUMAN_INPUT = RunEventDefinition("llm.human.input", "message")
LLM_AI_RESPONSE = RunEventDefinition("llm.ai.response", "message")
LLM_TOOL_RESULT = RunEventDefinition("llm.tool.result", "message")
LLM_ERROR_EVENT = RunEventDefinition("llm.error", "trace")

# M10 §2.8.3：注入账本只记 revision/hash/tokens，正文留在 checkpoint 的动态上下文里。
MEMORY_CONTEXT_EVENT = RunEventDefinition("context:memory", "context")

# M8 §14.3：子代理的三条事件。``category="subagent"`` 在 16 的上限内（8 字符），
# ``list_messages`` 天然不返回它们——用户的主会话只有 lead 的消息和工具结果。
SUBAGENT_START_EVENT = RunEventDefinition("subagent.start", "subagent")
SUBAGENT_STEP_EVENT = RunEventDefinition("subagent.step", "subagent")
SUBAGENT_END_EVENT = RunEventDefinition("subagent.end", "subagent")

# 评测（Task 2）：检索结果是评测样本的原始证据，必须结构化落库，而不是从
# llm.tool.result 或 subagent.step 的截断 JSON 里反推。三条事件各对应一个检索工具，
# 内容是该工具的最终模型可见结果（tool_search 预算裁剪之后）。
RETRIEVAL_TOOL_EVENT = RunEventDefinition("retrieval.tool", "retrieval")
RETRIEVAL_SKILL_EVENT = RunEventDefinition("retrieval.skill", "retrieval")
RETRIEVAL_KB_EVENT = RunEventDefinition("retrieval.kb", "retrieval")
