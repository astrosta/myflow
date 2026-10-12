"""M9 §2.2 原始 identity → 存储 ID：`make_safe_user_id` / `validate_user_id`。

存储 ID 是唯一允许进文件路径的形状。已经安全的 id 必须原样通过（否则等于给老用户
换桶 = 数据"消失"）；不安全的 identity 换字符 + 16 位 digest，**靠 digest 防撞，
不靠截断**。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from agentkit.utils.ids import make_safe_user_id, validate_user_id

_SAFE_SHAPE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST_SUFFIX = re.compile(r"-[0-9a-f]{16}$")

_ALREADY_SAFE = ("u1", "abc-123_XYZ", "default", "A1_b-2", "0", "-", "_")
_UNSAFE = ("a/b", "a b", "..", "Alice@Example.com", "a\\b", "café", "", "a\nb", "..\\..")

_HOSTILE = ("../etc/passwd", "../../..", "a\\b", "user@例.com", "a/b/c", "..", "a" * 400, " ", "\t\n")

_DISTINCT_PAIRS = (
    ("a/b", "a-b"),  # 斜杠被换成 `-`：两个输入清洗后同名，只能靠 digest 区分
    ("..", "."),
    ("../x", "..-x"),
    ("a b", "a-b"),
    ("Alice@Example.com", "alice@example.com"),  # 大小写是不同 identity
    ("a" * 200, "a" * 201),  # 超长输入不靠截断
    ("x" * 300 + "@a.com", "x" * 300 + "@b.com"),
)


@pytest.mark.parametrize("raw", _ALREADY_SAFE)
def test_already_safe_ids_pass_through_unchanged(raw: str) -> None:
    assert make_safe_user_id(raw) == raw
    assert validate_user_id(raw) == raw


def test_already_safe_ids_are_not_rebucketed() -> None:
    raw = "abc-123_XYZ"
    assert make_safe_user_id(raw) is raw


def test_email_becomes_a_directory_safe_storage_id() -> None:
    raw = "Alice@Example.com"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    safe = make_safe_user_id(raw)

    assert safe == f"Alice-Example-com-{digest}"
    assert _DIGEST_SUFFIX.search(safe)
    assert "-" in safe
    assert _SAFE_SHAPE.fullmatch(safe)
    assert validate_user_id(safe) == safe
    assert safe != raw


def test_storage_id_is_stable_and_reproducible() -> None:
    raw = "Alice@Example.com"
    first = make_safe_user_id(raw)
    assert [make_safe_user_id(raw) for _ in range(3)] == [first, first, first]


@pytest.mark.parametrize(("left", "right"), _DISTINCT_PAIRS)
def test_different_identities_never_share_a_bucket(left: str, right: str) -> None:
    left_id = make_safe_user_id(left)
    right_id = make_safe_user_id(right)

    assert left_id != right_id
    assert _SAFE_SHAPE.fullmatch(left_id)
    assert _SAFE_SHAPE.fullmatch(right_id)


@pytest.mark.parametrize("raw", _HOSTILE)
def test_every_storage_id_is_accepted_by_validate_user_id(raw: str) -> None:
    safe = make_safe_user_id(raw)
    assert validate_user_id(safe) == safe


@pytest.mark.parametrize("raw", _HOSTILE)
def test_storage_id_cannot_climb_out_of_the_users_directory(raw: str) -> None:
    safe = make_safe_user_id(raw)
    assert "/" not in safe and "\\" not in safe
    bucket = (Path("/base/users") / safe).resolve()
    assert bucket.parent == Path("/base/users").resolve()


@pytest.mark.parametrize("bad", ["", 0, 123, None, b"u1", ["u1"], {"user_id": "u1"}])
def test_non_empty_string_is_required(bad: object) -> None:
    with pytest.raises(ValueError, match=re.escape("user_id must be a non-empty string.")):
        make_safe_user_id(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("unsafe", _UNSAFE)
def test_validate_user_id_rejects_unsafe_ids(unsafe: str) -> None:
    with pytest.raises(ValueError, match=r"^Invalid user ID"):
        validate_user_id(unsafe)
