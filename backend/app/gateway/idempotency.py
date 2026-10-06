"""Idempotency-Key shape check. Hashing and scope stay in start_run."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Header

_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]{1,255}$")


def parse_idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> str | None:
    if key is None:
        return None
    key = key.strip()
    if not _VISIBLE_ASCII.match(key):
        from backend.app.gateway.services import RunRequestInvalid

        raise RunRequestInvalid("Idempotency-Key must be 1..255 visible ASCII characters")
    return key
