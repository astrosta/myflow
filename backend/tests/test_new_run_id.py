"""Run ids are allocated before admission so config and the row share one value."""

import re

from agentkit.utils.ids import new_run_id

_RUN_ID = re.compile(r"^[0-9a-f]{32}$")


def test_new_run_id_is_32_hex_and_unique() -> None:
    first, second = new_run_id(), new_run_id()
    assert _RUN_ID.fullmatch(first)
    assert _RUN_ID.fullmatch(second)
    assert first != second
    assert len(first) <= 64
