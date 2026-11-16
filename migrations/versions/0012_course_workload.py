"""Workload values per course (WM-131 follow-up): optional overrides of the estimate's rule.

Each column overrides one value of the rule for the course's Termine: the lead's factor and
preparation factor (× course hours), the lead's fixed hours per Termin, the support's factor and
fixed hours, and the travel time for everyone. Empty means inherited: the Format's own fixed hours
(lead) or the place's travel time, else the default in the instance config (`[workload]`).

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

COLUMNS = (
    "lead_factor",
    "preparation_factor",
    "lead_overhead_hours",
    "support_factor",
    "support_overhead_hours",
    "travel_hours",
)


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("courses", sa.Column(name, sa.Numeric(6, 2)))


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("courses", name)
