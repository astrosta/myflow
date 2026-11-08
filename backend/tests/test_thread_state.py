"""§2.1: every reducer field is registered, and every registered name has a reducer."""

import inspect
from typing import Annotated, NotRequired, Required, get_args, get_origin, get_type_hints

from agentkit.agents.thread_state import (
    RETRIEVED_TOOL_NAMES_MAX_ENTRIES,
    THREAD_STATE_REDUCER_FIELDS,
    ThreadState,
    get_thread_state_schema,
    merge_retrieved_tool_names,
)


def _reducer(hint):
    origin = get_origin(hint)
    if origin in (Required, NotRequired):
        hint = get_args(hint)[0]
        origin = get_origin(hint)
    if origin is not Annotated:
        return None
    for item in get_args(hint)[1:]:
        if inspect.isfunction(item):
            return item
    return None


def test_reducer_registry_matches_annotated_fields() -> None:
    hints = get_type_hints(ThreadState, include_extras=True)
    found = {name for name, hint in hints.items() if _reducer(hint) is not None}
    assert found == set(THREAD_STATE_REDUCER_FIELDS)


def test_get_thread_state_schema_returns_thread_state() -> None:
    assert get_thread_state_schema() is ThreadState


# --------------------------------------------------------------------------- #
# retrieved_tool_names: tool_search's run-scoped card list
# --------------------------------------------------------------------------- #


def test_retrieved_tool_names_replace_instead_of_merging() -> None:
    assert merge_retrieved_tool_names(
        ["mcp__github__search"], ["mcp__gitlab__search"]
    ) == ["mcp__gitlab__search"]


def test_retrieved_tool_names_empty_list_clears_the_previous_search() -> None:
    assert merge_retrieved_tool_names(["mcp__github__search"], []) == []


def test_retrieved_tool_names_none_leaves_the_state_untouched() -> None:
    assert merge_retrieved_tool_names(["mcp__github__search"], None) == [
        "mcp__github__search"
    ]


def test_retrieved_tool_names_dedupe_and_cap_at_five() -> None:
    merged = merge_retrieved_tool_names(
        ["mcp__a"],
        [
            "mcp__a",
            "mcp__b",
            "mcp__c",
            "mcp__d",
            "mcp__e",
            "mcp__f",
        ],
    )

    assert RETRIEVED_TOOL_NAMES_MAX_ENTRIES == 5
    assert merged == ["mcp__a", "mcp__b", "mcp__c", "mcp__d", "mcp__e"]
