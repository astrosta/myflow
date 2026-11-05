"""Skill catalog tables, partial uniques, PAT scope, and config defaults."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from agentkit.config.skills_config import SkillsConfig
from agentkit.persistence.schema import Base
from backend.app.gateway.auth.models import PAT_SCOPE_MENU

EXPECTED = {
    "skills",
    "skill_versions",
    "skill_private_active",
    "skill_security_scans",
    "skill_audit_events",
    "skill_draft_files",
    "skill_index_outbox",
    "skill_catalog_epoch",
}
REMOVED = {
    "skill_submissions",
    "skill_release_batches",
    "skill_releases",
    "skill_public_active",
    "skill_settings",
}
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_only_user_private_skill_tables_are_registered():
    tables = set(Base.metadata.tables)
    assert EXPECTED <= tables
    assert not (REMOVED & tables)
    assert "skills:write" in PAT_SCOPE_MENU


def test_skills_config_has_no_review_or_public_branch_defaults():
    config = SkillsConfig()

    assert config.source == "filesystem"
    assert config.creation.enabled is True
    assert config.creation.max_drafts_per_user == 20
    assert config.creation.max_package_bytes == 5_242_880
    assert config.creation.max_file_bytes == 1_048_576
    assert config.creation.max_files == 200
    assert config.git.repository == ""
    assert not hasattr(config.git, "public_branch")
    assert config.git.draft_ref_prefix == "refs/heads/myflow/drafts"
    assert config.git.cache_dir == ".myflow/skills"
    assert config.git.ssh_key == ""
    assert config.git.command_timeout_seconds == 120
    assert not hasattr(config, "review")
    assert config.index.outbox_poll_seconds == 2.0
    assert config.index.catalog_poll_seconds == 5.0
    assert config.index.reconciliation_interval_minutes == 10
    assert config.retention.keep_versions_per_skill == 20
    assert config.retention.cache_ttl_days == 30

    with pytest.raises(ValidationError, match="base64"):
        SkillsConfig(git={"ssh_key": "not-a-key"})


def _seed_skill_and_version(connection) -> None:
    connection.execute(
        Base.metadata.tables["skills"].insert().values(
            id="skill-1",
            owner_user_id="user-1",
            private_name="alpha",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    connection.execute(
        Base.metadata.tables["skill_versions"].insert().values(
            id="version-1",
            skill_id="skill-1",
            skill_version="v1",
            version_seq=1,
            package_sha256="a" * 64,
            skill_md_sha256="b" * 64,
            metadata_json={},
            skill_path="users/user-1/skill-1/SKILL.md",
            created_at=NOW,
        )
    )


def test_private_name_is_unique_per_owner_while_not_deleted(engine):
    with engine.begin() as connection:
        connection.execute(
            Base.metadata.tables["skills"].insert().values(
                id="skill-1",
                owner_user_id="user-1",
                private_name="alpha",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                Base.metadata.tables["skills"].insert().values(
                    id="skill-2",
                    owner_user_id="user-1",
                    private_name="alpha",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

    # Another owner may use the same name; there is no global public namespace.
    with engine.begin() as connection:
        connection.execute(
            Base.metadata.tables["skills"].insert().values(
                id="skill-3",
                owner_user_id="user-2",
                private_name="alpha",
                created_at=NOW,
                updated_at=NOW,
            )
        )


def test_one_active_pointer_per_skill_and_scan_fk(engine):
    with engine.begin() as connection:
        _seed_skill_and_version(connection)
        connection.execute(
            Base.metadata.tables["skill_private_active"].insert().values(
                skill_id="skill-1",
                owner_user_id="user-1",
                version_id="version-1",
                updated_at=NOW,
            )
        )
        connection.execute(
            Base.metadata.tables["skill_security_scans"].insert().values(
                id="scan-1",
                skill_id="skill-1",
                version_id="version-1",
                owner_user_id="user-1",
                package_sha256="a" * 64,
                status="completed",
                is_safe=True,
                max_severity="SAFE",
                findings_count=0,
                findings_json=[],
                scanner="cisco-ai-skill-scanner",
                scanner_version="2.3.2",
                duration_ms=1,
                created_at=NOW,
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                Base.metadata.tables["skill_private_active"].insert().values(
                    skill_id="skill-1",
                    owner_user_id="user-1",
                    version_id="version-1",
                    updated_at=NOW,
                )
            )
