"""Core team flag on people (WM-131 follow-up).

The Team page and "Wer ist frei?" show the core team plus anyone with a Termin in the time shown.
Nobody is core team after the migration: an admin ticks "Kernteam" on the Personen page.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("people", sa.Column("core_team", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("people", "core_team")
