"""Server-side login sessions (WM-102).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=False),
        sa.Column("admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("ended_by", sa.Text()),
    )
    op.create_index("ix_login_sessions_subject", "login_sessions", ["subject"])


def downgrade() -> None:
    op.drop_index("ix_login_sessions_subject", table_name="login_sessions")
    op.drop_table("login_sessions")
