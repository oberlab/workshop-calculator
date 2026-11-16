"""Equipment and what formats and Termine need of it (WM-140).

`equipment` is the Material list: critical equipment with the number available, a list with proposals
and admin decisions like places or formats. `format_equipment` holds what a format usually needs (a
new Termin copies it), `appointment_equipment` what a Termin needs. Nothing existing changes, so no
chart or key figure moves.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "equipment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("quantity", sa.Integer()),
        sa.Column("note", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="bestätigt"),
        sa.Column("merged_into_id", sa.Integer(), sa.ForeignKey("equipment.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('vorgeschlagen', 'bestätigt', 'abgelehnt', 'zusammengeführt')",
            name="ck_equipment_status",
        ),
        sa.CheckConstraint(
            "(status = 'zusammengeführt') = (merged_into_id IS NOT NULL)", name="ck_equipment_merged"
        ),
    )
    for table, owner in (("format_equipment", "formats"), ("appointment_equipment", "appointments")):
        key = f"{owner[:-1]}_id"
        op.create_table(
            table,
            sa.Column(key, sa.Integer(), sa.ForeignKey(f"{owner}.id", ondelete="CASCADE"), primary_key=True),
            sa.Column(
                "equipment_id", sa.Integer(), sa.ForeignKey("equipment.id"), primary_key=True, index=True
            ),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.CheckConstraint("quantity > 0", name=f"ck_{table}_quantity"),
        )


def downgrade() -> None:
    op.drop_table("appointment_equipment")
    op.drop_table("format_equipment")
    op.drop_table("equipment")
