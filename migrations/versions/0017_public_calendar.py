"""Public calendar flag on contexts (WM-110).

Termine of a context marked "Im öffentlichen Kalender" also get an event in the public calendar
(CALDAV_PUBLIC_URL), without names. No context is marked after the migration.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contexts", sa.Column("public_calendar", sa.Boolean(), nullable=False, server_default="false")
    )


def downgrade() -> None:
    op.drop_column("contexts", "public_calendar")
