"""Admin decisions on list entries and the login link of a person (WM-101).

List entries (places, contexts, formats, people) may now also be `abgelehnt` (a rejected
proposal) or `zusammengeführt` (merged into the entry named by `merged_into_id`). Both rows stay.
`people.login_subject` links a person to their login, set by e-mail at the first login.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLES = ("places", "contexts", "formats", "people")
OLD_STATUS = "status IN ('vorgeschlagen', 'bestätigt')"
NEW_STATUS = "status IN ('vorgeschlagen', 'bestätigt', 'abgelehnt', 'zusammengeführt')"
MERGED = "(status = 'zusammengeführt') = (merged_into_id IS NOT NULL)"


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("merged_into_id", sa.Integer(), sa.ForeignKey(f"{table}.id")))
        op.drop_constraint(f"ck_{table}_status", table, type_="check")
        op.create_check_constraint(f"ck_{table}_status", table, NEW_STATUS)
        op.create_check_constraint(f"ck_{table}_merged", table, MERGED)
    op.add_column("people", sa.Column("login_subject", sa.Text()))
    op.create_unique_constraint("people_login_subject_key", "people", ["login_subject"])


def downgrade() -> None:
    op.drop_constraint("people_login_subject_key", "people", type_="unique")
    op.drop_column("people", "login_subject")
    for table in TABLES:
        op.drop_constraint(f"ck_{table}_merged", table, type_="check")
        op.drop_constraint(f"ck_{table}_status", table, type_="check")
        op.create_check_constraint(f"ck_{table}_status", table, OLD_STATUS)  # fails while decisions exist
        op.drop_column(table, "merged_into_id")
