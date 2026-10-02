"""Thread rows do not keep run-scoped identity or trace keys."""

from agentkit.runtime.runs.worker import _RESERVED_METADATA_KEYS
from agentkit.trace_context import TRACE_METADATA_KEY


def test_reserved_metadata_keys_cover_identity_and_trace() -> None:
    metadata = {
        "topic": "中文",
        "user_id": "alice",
        "owner_id": "alice",
        "auth_user": "alice",
        "auth_user_id": "alice",
        TRACE_METADATA_KEY: "trace-1",
    }
    kept = {key: value for key, value in metadata.items() if key not in _RESERVED_METADATA_KEYS}
    assert kept == {"topic": "中文"}
