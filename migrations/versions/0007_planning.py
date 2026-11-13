"""Planning on the new model (WM-110): alternative dates, cancellation reason, calendar state.

- `appointments.cancel_reason`: why a Termin was cancelled (only Bestätigt Termine can be).
- `appointments.calendar_state`, `calendar_checked_at`: what the last calendar check found for the
  event linked by `calendar_uid` (ok, missing, outdated).
- `appointment_alternatives`: further possible dates of a Geplant Termin; "Bestätigen" picks one.
- `calendar_responses`: the answers (PARTSTAT) of the invited lead and support, read back from the
  calendar.
- `calendar_orphans`: future events in the course calendar without a Termin, as seen by the last
  check (bookkeeping of the check; an admin creates a course from one or ignores it).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hints", sa.Column("key", sa.Text()))
    op.execute(
        "UPDATE hints SET key = CASE WHEN other_appointment_id IS NOT NULL"
        " THEN 'termin:' || other_appointment_id ELSE text END"
    )
    op.add_column("hint_decisions", sa.Column("finding_key", sa.Text()))
    op.add_column("appointments", sa.Column("cancel_reason", sa.Text()))
    op.add_column("appointments", sa.Column("calendar_state", sa.String(16)))
    op.add_column("appointments", sa.Column("calendar_checked_at", sa.DateTime(timezone=True)))
    op.create_table(
        "appointment_alternatives",
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("date", sa.Date(), primary_key=True),
    )
    op.create_table(
        "calendar_responses",
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("email", sa.Text(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id"), index=True),
        sa.Column("partstat", sa.String(32), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "calendar_orphans",
        sa.Column("uid", sa.String(255), primary_key=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("location", sa.Text()),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ignored_by", sa.Text()),
        sa.Column("ignored_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("calendar_orphans")
    op.drop_table("calendar_responses")
    op.drop_table("appointment_alternatives")
    op.drop_column("appointments", "calendar_checked_at")
    op.drop_column("appointments", "calendar_state")
    op.drop_column("appointments", "cancel_reason")
    op.drop_column("hint_decisions", "finding_key")
    op.drop_column("hints", "key")
