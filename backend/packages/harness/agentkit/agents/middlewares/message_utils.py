"""Message-list predicates shared by agent middlewares."""

from collections.abc import Mapping

from langchain_core.messages import HumanMessage

from agentkit.agents.middlewares.provenance import read_provenance

_SUMMARY_MESSAGE_NAME = "summary"
_HUMAN_INPUT_RESPONSE_KEY = "human_input_response"


def is_genuine_user_message(message: object) -> bool:
    """Real user text, not a summary row or a hidden injection.

    ``hide_from_ui`` also marks a HumanInputCard reply. Those replies are
    genuine; hidden messages without a valid response are not.
    """
    if not isinstance(message, HumanMessage):
        return False
    if message.name == _SUMMARY_MESSAGE_NAME:
        return False
    kwargs = message.additional_kwargs or {}
    if kwargs.get("hide_from_ui") and read_human_input_response(kwargs) is None:
        return False
    return True


def is_real_user_message(message: object) -> bool:
    """The user's own turn. Dict rows, summaries, and injected blocks are not.

    A hidden human-input reply is a separate path and is not this turn's request.
    An id ending in ``__user`` is still the current request.
    """
    if not isinstance(message, HumanMessage):
        return False
    if getattr(message, "name", None) == _SUMMARY_MESSAGE_NAME:
        return False
    kwargs = message.additional_kwargs or {}
    if kwargs.get("hide_from_ui"):
        return False
    if kwargs.get("dynamic_context_reminder"):
        return False
    if read_provenance(message) is not None:
        return False
    return True


def read_human_input_response(additional_kwargs: Mapping | None) -> dict | None:
    """Return a valid human-input response payload, or None."""
    if not additional_kwargs:
        return None
    raw = additional_kwargs.get(_HUMAN_INPUT_RESPONSE_KEY)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("version") != 1 or raw.get("kind") != "human_input_response":
        return None
    source = _non_empty_string(raw.get("source"))
    request_id = _non_empty_string(raw.get("request_id"))
    value = _non_empty_string(raw.get("value"))
    if source is None or request_id is None or value is None:
        return None
    response_kind = raw.get("response_kind")
    if response_kind == "text":
        return {
            "version": 1,
            "kind": "human_input_response",
            "source": source,
            "request_id": request_id,
            "response_kind": "text",
            "value": value,
        }
    if response_kind == "option":
        option_id = _non_empty_string(raw.get("option_id"))
        if option_id is None:
            return None
        return {
            "version": 1,
            "kind": "human_input_response",
            "source": source,
            "request_id": request_id,
            "response_kind": "option",
            "option_id": option_id,
            "value": value,
        }
    return None


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def clone_ai_message_with_tool_calls(message: object, tool_calls: list) -> object:
    """换掉一条 AIMessage 的 tool_calls，并**同步 provider 的原始元数据**（M6 §5.1 / M8 §10.3）。

    只改结构化字段会留下悬空调用：raw ``additional_kwargs["tool_calls"]`` 里那条还在，
    下一次请求就会带着一个永远不会有结果的 tool_call 过去（严格后端直接 400）。
    raw 列表按 id 对齐；没有 id 的条目留着（分辨不出是哪一条）。

    返回值保持同一个 ``id`` —— ``add_messages`` 按 id 替换，所以在 ``after_model`` 里
    直接 ``{"messages": [new]}`` 就是"换掉最后那条 AI 消息"。
    """
    kwargs = dict(getattr(message, "additional_kwargs", None) or {})
    raw = kwargs.get("tool_calls")
    if isinstance(raw, list):
        kept_ids = {
            call.get("id")
            for call in tool_calls
            if isinstance(call, Mapping) and isinstance(call.get("id"), str)
        }
        kwargs["tool_calls"] = [
            entry
            for entry in raw
            if not isinstance(entry, Mapping) or entry.get("id") is None or entry.get("id") in kept_ids
        ]
    return message.model_copy(update={"tool_calls": list(tool_calls), "additional_kwargs": kwargs})


def insert_after_leading_system_messages(messages: list, injected: list) -> list:
    """Place injected messages after the opening system prompt, without copying the input lists."""
    from langchain_core.messages import SystemMessage

    index = 0
    while index < len(messages) and isinstance(messages[index], SystemMessage):
        index += 1
    return [*messages[:index], *injected, *messages[index:]]
