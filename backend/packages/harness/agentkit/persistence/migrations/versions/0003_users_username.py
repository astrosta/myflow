"""M9 §2.4 / §8：给已有的 `users` 补 `username`（第二个登录标识）。

**顺序不能反**：加可空列 → 回填 → 建 unique 索引 → 置 NOT NULL。
反过来（先 NOT NULL 或先建索引）在有数据的库上会直接失败——而"有数据的库"
正是这条 revision 存在的理由：新库走 `create_all`，根本不跑 `upgrade()`。

回填规则（§8）：

1. 从邮箱 local part 派生（`alice@example.com` → `alice`），按 §2.4 清洗；
2. 撞名加数字后缀（`alice-2`…），仍然撞就退回 `user-<id 前 8 位>`；
3. 清洗后不足 3 字符的同样走第 2 条。

Revision ID: 0003_users_username
Revises: 0002_users_pat
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0003_users_username"
down_revision = "0002_users_pat"
branch_labels = None
depends_on = None

USERNAME_MIN = 3
USERNAME_MAX = 32
_UNSAFE = re.compile(r"[^a-z0-9_.-]")
_EDGE = re.compile(r"^[^a-z0-9]+|[^a-z0-9]+$")


def _sanitize(raw: str) -> str:
    value = _UNSAFE.sub("-", (raw or "").strip().lower())
    return _EDGE.sub("", value)


def _derive(email: str | None, user_id: str, taken: set[str]) -> str:
    local = (email or "").split("@", 1)[0]
    base = _sanitize(local)
    if len(base) < USERNAME_MIN or len(base) > USERNAME_MAX:
        base = f"user-{_sanitize(user_id)[:8] or 'unknown'}"
    candidate = base
    suffix = 1
    while candidate in taken:
        suffix += 1
        tail = f"-{suffix}"
        candidate = f"{base[: USERNAME_MAX - len(tail)]}{tail}"
    return candidate


def _backfill(conn) -> None:
    """逐行在 Python 里算：纯 SQL 做不出"撞名加后缀"这种逻辑，而账号表很小。"""
    rows = conn.execute(sa.text("SELECT id, email FROM users ORDER BY created_at, id")).fetchall()
    taken: set[str] = set()
    for row in rows:
        username = _derive(row.email, row.id, taken)
        taken.add(username)
        conn.execute(
            sa.text("UPDATE users SET username = :username WHERE id = :id"),
            {"username": username, "id": row.id},
        )


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}


def _has_index(name: str) -> bool:
    return any(
        index["name"] == name for index in sa.inspect(op.get_bind()).get_indexes("users")
    )


def upgrade() -> None:
    """**可重入**：列已经存在（`create_all` 建的）就只补索引。

    与 0002 同一个理由：`0001_baseline` 的 `create_all` 会连着 app 的 `users` 一起建，
    所以"表已经是最新形状、revision 却停在旧号"是正常状态。
    """
    if "username" in _columns():
        if not _has_index("ix_users_username"):
            op.create_index("ix_users_username", "users", ["username"], unique=True)
        return

    op.add_column("users", sa.Column("username", sa.String(length=32), nullable=True))
    _backfill(op.get_bind())
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    # 置 NOT NULL。SQLite 不支持 ALTER COLUMN，batch 会重建表——
    # 重建会**带走反射到的索引**（email / username / system_role 都在），
    # 所以这里不要再显式 create_index 一次，否则在重建之后撞名。
    # `test_users_username_migration.py` 专门断言重建之后三条索引都还在。
    with op.batch_alter_table("users", recreate="always") as batch:
        batch.alter_column("username", existing_type=sa.String(length=32), nullable=False)


def downgrade() -> None:
    if "username" not in _columns():
        return
    # 先把 username 上的索引摘掉，再重建表：让 batch 去处理一个指向已删列的索引
    # 是自找麻烦（不同后端的行为不一致）。
    if _has_index("ix_users_username"):
        op.drop_index("ix_users_username", table_name="users")
    with op.batch_alter_table("users", recreate="always") as batch:
        batch.drop_column("username")
