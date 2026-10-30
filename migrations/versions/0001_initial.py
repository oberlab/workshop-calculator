"""Initial schema: sessions and import runs.

Revision ID: 0001
Revises:
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=False, unique=True),
        sa.Column("source_state", sa.String(32), nullable=False, server_default="active"),
        sa.Column("date", sa.Date()),
        sa.Column("course_name", sa.Text()),
        sa.Column("duration_hours", sa.Numeric(6, 2)),
        sa.Column("location", sa.Text()),
        sa.Column("school_type", sa.String(32)),
        sa.Column("class_age_text", sa.Text()),
        sa.Column("class_level", sa.Integer()),
        sa.Column("age", sa.Integer()),
        sa.Column("age_band", sa.String(32)),
        sa.Column("target_group", sa.String(64), nullable=False),
        sa.Column("participants_female", sa.Integer()),
        sa.Column("participants_male", sa.Integer()),
        sa.Column("participants_total", sa.Integer()),
        sa.Column("category", sa.String(64)),
        sa.Column("context", sa.Text()),
        sa.Column("session_number", sa.Integer()),
        sa.Column("status", sa.String(32)),
        sa.Column("instructor", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for column in ("source", "date", "target_group", "category", "context", "status"):
        op.create_index(f"ix_sessions_{column}", "sessions", [column])

    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("rows", sa.Integer()),
        sa.Column("message", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("import_runs")
    op.drop_table("sessions")
