"""Summarization settings fail closed on values that would silently disable compaction."""

import math

import pytest
from pydantic import ValidationError

from agentkit.config.summarization_config import ContextSize, SummarizationConfig


def test_fraction_percent_is_rejected() -> None:
    with pytest.raises(ValidationError, match="0.8"):
        ContextSize(type="fraction", value=80)


def test_non_finite_context_size_is_rejected() -> None:
    with pytest.raises(ValidationError, match="finite"):
        ContextSize(type="tokens", value=math.nan)
    with pytest.raises(ValidationError, match="finite"):
        ContextSize(type="fraction", value=math.inf)


def test_non_positive_tokens_are_rejected() -> None:
    with pytest.raises(ValidationError, match="positive"):
        ContextSize(type="tokens", value=0)
    with pytest.raises(ValidationError, match="positive"):
        ContextSize(type="tokens", value=-5)


def test_fractional_message_count_is_rejected() -> None:
    with pytest.raises(ValidationError, match="whole number"):
        ContextSize(type="messages", value=1.5)


def test_summarization_config_rejects_bad_trigger_at_load() -> None:
    with pytest.raises(ValidationError, match="0.8"):
        SummarizationConfig.model_validate({"trigger": [{"type": "fraction", "value": 80}]})
    with pytest.raises(ValidationError, match="whole number"):
        SummarizationConfig.model_validate({"keep": {"type": "messages", "value": 20.5}})


def test_valid_sizes_round_trip_to_tuples() -> None:
    assert ContextSize(type="fraction", value=0.8).to_tuple() == ("fraction", 0.8)
    assert ContextSize(type="messages", value=20).to_tuple() == ("messages", 20)
    assert SummarizationConfig().enabled is False
    assert SummarizationConfig().keep.to_tuple() == ("messages", 20)
    assert SummarizationConfig().trim_tokens_to_summarize == 4000
