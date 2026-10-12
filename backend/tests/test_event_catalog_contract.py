"""§4.3 catalog is the authority for event_type / category, and matches the contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentkit.runtime.events.catalog import RUN_EVENT_CATEGORIES, RunEventDefinition
from agentkit.runtime.events.constants import (
    RUN_EVENT_CATEGORY_MAX_LENGTH,
    RUN_EVENT_ID_MAX_LENGTH,
    RUN_EVENT_TYPE_MAX_LENGTH,
)
from agentkit.persistence.events.codec import normalize_event

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


def test_every_catalog_definition_uses_a_category_from_the_value_set() -> None:
    import agentkit.runtime.events.catalog as catalog

    declared = {
        value.category for value in vars(catalog).values() if isinstance(value, RunEventDefinition)
    }
    assert declared <= RUN_EVENT_CATEGORIES, "catalog 里出现了取值集合之外的 category"


def _envelope(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "thread_id": "thread-1",
        "run_id": "run-1",
        "event_type": "run.start",
        "category": "trace",
        "content": "",
        "metadata": {},
    }
    event.update(overrides)
    return event


def test_writes_outside_the_catalog_are_rejected_by_the_store() -> None:
    """§5.6：category 不在 catalog 的取值集合里 → 抛；写上来的 seq / created_at 一律丢掉。"""
    assert normalize_event(_envelope())["category"] == "trace"

    with pytest.raises(ValueError, match="category"):
        normalize_event(_envelope(category="nonsense"))

    with pytest.raises(ValueError, match="thread_id"):
        normalize_event(_envelope(thread_id="t" * (RUN_EVENT_ID_MAX_LENGTH + 1)))

    with pytest.raises(ValueError, match="run_id"):
        normalize_event(_envelope(run_id="r" * (RUN_EVENT_ID_MAX_LENGTH + 1)))

    stored = normalize_event(_envelope(seq=99, created_at="1999-01-01T00:00:00Z"))
    assert "seq" not in stored and "created_at" not in stored, "store 自己分配这两个值"


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
