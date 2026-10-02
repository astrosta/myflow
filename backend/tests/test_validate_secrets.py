"""context.secrets is a flat string map."""

import pytest

from backend.app.gateway.services import RunRequestInvalid, _validate_secrets


def test_validate_secrets_accepts_a_string_map_and_copies_it() -> None:
    raw = {"API_TOKEN": "secret"}
    cleaned = _validate_secrets("context", raw)
    assert cleaned == {"API_TOKEN": "secret"}
    cleaned["API_TOKEN"] = "changed"
    assert raw["API_TOKEN"] == "secret"


@pytest.mark.parametrize(
    "value",
    [None, ["API_TOKEN"], {"API_TOKEN": 1}, {"API_TOKEN": None}, {1: "secret"}],
)
def test_validate_secrets_rejects_anything_but_string_pairs(value) -> None:
    with pytest.raises(RunRequestInvalid, match=r"context\.secrets must be an object of strings"):
        _validate_secrets("context", value)
