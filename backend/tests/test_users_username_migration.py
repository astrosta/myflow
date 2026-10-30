"""M9 §8：`0003_users_username` 在**有数据**的库上回填 username。

新库走 `create_all`，压根不跑 `upgrade()`——所以这条 revision 只有在这里才被测到。
三件必须成立的事：回填不撞名、username 变 NOT NULL、**email 的 unique 索引还在**
（SQLite 上改列会重建表，顺手把别人的约束弄丢是这类迁移最典型的翻车方式）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from agentkit.persistence.bootstrap import _head_revision
from agentkit.persistence.migrations import __file__ as migrations_file

MIGRATIONS_DIR = Path(migrations_file).resolve().parent


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


#: `username` 之前那一版的 users 表，**手写**而不是靠 `0001` 的 create_all：
#: create_all 会用**当前**的 ORM 建表（已经带 username），那就不是被测的那条路径了。
_OLD_USERS_DDL = """
CREATE TABLE users (
    id VARCHAR(64) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255),
    system_role VARCHAR(16) DEFAULT 'user' NOT NULL,
    needs_setup BOOLEAN DEFAULT 0 NOT NULL,
    disabled BOOLEAN DEFAULT 0 NOT NULL,
    deleted_at DATETIME,
    token_version INTEGER DEFAULT '0' NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
)
"""


@pytest.fixture
def upgraded(tmp_path: Path):
    """一个停在 `0002_users_pat`、表是**旧形状**（没有 username）的库，塞四行用户。

    然后每个用例自己 `upgrade(cfg, "head")`——只有这一条路径才检验得到 `0003`。
    """
    db_path = tmp_path / "app.db"
    cfg = _config(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text(_OLD_USERS_DDL))
        conn.execute(text("CREATE UNIQUE INDEX ix_users_email ON users (email)"))
        conn.execute(text("CREATE INDEX ix_users_system_role ON users (system_role)"))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0002_users_pat')"))
        for user_id, email in (
            ("id-alice-1", "alice@example.com"),
            ("id-alice-2", "alice@other.com"),   # local part 撞车 → alice-2
            ("id-short-1", "a@b.com"),           # 清洗后只剩 1 个字符 → user-<id 前 8 位>
            ("id-dirty-1", "..Weird.Name..@x.com"),
        ):
            conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, system_role, needs_setup,"
                    " disabled, token_version, created_at, updated_at)"
                    " VALUES (:id, :email, 'hash', 'user', 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": user_id, "email": email},
            )
    try:
        yield engine, cfg, db_path
    finally:
        engine.dispose()


def test_upgrading_a_database_that_is_already_at_the_new_shape(tmp_path: Path) -> None:
    """**"库被 stamp 在旧号、表却已经是最新形状"是正常状态。**

    `0001_baseline` 跑 `Base.metadata.create_all`，而这个进程只要 import 过
    `backend.app.gateway.auth.models`，metadata 里就带着 `users`（连 username 一起）。
    升级如果再无条件 `CREATE TABLE users` / `ADD COLUMN username`，这种库就永远起不来——
    而它恰恰是"auth 代码上线后重启一次"最常见的形态。
    """
    import backend.app.gateway.auth.models  # noqa: F401 —— 让 Base.metadata 带上 users
    from agentkit.persistence.base import Base

    db_path = tmp_path / "adopted.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)          # 表已经是最新形状
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0001_baseline')"))
        conn.execute(
            text(
                "INSERT INTO users (id, email, username, system_role, needs_setup, disabled,"
                " token_version, created_at, updated_at)"
                " VALUES ('u1', 'a@b.com', 'alice', 'admin', 0, 0, 0,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(_config(db_path), "head")

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT username FROM users WHERE id = 'u1'")) == "alice"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _head_revision()
        indexes = {row[1] for row in conn.execute(text("PRAGMA index_list(users)"))}
    assert "ix_users_username" in indexes
    engine.dispose()


def test_usernames_are_backfilled_without_collisions(upgraded) -> None:
    engine, cfg, _path = upgraded

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, username FROM users")).fetchall())

    assert rows["id-alice-1"] == "alice"
    assert rows["id-alice-2"] == "alice-2"          # 撞名加后缀
    assert rows["id-short-1"] == "user-id-short"    # 太短 → 用 id 前 8 位
    assert rows["id-dirty-1"] == "weird.name"       # 首尾的点和连字符被剥掉
    assert len(set(rows.values())) == len(rows)


def test_the_column_becomes_not_null(upgraded) -> None:
    engine, cfg, _path = upgraded

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        columns = {row[1]: row for row in conn.execute(text("PRAGMA table_info(users)"))}
    username = columns["username"]
    assert username[3] == 1, "username 必须 NOT NULL（槽位 3 是 notnull 标志）"


def test_email_becomes_nullable_and_new_users_need_no_address(upgraded) -> None:
    engine, cfg, _path = upgraded

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        columns = {row[1]: row for row in conn.execute(text("PRAGMA table_info(users)"))}
    assert columns["email"][3] == 0, "email must be nullable after 0010"

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, username, system_role, needs_setup, disabled,"
                " token_version, created_at, updated_at)"
                " VALUES ('id-no-email', NULL, 'no-email', 'user', 0, 0, 0,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )


def test_the_unique_constraints_survive_the_table_rebuild(upgraded) -> None:
    engine, cfg, _path = upgraded

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        indexes = {
            row[1]: bool(row[2]) for row in conn.execute(text("PRAGMA index_list(users)"))
        }
    assert indexes.get("ix_users_username") is True
    assert indexes.get("ix_users_email") is True, "email 的 unique 索引不能被重建弄丢"
    assert indexes.get("ix_users_system_role") is False

    with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO users (id, email, username, system_role, needs_setup, disabled,"
                    " token_version, created_at, updated_at)"
                    " VALUES ('id-dup-email', 'alice@example.com', 'fresh', 'user', 0, 0, 0,"
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )


def test_downgrade_drops_the_column_again(upgraded) -> None:
    engine, cfg, _path = upgraded
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0002_users_pat")

    with engine.connect() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(users)"))]
    assert "username" not in columns
