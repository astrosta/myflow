"""First writer of original_user_content wins; later readers see that text."""

from agentkit.utils.messages import (
    INJECTED_USER_MESSAGE_ID_SUFFIX,
    ORIGINAL_USER_CONTENT_KEY,
    get_original_user_content_text,
    message_content_to_text,
    record_original_user_content,
)


def test_message_content_to_text_returns_str_unchanged() -> None:
    assert message_content_to_text("原话") == "原话"


def test_message_content_to_text_joins_str_and_text_blocks() -> None:
    content = [
        "第一段",
        {"type": "text", "text": "第二段"},
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        {"text": 1},
    ]
    assert message_content_to_text(content) == "第一段\n第二段"


def test_message_content_to_text_stringifies_other_values() -> None:
    assert message_content_to_text(None) == "None"


def test_record_writes_text_when_key_is_missing() -> None:
    kwargs: dict = {}
    record_original_user_content(kwargs, ["你好", {"text": "世界"}])
    assert kwargs[ORIGINAL_USER_CONTENT_KEY] == "你好\n世界"


def test_record_overwrites_non_string_existing_value() -> None:
    kwargs = {ORIGINAL_USER_CONTENT_KEY: 1}
    record_original_user_content(kwargs, "原话")
    assert kwargs[ORIGINAL_USER_CONTENT_KEY] == "原话"


def test_first_writer_wins_when_upload_then_sanitization_rewrite() -> None:
    kwargs: dict = {}
    original = "用户原话"
    record_original_user_content(kwargs, original)
    record_original_user_content(kwargs, "<current_uploads>\nfile.txt\n</current_uploads>\n用户原话")
    assert kwargs[ORIGINAL_USER_CONTENT_KEY] == original


def test_empty_string_is_a_winning_first_write() -> None:
    kwargs = {ORIGINAL_USER_CONTENT_KEY: ""}
    record_original_user_content(kwargs, "后来的包装")
    assert kwargs[ORIGINAL_USER_CONTENT_KEY] == ""


def test_reader_uses_recorded_original_not_rewritten_content() -> None:
    kwargs = {ORIGINAL_USER_CONTENT_KEY: "用户原话"}
    rewritten = "<current_uploads>\nfile.txt\n</current_uploads>\n用户原话"
    assert get_original_user_content_text(rewritten, kwargs) == "用户原话"


def test_reader_falls_back_to_content_without_a_string_original() -> None:
    assert get_original_user_content_text("现内容", None) == "现内容"
    assert get_original_user_content_text("现内容", {ORIGINAL_USER_CONTENT_KEY: 1}) == "现内容"
    assert get_original_user_content_text(["甲", {"text": "乙"}], {}) == "甲\n乙"


def test_injected_user_message_id_suffix() -> None:
    assert INJECTED_USER_MESSAGE_ID_SUFFIX == "__user"
