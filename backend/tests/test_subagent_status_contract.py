"""M8 §9.3：Python 元组与 contracts/subagent_status_contract.json **双向**钉死。

跨语言契约最容易烂的方式是"两边各改一边"：Python 加一个状态值、前端枚举没跟上，
于是前端遇到未知值就把卡片画成空白。这个文件让两边的任何一边改动都先在这里红。
"""

from __future__ import annotations

import pytest

from agentkit.agents.thread_state import STOP_REASONS, TERMINAL_STATUSES
from agentkit.subagents.results import EXTERNAL_STATUS_BY_RESULT_STATUS, SubagentStatus
from agentkit.subagents.status_contract import (
    LEGACY_STATUS_VALUES,
    SUBAGENT_RESULT_MAX_CHARS,
    SUBAGENT_STATUS_VALUES,
    SUBAGENT_STOP_REASON_VALUES,
    SUBAGENT_TOOL_MESSAGE_KEYS,
    SubagentUsage,
    contract_path,
    format_subagent_result_message,
    load_subagent_status_contract,
    make_subagent_additional_kwargs,
    normalize_status,
    subagent_status_contract,
)


def test_the_shared_json_is_the_python_contract() -> None:
    assert load_subagent_status_contract() == subagent_status_contract()
    assert contract_path().is_file()


def test_the_json_lists_exactly_the_valid_values() -> None:
    contract = load_subagent_status_contract()
    assert sorted(contract["valid_status_values"]) == sorted(SUBAGENT_STATUS_VALUES)
    assert sorted(contract["valid_stop_reason_values"]) == sorted(SUBAGENT_STOP_REASON_VALUES)


def test_the_external_values_are_the_ledger_terminal_statuses() -> None:
    """台账（§6.1）与对外契约（§9.3）必须是同一套值，否则卡片和台账会对不上。"""
    assert set(SUBAGENT_STATUS_VALUES) == TERMINAL_STATUSES
    assert set(SUBAGENT_STOP_REASON_VALUES) == STOP_REASONS


def test_polling_timed_out_is_not_an_execution_status() -> None:
    """§8：``polling_timed_out`` 只是**父侧**给出的 ToolMessage 状态，执行侧仍是 timed_out。"""
    assert SubagentStatus.POLLING_TIMED_OUT.value == "polling_timed_out"
    assert SubagentStatus.POLLING_TIMED_OUT not in EXTERNAL_STATUS_BY_RESULT_STATUS
    assert EXTERNAL_STATUS_BY_RESULT_STATUS[SubagentStatus.TIMED_OUT] == "timed_out"


def test_every_terminal_status_maps_to_a_contract_value() -> None:
    #: ``POLLING_TIMED_OUT`` 是父侧结果，不经执行侧 holder → 对外映射表。
    mapped = {
        status
        for status in SubagentStatus
        if status.is_terminal and status is not SubagentStatus.POLLING_TIMED_OUT
    }
    assert set(EXTERNAL_STATUS_BY_RESULT_STATUS) == mapped
    assert set(EXTERNAL_STATUS_BY_RESULT_STATUS.values()) <= set(SUBAGENT_STATUS_VALUES)
    assert SubagentStatus.PENDING not in EXTERNAL_STATUS_BY_RESULT_STATUS
    assert SubagentStatus.RUNNING not in EXTERNAL_STATUS_BY_RESULT_STATUS


def test_read_side_maps_the_legacy_max_turns_value() -> None:
    assert LEGACY_STATUS_VALUES["max_turns_reached"] == ("failed", "turn_capped")
    assert normalize_status("max_turns_reached") == ("failed", "turn_capped")


def test_write_side_never_produces_the_legacy_value() -> None:
    with pytest.raises(ValueError, match="unknown subagent status"):
        make_subagent_additional_kwargs(status="max_turns_reached")


def test_an_unknown_status_reads_as_failed() -> None:
    """宁可说"没做成"，也不要把没验证的东西显示成完成。"""
    assert normalize_status("weird") == ("failed", None)
    assert normalize_status(None) == ("failed", None)
    assert normalize_status("completed") == ("completed", None)


def test_stop_reason_is_optional_and_validated() -> None:
    kwargs = make_subagent_additional_kwargs(status="completed", result="half", stop_reason="turn_capped")
    assert kwargs["subagent_stop_reason"] == "turn_capped"
    with pytest.raises(ValueError, match="unknown stop_reason"):
        make_subagent_additional_kwargs(status="completed", stop_reason="timeout")


def test_only_completed_carries_the_result_digest() -> None:
    """§9.3 的表：brief / sha256 只跟 completed 走；失败时前端读 ``subagent_error``。"""
    completed = make_subagent_additional_kwargs(status="completed", result="the answer")
    assert completed["subagent_result_brief"] == "the answer"
    assert len(completed["subagent_result_sha256"]) == 64

    failed = make_subagent_additional_kwargs(status="failed", result="partial", error="boom")
    assert "subagent_result_brief" not in failed
    assert "subagent_result_sha256" not in failed
    assert failed["subagent_error"] == "boom"


def test_kwargs_only_use_the_documented_keys() -> None:
    kwargs = make_subagent_additional_kwargs(
        status="completed", result="x", model_name="m", usage=SubagentUsage(1, 2, 3)
    )
    assert set(kwargs) <= SUBAGENT_TOOL_MESSAGE_KEYS
    assert SUBAGENT_TOOL_MESSAGE_KEYS == {
        "subagent_status",
        "subagent_stop_reason",
        "subagent_error",
        "subagent_result_brief",
        "subagent_result_sha256",
        "subagent_model_name",
        "subagent_token_usage",
    }


def test_usage_is_omitted_instead_of_zeroed() -> None:
    """§15：0 和"未知"是不同的信息。provider 没给就整个键都不写。"""
    assert SubagentUsage(0, 0, 0).to_payload() is None
    kwargs = make_subagent_additional_kwargs(status="failed", error="boom", usage=SubagentUsage(0, 0, 0))
    assert "subagent_token_usage" not in kwargs


def test_usage_round_trips_and_rejects_malformed_payloads() -> None:
    payload = SubagentUsage(10, 5, 15).to_payload()
    assert payload == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert SubagentUsage.from_payload(payload) == SubagentUsage(10, 5, 15)
    assert SubagentUsage.from_payload({"input_tokens": "10"}) is None
    assert SubagentUsage.from_payload(None) is None
    assert SubagentUsage.from_payload({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}) is None


def test_the_result_message_marks_a_capped_completion_as_partial() -> None:
    """§13.1：状态仍是 completed，但要说明"没做全"，否则 lead 会当成完整答案。"""
    message = format_subagent_result_message(status="completed", result="half", stop_reason="turn_capped")
    assert message.startswith("任务成功（触发护栏上限：轮次预算；结果为部分结果）。")
    assert "结果：half" in message


def test_the_result_message_covers_every_status() -> None:
    for status in SUBAGENT_STATUS_VALUES:
        assert "任务" in format_subagent_result_message(status=status, error="why")


def test_the_result_budget_is_bounded() -> None:
    assert SUBAGENT_RESULT_MAX_CHARS == 8192
