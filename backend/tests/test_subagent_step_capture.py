"""M8 §14.1 / §14.2：步骤捕获是**纯函数**，不启动图、不需要 Gateway。

三个必须钉住的点：

* 一轮多工具：ToolNode 会在**一个超级步**里追加 3 条 ToolMessage，只看 ``messages[-1]`` 会丢两条
* 压缩收缩：摘要把整条消息通道重写过，``len(messages)`` 会突然变小——游标要重置，而且不能重发
* 载荷有界：一次 ``write_file`` 的全文不能变成无界的一行事件
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agentkit.subagents.step_events import (
    SUBAGENT_STEP_MAX_CHARS,
    build_subagent_step,
    capture_new_step_messages,
    capture_step_message,
)


def _ai(text: str = "", *, id: str | None = None, tool_calls: list | None = None) -> AIMessage:
    return AIMessage(content=text, id=id, tool_calls=tool_calls or [])


def _tool(text: str, *, id: str | None = None, call_id: str = "call-1") -> ToolMessage:
    return ToolMessage(content=text, id=id, tool_call_id=call_id, name="read_file")


def _capture(messages: list, captured: list, seen: set, cursor: int) -> int:
    return capture_new_step_messages(messages, captured, seen, cursor)


def test_a_super_step_with_three_tool_messages_is_fully_captured() -> None:
    """§14.1：一轮 3 个工具 = 一个超级步里 3 条 ToolMessage，一条都不能丢。"""
    calls = [{"name": "read_file", "id": f"call-{index}", "args": {"path": "/a"}} for index in range(3)]
    messages = [
        HumanMessage(content="go"),
        _ai("thinking", id="ai-1", tool_calls=calls),
        _tool("A", id="tool-1", call_id="call-0"),
        _tool("B", id="tool-2", call_id="call-1"),
        _tool("C", id="tool-3", call_id="call-2"),
    ]
    captured: list = []

    cursor = _capture(messages, captured, set(), 0)

    assert [step["message_index"] for step in captured] == [1, 2, 3, 4]
    assert [step["message"]["content"] for step in captured] == ["thinking", "A", "B", "C"]
    assert cursor == len(messages)


def test_only_human_and_system_messages_are_ignored() -> None:
    messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    captured: list = []

    _capture(messages, captured, set(), 0)

    assert captured == []


def test_the_cursor_advances_without_resending_old_steps() -> None:
    messages = [_ai("first", id="ai-1")]
    captured: list = []
    seen: set = set()
    cursor = _capture(messages, captured, seen, 0)

    messages.append(_tool("done", id="tool-1"))
    cursor = _capture(messages, captured, seen, cursor)

    assert [step["message"]["id"] for step in captured] == ["ai-1", "tool-1"]
    assert cursor == 2


def test_an_unnamed_message_with_the_same_content_is_not_captured_twice() -> None:
    """没 id 就按整 dict 去重：同一条消息被重复交出时不能重复计数。"""
    messages = [_ai("same")]
    captured: list = []
    seen: set = set()
    cursor = _capture(messages, captured, seen, 0)

    cursor = _capture(messages, captured, seen, cursor)  # 等长、没变

    assert len(captured) == 1
    assert cursor == 1


def test_an_in_place_replacement_is_captured() -> None:
    """等长：同一条消息被换掉（新 id）→ 还是要捕获。"""
    messages = [_ai("first", id="ai-1")]
    captured: list = []
    seen: set = set()
    cursor = _capture(messages, captured, seen, 0)

    messages[-1] = _ai("rewritten", id="ai-2")
    _capture(messages, captured, seen, cursor)

    assert [step["message"]["id"] for step in captured] == ["ai-1", "ai-2"]
    assert captured[-1]["message_index"] == 0


def test_the_same_id_is_never_captured_twice() -> None:
    """有 id 按 id 去重（§14.1）——同一条消息换个内容也还是同一条。"""
    messages = [ToolMessage(content="v1", id="tool-1", tool_call_id="call-1")]
    captured: list = []
    seen: set = set()
    cursor = _capture(messages, captured, seen, 0)

    messages[-1] = ToolMessage(content="v2", id="tool-1", tool_call_id="call-1")
    _capture(messages, captured, seen, cursor)

    assert len(captured) == 1


def test_a_compaction_shrink_resets_the_cursor_to_the_new_tail() -> None:
    """§14.1 的收缩问题：压缩把消息通道改短，游标必须重置到新尾部。

    不重置的话，之后新产生的步骤会一直被丢弃，直到 ``total`` 重新超过那个陈旧的游标。
    """
    messages = [_ai("one", id="ai-1"), _tool("two", id="tool-1"), _ai("three", id="ai-2")]
    captured: list = []
    seen: set = set()
    cursor = _capture(messages, captured, seen, 0)
    assert cursor == 3

    # 压缩：整条通道被重写成"摘要 + 尾巴"，长度掉回 2
    messages = [_ai("summary", id="ai-9"), _tool("fresh", id="tool-9")]
    cursor = _capture(messages, captured, seen, cursor)

    assert cursor == 2, "游标要跟着新的尾部走"
    # 重置之后只重看新的 ``messages[-1]``（§14.1 的不变量）
    assert [step["message"]["id"] for step in captured] == ["ai-1", "tool-1", "ai-2", "tool-9"]

    # 之后照常推进：压缩不该让后续步骤永久失明
    messages.append(_tool("after", id="tool-10"))
    _capture(messages, captured, seen, cursor)
    assert captured[-1]["message"]["id"] == "tool-10"


def test_a_shrink_that_repeats_a_known_tail_does_not_resend_it() -> None:
    """压缩后留下的尾巴是见过的 → 不能重发（靠 id 去重）。"""
    tail = _tool("kept", id="tool-kept")
    messages = [_ai("old", id="ai-1"), tail]
    captured: list = []
    seen: set = set()
    cursor = _capture(messages, captured, seen, 0)

    messages = [_ai("summary", id="ai-sum"), tail]
    _capture(messages, captured, seen, cursor)

    assert [step["message"]["id"] for step in captured] == ["ai-1", "tool-kept"]


def test_the_compaction_invariant_nothing_new_appears_below_the_cursor() -> None:
    """§14.1 的**不变量**，写成断言：压缩不得在游标以下插入新的可捕获消息。

    今天 Summarization 把摘要放进 ``summary_text``，messages 通道只留已经见过的尾巴——
    所以重置后"等长"分支只重看 ``messages[-1]`` 就够了。哪天有中间件违反这条（在游标以下
    插入一条新消息，且长度没有变短），这里会红，提示"重置分支要改成全量重扫"。
    """
    messages = [_ai("known", id="ai-1"), _tool("known", id="tool-1")]
    captured: list = []
    seen: set = set()
    cursor = _capture(messages, captured, seen, 0)

    # 违反不变量的形状：等长、但在游标以下塞了一条新消息
    messages = [_ai("inserted", id="ai-new"), _tool("known", id="tool-1")]
    _capture(messages, captured, seen, cursor)

    assert [step["message"]["id"] for step in captured] == ["ai-1", "tool-1"]
    assert "ai-new" not in [step["message"]["id"] for step in captured], (
        "现在的实现看不到它——这正是 §14.1 要求「压缩只在尾部变化」的原因"
    )


def test_capture_step_message_reports_whether_it_captured() -> None:
    captured: list = []
    seen: set = set()

    assert capture_step_message(_ai("x", id="ai-1"), captured, seen, index=3) is True
    assert capture_step_message(_ai("x", id="ai-1"), captured, seen, index=3) is False
    assert capture_step_message(HumanMessage(content="hi"), captured, seen, index=4) is False
    assert captured[0]["message_index"] == 3


# ---------------------------------------------------------------- §14.2 载荷有界


def test_a_long_content_is_truncated_and_flagged() -> None:
    payload = build_subagent_step(_tool("x" * (SUBAGENT_STEP_MAX_CHARS + 500)))

    assert len(payload["content"]) == SUBAGENT_STEP_MAX_CHARS
    assert payload["content_truncated"] is True


def test_a_long_tool_call_becomes_a_preview() -> None:
    """一次 ``write_file`` 的全文不能变成无界的一行事件。"""
    huge = {"path": "/a", "content": "y" * (SUBAGENT_STEP_MAX_CHARS + 500)}
    payload = build_subagent_step(_ai("", id="ai-1", tool_calls=[{"name": "write_file", "id": "c1", "args": huge}]))

    call = payload["tool_calls"][0]
    assert "args" not in call
    assert call["args_truncated"] is True
    assert len(call["args_preview"]) == SUBAGENT_STEP_MAX_CHARS


def test_a_short_tool_call_keeps_its_arguments() -> None:
    payload = build_subagent_step(
        _ai("", id="ai-1", tool_calls=[{"name": "read_file", "id": "c1", "args": {"path": "/a"}}])
    )

    call = payload["tool_calls"][0]
    assert call["name"] == "read_file" and call["id"] == "c1"
    assert json.loads(call["args"]) == {"path": "/a"}
    assert "args_truncated" not in call


def test_the_payload_is_json_safe() -> None:
    payload = build_subagent_step(_tool("你好", id="tool-1"))
    assert json.loads(json.dumps(payload, ensure_ascii=False))["content"] == "你好"
    assert payload["tool_call_id"] == "call-1"
    assert payload["name"] == "read_file"
