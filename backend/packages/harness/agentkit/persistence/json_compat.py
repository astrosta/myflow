"""One JSON equality rule for memory and for SQLite / Postgres."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Boolean, literal
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.visitors import InternalTraversal

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
JsonScalar = None | bool | int | float | str
MISSING = object()


class InvalidMetadataFilterError(ValueError):
    """search metadata key or value cannot be compiled safely."""


def validate_metadata_filter(key: str, expected: JsonScalar) -> None:
    """Reject keys and values before they can reach a SQL path expression."""
    if not KEY_PATTERN.match(key):
        raise InvalidMetadataFilterError(f"unsafe metadata key: {key!r}")
    if expected is not None and not isinstance(expected, (bool, int, float, str)):
        raise InvalidMetadataFilterError(
            f"unsupported metadata value type: {type(expected).__name__}"
        )
    if isinstance(expected, int) and not isinstance(expected, bool) and not (INT64_MIN <= expected <= INT64_MAX):
        raise InvalidMetadataFilterError("integer metadata filters must fit in signed int64")


def python_value_matches(actual: Any, expected: JsonScalar) -> bool:
    """Same distinctions the SQL compilers enforce. ``MISSING`` is not JSON null."""
    if actual is MISSING:
        return False
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, int):
        return isinstance(actual, int) and not isinstance(actual, bool) and actual == expected
    if isinstance(expected, float):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual == expected
    return isinstance(actual, str) and actual == expected


class SqlJsonMatch(ColumnElement[bool]):
    inherit_cache = True
    type = Boolean()
    _traverse_internals = [
        ("column", InternalTraversal.dp_clauseelement),
        ("key", InternalTraversal.dp_string),
        ("expected", InternalTraversal.dp_plain_obj),
        ("value_type", InternalTraversal.dp_string),
    ]

    def __init__(self, column, key: str, expected: JsonScalar, *, value_type: str) -> None:
        super().__init__()
        self.column = column
        self.key = key
        self.expected = expected
        self.value_type = value_type


def json_value_matches(column, key: str, expected: JsonScalar) -> SqlJsonMatch:
    """Compile "metadata key equals expected" for both database dialects."""
    validate_metadata_filter(key, expected)
    value_type = "null" if expected is None else type(expected).__name__
    return SqlJsonMatch(column, key, expected, value_type=value_type)


def _sqlite_json_match(column_sql: str, key: str, expected: JsonScalar, compiler, **kw) -> str:
    type_sql = f"json_type({column_sql}, '$.{key}')"
    extract_sql = f"json_extract({column_sql}, '$.{key}')"
    if expected is None:
        return f"{type_sql} = 'null'"
    if isinstance(expected, bool):
        flag = "true" if expected else "false"
        return f"{type_sql} = '{flag}'"
    value_sql = compiler.process(literal(expected), **kw)
    if isinstance(expected, int):
        return f"{type_sql} = 'integer' AND CAST({extract_sql} AS INTEGER) = {value_sql}"
    if isinstance(expected, float):
        return f"{type_sql} IN ('integer', 'real') AND CAST({extract_sql} AS REAL) = {value_sql}"
    return f"{type_sql} = 'text' AND {extract_sql} = {value_sql}"


def _postgres_json_match(column_sql: str, key_sql: str, expected: JsonScalar, compiler, **kw) -> str:
    exists_sql = f"(CAST({column_sql} AS JSONB) ? {key_sql})"
    typeof_sql = f"json_typeof({column_sql} -> {key_sql})"
    text_sql = f"({column_sql} ->> {key_sql})"
    if expected is None:
        return f"{exists_sql} AND {typeof_sql} = 'null'"
    value_sql = compiler.process(literal(expected), **kw)
    if isinstance(expected, bool):
        return f"{exists_sql} AND {typeof_sql} = 'boolean' AND CAST({text_sql} AS BOOLEAN) = {value_sql}"
    if isinstance(expected, int):
        return (
            f"{exists_sql} AND {typeof_sql} = 'number' "
            f"AND {text_sql} ~ '^-?[0-9]+$' "
            f"AND CAST({text_sql} AS BIGINT) = {value_sql}"
        )
    if isinstance(expected, float):
        return f"{exists_sql} AND {typeof_sql} = 'number' AND CAST({text_sql} AS DOUBLE PRECISION) = {value_sql}"
    return f"{exists_sql} AND {typeof_sql} = 'string' AND {text_sql} = {value_sql}"


@compiles(SqlJsonMatch, "sqlite")
def _compile_sqlite_json_match(element: SqlJsonMatch, compiler, **kw) -> str:
    column_sql = compiler.process(element.column, **kw)
    return _sqlite_json_match(column_sql, element.key, element.expected, compiler, **kw)


@compiles(SqlJsonMatch, "postgresql")
def _compile_postgres_json_match(element: SqlJsonMatch, compiler, **kw) -> str:
    column_sql = compiler.process(element.column, **kw)
    key_sql = compiler.process(literal(element.key), **kw)
    return _postgres_json_match(column_sql, key_sql, element.expected, compiler, **kw)
