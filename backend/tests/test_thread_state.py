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
    assert merge_retrieved_tool_names(["cf__github_search"], ["cf__gitlab_search"]) == [
        "cf__gitlab_search"
    ]


def test_retrieved_tool_names_empty_list_clears_the_previous_search() -> None:
    assert merge_retrieved_tool_names(["cf__github_search"], []) == []


def test_retrieved_tool_names_none_leaves_the_state_untouched() -> None:
    assert merge_retrieved_tool_names(["cf__github_search"], None) == [
        "cf__github_search"
    ]


def test_retrieved_tool_names_dedupe_and_cap_at_five() -> None:
    merged = merge_retrieved_tool_names(
        ["cf__a"], ["cf__a", "cf__b", "cf__c", "cf__d", "cf__e", "cf__f"]
    )

    assert RETRIEVED_TOOL_NAMES_MAX_ENTRIES == 5
    assert merged == ["cf__a", "cf__b", "cf__c", "cf__d", "cf__e"]
