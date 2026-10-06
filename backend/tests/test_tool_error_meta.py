"""Tool results get one structured stamp; classification stops at the first matching rule."""

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agentkit.agents.middlewares.tool_result_meta import (
    TOOL_META_KEY,
    normalize_tool_result,
    stamp_exception_meta,
)

_ROWS = (
    ("401 unauthorized", "auth", False, "stop"),
    ("rate limit exceeded", "rate_limited", False, "summarize"),
    ("connection timeout", "transient", False, "try_alternative"),
    ("tool is not configured", "config", False, "stop"),
    ("permission denied", "permission", True, "try_alternative"),
    ("no results found", "no_results", True, "rewrite_query"),
    ("no such file", "not_found", True, "rewrite_query"),
    ("internal error", "internal", False, "stop"),
    ("disk is full", "unknown", True, "try_alternative"),
)


def _tool(content: str, *, status: str = "success", name: str = "search", tool_call_id: str = "call-1", **kwargs: object) -> ToolMessage:
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=name,
        status=status,
        additional_kwargs=dict(kwargs),
    )


def _meta(message: ToolMessage) -> dict:
    return message.additional_kwargs[TOOL_META_KEY]


@pytest.mark.parametrize(("text", "error_type", "recoverable", "action"), _ROWS)
def test_error_prefix_uses_first_matching_rule(text: str, error_type: str, recoverable: bool, action: str) -> None:
    message = normalize_tool_result(_tool(f"Error: {text}", status="error"))
    assert _meta(message) == {
        "status": "error",
        "error_type": error_type,
        "recoverable_by_model": recoverable,
        "recommended_next_action": action,
        "source": "tool_return",
    }


def test_duration_suffix_is_not_an_http_500() -> None:
    message = normalize_tool_result(_tool("took 500ms", status="error"))
    assert _meta(message)["error_type"] == "unknown"
    assert _meta(message)["status"] == "error"


def test_semantic_zero_json_error_is_success() -> None:
    message = normalize_tool_result(_tool('{"error": "none", "results": [1], "user_id": 401}'))
    assert _meta(message)["status"] == "success"
    assert _meta(message)["recommended_next_action"] == "continue"
    assert _meta(message)["source"] == "content_analysis"


def test_json_false_null_and_empty_error_are_success() -> None:
    for content in ('{"error": false}', '{"error": null}', '{"error": ""}', '{"error": "OK"}'):
        message = normalize_tool_result(_tool(content, status="error"))
        assert _meta(message)["status"] == "success"


def test_error_status_json_without_error_field_is_unknown() -> None:
    message = normalize_tool_result(_tool('{"user_id": 401}', status="error"))
    meta = _meta(message)
    assert meta["status"] == "error"
    assert meta["error_type"] == "unknown"
    assert meta["source"] == "tool_return"


def test_success_status_json_error_string_is_classified() -> None:
    message = normalize_tool_result(_tool('{"error": 404, "results": []}'))
    meta = _meta(message)
    assert meta["error_type"] == "not_found"
    assert meta["status"] == "error"
    assert meta["source"] == "tool_return"


def test_existing_stamp_is_left_untouched() -> None:
    message = _tool("Error: 401 unauthorized", status="error", **{TOOL_META_KEY: {"status": "success"}, "trace": "1"})
    assert normalize_tool_result(message) is message


def test_partial_marker_rewrites_the_query() -> None:
    message = normalize_tool_result(_tool("Results may be incomplete for this query."))
    assert _meta(message) == {
        "status": "partial_success",
        "error_type": None,
        "recoverable_by_model": True,
        "recommended_next_action": "rewrite_query",
        "source": "content_analysis",
    }


def test_plain_success_keeps_other_kwargs() -> None:
    message = normalize_tool_result(_tool("all good", trace="1"))
    assert message.additional_kwargs["trace"] == "1"
    assert _meta(message)["status"] == "success"
    assert message.content == "all good"


def test_subagent_failure_classifies_subagent_error() -> None:
    message = normalize_tool_result(
        _tool(
            "looks fine",
            subagent_status="failed",
            subagent_error="connection timeout",
        )
    )
    meta = _meta(message)
    assert meta["status"] == "error"
    assert meta["error_type"] == "transient"
    assert meta["source"] == "tool_return"


def test_subagent_failure_without_detail_uses_content() -> None:
    message = normalize_tool_result(_tool("path traversal blocked", subagent_status="timed_out"))
    assert _meta(message)["error_type"] == "permission"


def test_web_fetch_http_reason_title_is_an_error() -> None:
    message = normalize_tool_result(_tool("HTTP 404 Not Found\n<html></html>", name="web_fetch"))
    meta = _meta(message)
    assert meta["status"] == "error"
    assert meta["error_type"] == "not_found"
    assert meta["source"] == "content_analysis"


def test_web_fetch_title_with_extra_words_stays_success() -> None:
    message = normalize_tool_result(_tool("404 Ways to Cook Rice\nA cookbook.", name="web_fetch"))
    assert _meta(message)["status"] == "success"


def test_stamp_exception_meta_overwrites_and_ignores_content_template() -> None:
    message = _tool(
        "Error: Tool 'search' failed with ValueError: 401 unauthorized. Continue with available context.",
        status="error",
        **{TOOL_META_KEY: {"status": "success"}},
    )
    stamped = stamp_exception_meta(message, "TimeoutError: connection timeout")
    meta = _meta(stamped)
    assert meta["source"] == "exception"
    assert meta["error_type"] == "transient"
    assert meta["status"] == "error"
    assert stamped.content == message.content


def test_command_messages_are_stamped_and_other_fields_stay() -> None:
    tool = _tool("Error: 403 unauthorized", status="error", tool_call_id="keep")
    other = {"role": "human", "content": "hi"}
    command = Command(update={"messages": [other, tool], "artifacts": ["a"]}, goto="agent")
    stamped = normalize_tool_result(command)
    assert stamped.goto == "agent"
    assert stamped.update["artifacts"] == ["a"]
    assert stamped.update["messages"][0] is other
    assert _meta(stamped.update["messages"][1])["error_type"] == "auth"


def test_command_stamps_only_the_matching_tool_call() -> None:
    first = _tool("Error: 401 unauthorized", status="error", tool_call_id="a")
    second = _tool("Error: no such file", status="error", tool_call_id="b")
    command = Command(update={"messages": [first, second], "n": 1})
    stamped = normalize_tool_result(command, tool_call_id="b")
    assert TOOL_META_KEY not in stamped.update["messages"][0].additional_kwargs
    assert _meta(stamped.update["messages"][1])["error_type"] == "not_found"
    assert stamped.update["n"] == 1


def test_command_accepts_a_single_tool_message() -> None:
    command = Command(update={"messages": _tool("done"), "n": 2}, goto="end")
    stamped = normalize_tool_result(command)
    assert isinstance(stamped.update["messages"], ToolMessage)
    assert _meta(stamped.update["messages"])["status"] == "success"
    assert stamped.goto == "end"
    assert stamped.update["n"] == 2


def test_command_without_a_message_list_is_unchanged() -> None:
    missing = Command(update={"artifacts": []}, goto="agent")
    assert normalize_tool_result(missing) is missing
    scalar = Command(update={"messages": "nope"})
    assert normalize_tool_result(scalar) is scalar
    unchanged = Command(update=None)
    assert normalize_tool_result(unchanged) is unchanged
