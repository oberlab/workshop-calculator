"""Sent Slack messages (WM-130).

`slack_messages` records each message of the bot (the DM after the course, the reminder the day
before, the weekly status) under a unique key before it is sent, so a restart of the scheduler never
sends one twice. The message text is not stored.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slack_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("key", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
            index=True,
        ),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error", sa.Text()),
        sa.Column("ts", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("slack_messages")
