"""Continuous formats (WM-114): `formats.continuous`.

A continuous format (e.g. JugendLab, Repair Café) has no fixed group: every Termin of a course
in the app counts its participants, not only Terminnummer 1. Imported rows keep their recorded
numbers and their rule. Every existing format starts as not continuous.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("formats", sa.Column("continuous", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("formats", "continuous")
