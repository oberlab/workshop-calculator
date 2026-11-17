"""Absences and the Kimai user of a person (WM-142).

`absences`: one row per Kimai absence and day, written only by the Kimai job; an absence that
disappears from Kimai is marked `withdrawn_at`, never deleted. `people.kimai_user_id`: the Kimai
user an admin assigned by hand, for people whose e-mail in Kimai differs from the app's.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("people", sa.Column("kimai_user_id", sa.Integer()))
    op.create_unique_constraint("people_kimai_user_id_key", "people", ["kimai_user_id"])
    op.create_table(
        "absences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("half_day", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("kimai_key", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("kimai_key", "day", name="uq_absences_kimai_key_day"),
    )
    op.create_index("ix_absences_person_day", "absences", ["person_id", "day"])


def downgrade() -> None:
    op.drop_index("ix_absences_person_day", table_name="absences")
    op.drop_table("absences")
    op.drop_constraint("people_kimai_user_id_key", "people", type_="unique")
    op.drop_column("people", "kimai_user_id")
