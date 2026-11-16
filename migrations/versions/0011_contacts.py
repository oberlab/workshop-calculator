"""Contact persons at places (WM-141): `contacts` and `courses.contact_id`.

A Kontaktperson (name, role, e-mail, phone) belongs to a place and is chosen on a course. It is
planning data about people outside the team: no chart, key figure or export reads it. Deleting a
contact on request removes its row; `courses.contact_id` is cleared by the foreign key
(ON DELETE SET NULL) as a safety net, although `metrics.contacts.delete` clears it itself and records
it in the Verlauf.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("place_id", sa.Integer(), sa.ForeignKey("places.id"), nullable=False, index=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "courses",
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", name="fk_courses_contact_id", ondelete="SET NULL"),
        ),
    )
    op.create_index("ix_courses_contact_id", "courses", ["contact_id"])


def downgrade() -> None:
    op.drop_index("ix_courses_contact_id", table_name="courses")
    op.drop_column("courses", "contact_id")
    op.drop_table("contacts")
