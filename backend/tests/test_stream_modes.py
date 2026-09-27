import pytest

from agentkit.runtime.stream_modes import (
    UnsupportedStreamModeError,
    normalize_stream_modes,
    to_internal_stream_modes,
)


def test_none_defaults_to_values() -> None:
    assert normalize_stream_modes(None) == ["values"]


def test_string_is_wrapped_and_public_name_is_kept() -> None:
    assert normalize_stream_modes("messages-tuple") == ["messages-tuple"]


def test_public_modes_are_preserved_in_order() -> None:
    raw = ["custom", "updates", "values", "messages-tuple"]
    assert normalize_stream_modes(raw) == raw


def test_events_is_rejected() -> None:
    with pytest.raises(UnsupportedStreamModeError, match="events"):
        normalize_stream_modes(["events"])


def test_internal_messages_name_is_rejected() -> None:
    with pytest.raises(UnsupportedStreamModeError, match="messages"):
        normalize_stream_modes("messages")


def test_unsupported_mode_is_not_replaced_with_values() -> None:
    with pytest.raises(UnsupportedStreamModeError):
        normalize_stream_modes(["values", "debug"])


def test_messages_tuple_maps_to_internal_messages() -> None:
    public = normalize_stream_modes(["messages-tuple", "values", "updates", "custom"])
    assert to_internal_stream_modes(public) == ["messages", "values", "updates", "custom"]
