"""Frozen course values on a Termin (WM-135).

A course's format, context, place and class/age stay editable after some of its Termine are
Abgeschlossen; a change applies only to the open Termine. Before it, the Abgeschlossen and Abgesagt
Termine keep the values they had: `frozen_at` and the `frozen_*` columns. Existing Termine get none
(they follow their course, as before), so no figure changes.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

REFERENCES = (("format", "formats"), ("context", "contexts"), ("place", "places"))
VALUES = (
    ("frozen_class_age_text", sa.Text()),
    ("frozen_class_level", sa.Integer()),
    ("frozen_age", sa.Integer()),
    ("frozen_age_band", sa.String(32)),
    ("frozen_target_group", sa.String(64)),
)


def upgrade() -> None:
    op.add_column("appointments", sa.Column("frozen_at", sa.DateTime(timezone=True)))
    for name, table in REFERENCES:
        column = f"frozen_{name}_id"
        op.add_column(
            "appointments",
            sa.Column(column, sa.Integer(), sa.ForeignKey(f"{table}.id", name=f"appointments_{column}_fkey")),
        )
        op.create_index(f"ix_appointments_{column}", "appointments", [column])
    for column, type_ in VALUES:
        op.add_column("appointments", sa.Column(column, type_))


def downgrade() -> None:
    for column, _ in reversed(VALUES):
        op.drop_column("appointments", column)
    for name, _ in reversed(REFERENCES):
        column = f"frozen_{name}_id"
        op.drop_index(f"ix_appointments_{column}", table_name="appointments")
        op.drop_column("appointments", column)
    op.drop_column("appointments", "frozen_at")
