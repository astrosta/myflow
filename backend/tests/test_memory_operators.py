"""Task 4: evidence mapping and stateless LangMem operator contracts."""

from __future__ import annotations

import asyncio

import pytest

from agentkit.agents.memory.canonical import evidence
from agentkit.agents.memory.canonical.fakes import FakeOperators
from agentkit.agents.memory.canonical.operators import LangMemOperators


def test_evidence_tier_confidence_is_fixed() -> None:
    assert evidence.confidence_for_tier("EXPLICIT_MANDATE") == 0.95
    assert evidence.confidence_for_tier("EXPLICIT_STATEMENT") == 0.85
    assert evidence.confidence_for_tier("BEHAVIORAL_OBSERVED") == 0.70
    assert evidence.confidence_for_tier("INFERRED_HYPOTHESIS") == 0.50
    assert evidence.is_active_confidence(0.80) is True
    assert evidence.is_pending_confidence(0.79) is True
    assert evidence.is_pending_confidence(0.49) is False
    assert evidence.is_profile_confidence(0.85) is True
    assert evidence.is_profile_confidence(0.84) is False


def test_fake_operators_are_stateless() -> None:
    ops = FakeOperators()
    assert asyncio.run(ops.extract_semantic([], [])) == []
    assert ops.calls == ["extract_semantic"]


def test_operator_rejects_unsupported_model() -> None:
    class NoTools:
        pass

    with pytest.raises(Exception, match="structured output|tool"):
        LangMemOperators(NoTools(), timeout_seconds=1)
