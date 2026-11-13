"""Stored hints per Termin (WM-113).

`hints` holds the findings of the hint rules (`metrics.hints`) as last computed: recomputed after
each change and nightly by the scheduler. Derived data only; accepted warnings stay in
`hint_decisions`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("rule", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "other_appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            index=True,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hints_rule_level", "hints", ["rule", "level"])


def downgrade() -> None:
    op.drop_index("ix_hints_rule_level", "hints")
    op.drop_table("hints")
