"""0011_user_isolated_skills: preserve private data, drop public/review state."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from agentkit.persistence.migrations import __file__ as migrations_file

MIGRATIONS_DIR = Path(migrations_file).resolve().parent


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


_OLD_SCHEMA = """
CREATE TABLE skills (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    owner_user_id VARCHAR(64) NOT NULL,
    private_name VARCHAR(128) NOT NULL,
    public_name VARCHAR(128),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME
);
CREATE UNIQUE INDEX uq_skills_public_name ON skills (public_name) WHERE public_name IS NOT NULL;
CREATE TABLE skill_versions (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    skill_id VARCHAR(64) NOT NULL REFERENCES skills(id),
    skill_version VARCHAR(32) NOT NULL,
    version_seq INTEGER NOT NULL,
    package_sha256 VARCHAR(64) NOT NULL,
    skill_md_sha256 VARCHAR(64) NOT NULL,
    metadata_json JSON NOT NULL,
    draft_ref VARCHAR(255),
    draft_commit VARCHAR(40),
    skill_path VARCHAR(1024) NOT NULL,
    index_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL
);
CREATE TABLE skill_private_active (
    skill_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES skills(id),
    owner_user_id VARCHAR(64) NOT NULL,
    version_id VARCHAR(64) NOT NULL REFERENCES skill_versions(id),
    updated_at DATETIME NOT NULL
);
CREATE TABLE skill_draft_files (
    skill_id VARCHAR(64) NOT NULL REFERENCES skills(id),
    path VARCHAR(1024) NOT NULL,
    content BLOB NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    size_bytes INTEGER NOT NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'text',
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (skill_id, path)
);
CREATE TABLE skill_index_outbox (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    op VARCHAR(16) NOT NULL,
    projection_key VARCHAR(255) NOT NULL,
    scope VARCHAR(16) NOT NULL,
    owner_user_id VARCHAR(64),
    skill_id VARCHAR(64) NOT NULL,
    version_id VARCHAR(64),
    payload_json JSON NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at DATETIME NOT NULL,
    last_error TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE TABLE skill_audit_events (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    actor VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    skill_id VARCHAR(64),
    submission_id VARCHAR(64),
    release_id VARCHAR(64),
    detail_json JSON NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE skill_catalog_epoch (
    id TEXT NOT NULL PRIMARY KEY,
    epoch INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL
);
CREATE TABLE skill_submissions (id VARCHAR(64) NOT NULL PRIMARY KEY);
CREATE TABLE skill_release_batches (id VARCHAR(64) NOT NULL PRIMARY KEY);
CREATE TABLE skill_releases (id VARCHAR(64) NOT NULL PRIMARY KEY);
CREATE TABLE skill_public_active (skill_id VARCHAR(64) NOT NULL PRIMARY KEY);
CREATE TABLE skill_settings (skill_id VARCHAR(64) NOT NULL PRIMARY KEY);
"""


def test_migration_preserves_private_rows_and_drops_public_state(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        for statement in _OLD_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(
            text(
                "INSERT INTO alembic_version (version_num) VALUES ('0010_users_email_optional')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO skills (id, owner_user_id, private_name, public_name,"
                " created_at, updated_at) VALUES ('s1', 'u1', 'pdf-tools', 'old-public',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO skill_versions (id, skill_id, skill_version, version_seq,"
                " package_sha256, skill_md_sha256, metadata_json, skill_path, created_at)"
                " VALUES ('v1', 's1', 'v1', 1, 'a', 'b', '{}',"
                " 'users/u1/s1/SKILL.md', CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(_config(db_path), "head")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "skill_security_scans" in tables
    assert "skill_private_active" in tables
    for removed in (
        "skill_submissions",
        "skill_release_batches",
        "skill_releases",
        "skill_public_active",
        "skill_settings",
    ):
        assert removed not in tables
    # SQLite keeps the legacy nullable column because rebuilding a referenced
    # table is unsafe with foreign keys enabled; no ORM/store path reads it.
    assert "public_name" in {column["name"] for column in inspector.get_columns("skills")}
    skill_columns = {column["name"] for column in inspector.get_columns("skills")}
    assert {"source", "source_uri", "enabled", "load_count"} <= skill_columns
    version_columns = {
        column["name"] for column in inspector.get_columns("skill_versions")
    }
    assert {"index_attempts", "index_last_error", "index_next_attempt_at"} <= version_columns
    assert "ix_skill_versions_index_status" in {
        index["name"] for index in inspector.get_indexes("skill_versions")
    }

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT private_name FROM skills WHERE id = 's1'")) == "pdf-tools"
        assert conn.scalar(text("SELECT source FROM skills WHERE id = 's1'")) == "manual"
        assert conn.scalar(text("SELECT enabled FROM skills WHERE id = 's1'")) in (True, 1)
        assert conn.scalar(text("SELECT skill_path FROM skill_versions WHERE id = 'v1'")) == (
            "users/u1/s1/SKILL.md"
        )
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0015_skill_index_repair"
        )
        assert conn.scalar(
            text("SELECT index_attempts FROM skill_versions WHERE id = 'v1'")
        ) in (0, "0")
    engine.dispose()
