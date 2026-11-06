import pytest

from backend.app.gateway.tools_wire import (
    MASK,
    InvalidToolHeader,
    mask_headers,
    restore_masked_headers,
)


def test_masks_only_sensitive_headers_case_insensitively() -> None:
    assert mask_headers({"authorization": "Bearer x", "X-Env": "prod"}) == {
        "authorization": MASK,
        "X-Env": "prod",
    }


def test_restore_replaces_mask_with_stored_value() -> None:
    original = {"Authorization": "Bearer x", "X-Env": "prod"}
    masked = mask_headers(original)
    assert restore_masked_headers(masked, original) == original


def test_restore_rejects_mask_without_stored_value() -> None:
    with pytest.raises(InvalidToolHeader):
        restore_masked_headers({"Authorization": MASK}, {})
