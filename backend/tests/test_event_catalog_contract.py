"""§4.3 catalog is the authority for event_type / category, and matches the contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentkit.runtime.events.catalog import RunEventDefinition
from agentkit.runtime.events.constants import (
    RUN_EVENT_CATEGORY_MAX_LENGTH,
    RUN_EVENT_TYPE_MAX_LENGTH,
)

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "run_event_stream_contract.json"


def _catalog_pairs() -> set[tuple[str, str]]:
    import agentkit.runtime.events.catalog as catalog

    return {
        (value.event_type, value.category)
        for value in vars(catalog).values()
        if isinstance(value, RunEventDefinition)
    }


def test_catalog_and_contract_list_the_same_events() -> None:
    document = json.loads(_CONTRACT.read_text(encoding="utf-8"))
    contract_pairs = {(item["event_type"], item["category"]) for item in document["events"]}
    assert _catalog_pairs() == contract_pairs
    assert len(document["events"]) == len(contract_pairs)


def test_overlong_event_type_or_category_is_rejected_at_construction() -> None:
    RunEventDefinition("x" * RUN_EVENT_TYPE_MAX_LENGTH, "y" * RUN_EVENT_CATEGORY_MAX_LENGTH)
    with pytest.raises(ValueError):
        RunEventDefinition("x" * (RUN_EVENT_TYPE_MAX_LENGTH + 1), "trace")
    with pytest.raises(ValueError):
        RunEventDefinition("run.start", "y" * (RUN_EVENT_CATEGORY_MAX_LENGTH + 1))

    prefix = "middleware:"
    tag_limit = RUN_EVENT_TYPE_MAX_LENGTH - len(prefix)
    RunEventDefinition(prefix + ("t" * tag_limit), "trace")
    with pytest.raises(ValueError):
        RunEventDefinition(prefix + ("t" * (tag_limit + 1)), "trace")
