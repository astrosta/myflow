"""Add knowledge base and document metadata tables.

Revision ID: 0004_knowledge_base
Revises: 0003_users_username
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_knowledge_base"
down_revision = "0003_users_username"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("knowledge_bases"):
        op.create_table(
            "knowledge_bases",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("type", sa.String(length=24), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("document_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("chunk_count", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "name", name="uq_knowledge_bases_user_name"),
        )
        op.create_index("ix_knowledge_bases_user_id", "knowledge_bases", ["user_id"])
        op.create_index(
            "ix_knowledge_bases_user_updated", "knowledge_bases", ["user_id", "updated_at"]
        )

    if not _has_table("knowledge_documents"):
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("knowledge_base_id", sa.String(length=64), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("stored_name", sa.String(length=255), nullable=False),
            sa.Column("mime_type", sa.String(length=128), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
            ),
        )
        op.create_index("ix_knowledge_documents_user_id", "knowledge_documents", ["user_id"])
        op.create_index(
            "ix_knowledge_documents_knowledge_base_id", "knowledge_documents", ["knowledge_base_id"]
        )
        op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])
        op.create_index(
            "ix_knowledge_documents_base_updated",
            "knowledge_documents",
            ["user_id", "knowledge_base_id", "updated_at"],
        )


def downgrade() -> None:
    if _has_table("knowledge_documents"):
        op.drop_table("knowledge_documents")
    if _has_table("knowledge_bases"):
        op.drop_table("knowledge_bases")
