"""Chain root. Table DDL is filled in when the ORM models land (§8.4).

Empty databases do not run ``upgrade()``; bootstrap stamps this revision after
``create_all``. ``upgrade()`` is only for a database already carrying an older
revision that this build understands.
"""

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    import agentkit.persistence.models  # noqa: F401
    from agentkit.persistence.base import Base

    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    from alembic import op

    import agentkit.persistence.models  # noqa: F401
    from agentkit.persistence.base import Base

    Base.metadata.drop_all(op.get_bind())
