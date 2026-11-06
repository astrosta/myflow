from agentkit.contextforge.admin_events import (
    parse_sse_block,
    should_refresh_tool_event,
)


def test_parse_multiline_event_and_keepalive() -> None:
    event = parse_sse_block(
        "event: tool_updated\ndata: {\"id\": \"t1\"}\n\ndata: ignored\n\n"
    )
    assert event is not None
    assert event.type == "tool_updated"
    assert event.data == {"id": "t1"}
    assert parse_sse_block(": keepalive\n\n") is None


def test_tool_and_gateway_events_refresh() -> None:
    assert should_refresh_tool_event("tool_updated") is True
    assert should_refresh_tool_event("gateway_activated") is True
    assert should_refresh_tool_event("tool_executed") is False
