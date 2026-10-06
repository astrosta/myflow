"""Shared list parameters and the one page envelope."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Query, Request

from backend.app.gateway.models.request import JsonScalar
from backend.app.gateway.models.response import Page

DEFAULT_LIMIT, MAX_LIMIT = 50, 200


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int
    cursor: str | None
    order: Literal["asc", "desc"]

    @property
    def ascending(self) -> bool:
        return self.order == "asc"


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
) -> PageParams:
    return PageParams(limit=limit, cursor=cursor, order=order)


def page_response[T](items: Sequence[T], *, has_more: bool, next_cursor: str | None) -> Page[T]:
    """The cursor is omitted on the last page."""
    return Page(data=list(items), has_more=has_more, next_cursor=next_cursor if has_more else None)


@dataclass(frozen=True, slots=True)
class MessagePageParams:
    limit: int
    before_seq: int | None
    after_seq: int | None


def message_page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
) -> MessagePageParams:
    if before_seq is not None and after_seq is not None:
        from backend.app.gateway.services import RunRequestInvalid

        raise RunRequestInvalid("before_seq and after_seq are mutually exclusive")
    return MessagePageParams(limit=limit, before_seq=before_seq, after_seq=after_seq)


def trim_message_page(rows: list[dict], *, limit: int, after_seq: int | None) -> tuple[list[dict], bool]:
    """Store rows are ascending. The extra row is at the head when paging backward, at the tail when paging forward."""
    has_more = len(rows) > limit
    if not has_more:
        return rows, False
    if after_seq is not None:
        return rows[:limit], True
    return rows[-limit:], True


def metadata_filters(request: Request) -> dict[str, JsonScalar]:
    """Read metadata.<key>=<scalar>. Query values are strings; types are restored here."""
    from backend.app.gateway.services import RunRequestInvalid

    found: dict[str, JsonScalar] = {}
    for key, raw in request.query_params.multi_items():
        if not key.startswith("metadata."):
            continue
        name = key.removeprefix("metadata.")
        if not name or name in found:
            raise RunRequestInvalid(f"Invalid metadata filter: {key}")
        found[name] = _parse_metadata_scalar(raw)
    return found


def _parse_metadata_scalar(raw: str) -> JsonScalar:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    return raw
