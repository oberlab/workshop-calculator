"""Instructors: names and profile pictures.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instructors",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("image", sa.LargeBinary()),
        sa.Column("image_type", sa.String(64)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("instructors")
