"""M9 §8: `users` + `personal_access_tokens`.

两个刻意的决定：

- **DDL 写死在这里**，不用 `Base.metadata.create_all`。users 表住在 app 层
  （`backend.app.gateway.auth.models`），而 harness 不允许 import app
  （`backend/tests/test_harness_banned_prefixs_import.py` 盯着这条）。
  迁移脚本本来就该是"那一刻的 DDL"，不该随 ORM 漂移。
- **`system_role` 上没有唯一索引**。§2.7 提过"admin 部分唯一索引"，
  但 UNIQUE(system_role) 会禁止第二个 admin，而 §2.13 明确要求
  "不能把最后一个 admin 降级"——那前提就是可以有多个 admin。
  并发初始化改用条件插入（`INSERT ... SELECT ... WHERE NOT EXISTS (admin)`），
  见 `SqlUserStore.create_first_admin`。

Revision ID: 0002_users_pat
Revises: 0001_baseline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_users_pat"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_index(table: str, name: str) -> bool:
    return any(index["name"] == name for index in sa.inspect(op.get_bind()).get_indexes(table))


def _create_index_if_missing(name: str, table: str, columns: list[str], *, unique: bool) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    """**可重入**：表已经存在就跳过。

    为什么必须这样写：`0001_baseline` 跑的是 `Base.metadata.create_all`，而这个进程
    只要 import 过 `backend.app.gateway.auth.models`，metadata 里就**已经有 users**。
    于是"库被 stamp 在 0001、但表已经建好"是完全正常的一种状态——
    这时候再来一次 `CREATE TABLE users` 会让升级直接失败，而那个库正是
    "auth 代码上线之后重启一次"的库，也就是最不该在这里翻车的那种。
    """
    if _has_table("users") and _has_table("personal_access_tokens"):
        return

    if _has_table("users"):
        # 只有 PAT 表缺：users 是 create_all 建的，索引也一起建好了
        _create_pat_table()
        return

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("system_role", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("needs_setup", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_index_if_missing("ix_users_email", "users", ["email"], unique=True)
    _create_index_if_missing("ix_users_system_role", "users", ["system_role"], unique=False)

    _create_pat_table()


def _create_pat_table() -> None:
    if _has_table("personal_access_tokens"):
        return
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        # 只存 sha256 digest：明文只在创建响应里出现一次（§2.15）
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        # 与 users.token_version 对比：改密 / 禁用 / 软删都会让旧 PAT 一起失效
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_index_if_missing(
        "ix_personal_access_tokens_user_id", "personal_access_tokens", ["user_id"], unique=False
    )
    _create_index_if_missing(
        "ix_personal_access_tokens_token_hash", "personal_access_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    if _has_table("personal_access_tokens"):
        op.drop_index("ix_personal_access_tokens_token_hash", table_name="personal_access_tokens")
        op.drop_index("ix_personal_access_tokens_user_id", table_name="personal_access_tokens")
        op.drop_table("personal_access_tokens")
    if _has_table("users"):
        for name in ("ix_users_username", "ix_users_system_role", "ix_users_email"):
            if _has_index("users", name):
                op.drop_index(name, table_name="users")
        op.drop_table("users")
