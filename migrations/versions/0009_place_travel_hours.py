"""Travel time per place (WM-131): `places.travel_hours`.

The Team page estimates the working hours of a Termin as course hours plus overhead; a place far
away adds its travel time (e.g. +1 h outside the district) for everyone on the Termin. Empty means
no extra time. The weekly hours of a Person and a Format's overhead already exist (0003).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("places", sa.Column("travel_hours", sa.Numeric(6, 2)))


def downgrade() -> None:
    op.drop_column("places", "travel_hours")
