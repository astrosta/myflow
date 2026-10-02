"""§2.1: every reducer field is registered, and every registered name has a reducer."""

import inspect
from typing import Annotated, NotRequired, Required, get_args, get_origin, get_type_hints

from agentkit.agents.thread_state import THREAD_STATE_REDUCER_FIELDS, ThreadState, get_thread_state_schema


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
