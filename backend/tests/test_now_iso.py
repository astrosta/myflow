"""In-memory run timestamps use the same UTC text as rows read back from SQL."""

from datetime import datetime

from agentkit.persistence.base import now_iso


def test_now_iso_is_utc_zulu_and_parseable() -> None:
    stamped = now_iso()
    parsed = datetime.fromisoformat(stamped.replace("Z", "+00:00"))
    assert stamped.endswith("Z")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
