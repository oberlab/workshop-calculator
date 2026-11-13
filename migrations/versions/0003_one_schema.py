"""One schema for planning, tracking and analysis (WM-100); the legacy data moves into it.

Creates places (Ort), school_types, contexts (Kontext), formats (Format), people (Person),
courses (Kurs), appointments (Termin) with support and origins (Herkunft), history (Verlauf),
proposals (Vorschlag), hint_decisions (Hinweis-Entscheidung) and job_runs.

An existing database with rows in `sessions` (the frozen baseline and any legacy rows) is
migrated: the reference data from the instance directory becomes confirmed list entries, every
recorded name becomes a confirmed entry exactly as recorded, and every row becomes a course with
one Termin (a held row in stage Abgeschlossen) that keeps its source and original key. Synced
Slack rows are not migrated (WM-120 transfers the Slack Lists once). The instructors' names and
pictures move into people. The migration checks the counts and only then drops `sessions` and
`instructors`.

This file is self-contained on purpose: it must give the same result when the application code
has changed, so it reads the instance files itself and uses its own table definitions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

import csv
import re
import tomllib
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

CONFIRMED = "bestätigt"
LIST_STATUS = "status IN ('vorgeschlagen', 'bestätigt')"
STAGES = "stage IN ('Geplant', 'Bestätigt', 'Durchgeführt', 'Abgeschlossen', 'Abgesagt')"
SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")
SOURCE_SLACK = "slack"
DEFAULT_SCHOOL_TYPES = {
    "GS": "Grundschule",
    "MS": "Mittelschule",
    "RS": "Realschule",
    "GYM": "Gymnasium",
    "MOS": "MOS",
    "FS": "Förderschule",
}


def _status() -> sa.Column:
    return sa.Column("status", sa.String(16), nullable=False, server_default=CONFIRMED)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _create_tables() -> None:
    op.create_table(
        "school_types",
        sa.Column("code", sa.String(16), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_table(
        "places",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("is_school", sa.Boolean()),
        sa.Column("school_type", sa.String(16)),
        sa.Column("address", sa.Text()),
        sa.Column("municipality", sa.Text()),
        sa.Column("lat", sa.Float()),
        sa.Column("lon", sa.Float()),
        _status(),
        _created_at(),
        sa.CheckConstraint(LIST_STATUS, name="ck_places_status"),
    )
    op.create_table(
        "contexts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("is_project", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("page_slug", sa.String(64)),
        sa.Column("page_title", sa.Text()),
        sa.Column("asks_municipalities", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("teacher_training", sa.Boolean(), nullable=False, server_default="false"),
        _status(),
        _created_at(),
        sa.CheckConstraint(LIST_STATUS, name="ck_contexts_status"),
    )
    op.create_table(
        "formats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("category", sa.String(64)),
        sa.Column("duration_hours", sa.Numeric(6, 2)),
        sa.Column("overhead_hours", sa.Numeric(6, 2)),
        _status(),
        _created_at(),
        sa.UniqueConstraint(
            "name", "category", name="uq_formats_name_category", postgresql_nulls_not_distinct=True
        ),
        sa.CheckConstraint(LIST_STATUS, name="ck_formats_status"),
    )
    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slack_id", sa.String(32), unique=True),
        sa.Column("email", sa.Text(), unique=True),
        sa.Column("kimai_user", sa.Text()),
        sa.Column("image", sa.LargeBinary()),
        sa.Column("image_type", sa.String(64)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("weekly_hours", sa.Numeric(5, 2)),
        _status(),
        _created_at(),
        sa.CheckConstraint(LIST_STATUS, name="ck_people_status"),
    )
    op.create_table(
        "courses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("format_id", sa.Integer(), sa.ForeignKey("formats.id"), index=True),
        sa.Column("context_id", sa.Integer(), sa.ForeignKey("contexts.id"), index=True),
        sa.Column("place_id", sa.Integer(), sa.ForeignKey("places.id"), index=True),
        sa.Column("class_age_text", sa.Text()),
        sa.Column("class_level", sa.Integer()),
        sa.Column("age", sa.Integer()),
        sa.Column("age_band", sa.String(32)),
        sa.Column("target_group", sa.String(64), nullable=False, index=True),
        sa.Column("note", sa.Text()),
        _created_at(),
    )
    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id"), nullable=False, index=True),
        sa.Column("number", sa.Integer()),
        sa.Column("date", sa.Date(), index=True),
        sa.Column("start_time", sa.Time()),
        sa.Column("end_time", sa.Time()),
        sa.Column("duration_hours", sa.Numeric(6, 2)),
        sa.Column("stage", sa.String(16), nullable=False, index=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("people.id"), index=True),
        sa.Column("participants_female", sa.Integer()),
        sa.Column("participants_male", sa.Integer()),
        sa.Column("participants_diverse", sa.Integer()),
        sa.Column("participants_unspecified", sa.Integer()),
        sa.Column("participants_total", sa.Integer()),
        sa.Column("billed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("calendar_uid", sa.String(255)),
        sa.Column("source", sa.String(32), nullable=False, server_default="app", index=True),
        sa.Column("source_key", sa.String(128), unique=True),
        sa.Column("source_data", JSONB()),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(STAGES, name="ck_appointments_stage"),
    )
    op.create_table(
        "appointment_support",
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id"), primary_key=True, index=True),
    )
    op.create_table(
        "appointment_origins",
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("municipality", sa.Text(), primary_key=True),
        sa.Column("participants", sa.Integer(), nullable=False),
    )
    op.create_table(
        "history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("row_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(64)),
        sa.Column("old_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reason", sa.Text()),
        sa.Index("ix_history_row", "table_name", "row_id"),
    )
    op.create_table(
        "proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_table", sa.String(64), nullable=False),
        sa.Column("target_id", sa.Integer()),
        sa.Column("proposed_values", JSONB(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decision", sa.String(32)),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_reason", sa.Text()),
        sa.CheckConstraint("kind IN ('eintrag', 'korrektur')", name="ck_proposals_kind"),
    )
    op.create_table(
        "hint_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rule", sa.String(64), nullable=False),
        sa.Column(
            "appointment_id",
            sa.Integer(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(64), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("ok", sa.Boolean()),
        sa.Column("message", sa.Text()),
    )


# --- The instance files, read the way the application read them at revision 0002 ---


def _clean(value) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _instance_dir() -> Path:
    from metrics.config import get_settings

    return get_settings().instance_dir


def _reference(instance_dir: Path) -> dict:
    config = {}
    if (instance_dir / "config.toml").exists():
        with (instance_dir / "config.toml").open("rb") as f:
            config = tomllib.load(f)

    places: dict[str, dict] = {}
    for row in _read_csv(instance_dir / "seeds" / "locations.csv"):
        lat, lon = (row.get("lat") or "").strip(), (row.get("lon") or "").strip()
        places[_clean(row["name"])] = {
            "is_school": row["school"].strip().lower() in ("ja", "yes", "true", "1"),
            "school_type": (row.get("school_type") or "").strip() or None,
            "address": _clean(row.get("address")),
            "municipality": _clean(row.get("municipality")) or None,
            "lat": float(lat) if lat and lon else None,
            "lon": float(lon) if lat and lon else None,
        }

    type_rows = _read_csv(instance_dir / "seeds" / "school_types.csv")
    school_types = (
        {row["code"].strip(): row["label"].strip() for row in type_rows}
        if type_rows
        else DEFAULT_SCHOOL_TYPES
    )

    contexts: dict[str, dict] = {}
    for name in config.get("target_groups", {}).get("teacher_contexts", []):
        contexts.setdefault(name, {})["teacher_training"] = True
    for entry in config.get("projects", []):
        title = str(entry["title"]).strip()
        slug = str(entry.get("slug") or title).strip().lower()
        for name in entry.get("contexts") or [title]:
            contexts.setdefault(name, {}).update(
                is_project=True,
                page_slug=slug,
                page_title=title,
                asks_municipalities=bool(entry.get("participant_municipalities", False)),
            )

    aliases = {
        _clean(row["alias"]): row["slack_id"].strip()
        for row in _read_csv(instance_dir / "seeds" / "instructors.csv")
    }
    return {"places": places, "school_types": school_types, "contexts": contexts, "aliases": aliases}


# --- Data migration ---


def _stage(status: str | None) -> str:
    if status == "Durchgeführt":
        return "Abgeschlossen"
    if status == "Abgesagt":
        return "Abgesagt"
    return "Geplant"


def _insert_returning_ids(conn, table: sa.Table, rows: list[dict], key) -> dict:
    """Insert rows one by one; returns key(row) -> new id."""
    ids = {}
    for row in rows:
        ids[key(row)] = conn.execute(table.insert().values(**row).returning(table.c.id)).scalar_one()
    return ids


def _migrate_sessions(conn) -> None:
    meta = sa.MetaData()
    sessions = sa.Table("sessions", meta, autoload_with=conn)
    instructors = sa.Table("instructors", meta, autoload_with=conn)
    t = {name: sa.Table(name, meta, autoload_with=conn) for name in (
        "school_types", "places", "contexts", "formats", "people", "courses", "appointments"
    )}  # fmt: skip

    rows = (
        conn.execute(sa.select(sessions).where(sessions.c.source != SOURCE_SLACK).order_by(sessions.c.id))
        .mappings()
        .all()
    )
    if not rows:
        return
    instance_dir = _instance_dir()
    if not (instance_dir / "seeds" / "locations.csv").exists():
        # Without the real instance the places would lose their addresses and school types, and
        # sessions is dropped below; refuse rather than migrate with incomplete reference data.
        raise RuntimeError(
            f"{instance_dir} has no seeds/locations.csv: set INSTANCE_DIR to the instance before migrating"
        )
    ref = _reference(instance_dir)

    # Reference data from the instance, then every recorded name not in it, exactly as recorded.
    conn.execute(
        t["school_types"].insert(),
        [
            {"code": c, "label": label, "position": i}
            for i, (c, label) in enumerate(ref["school_types"].items())
        ],
    )
    place_rows = [{"name": name, **values} for name, values in ref["places"].items()]
    place_rows += [
        {"name": name} for name in sorted({r["location"] for r in rows if r["location"]} - set(ref["places"]))
    ]
    place_ids = _insert_returning_ids(conn, t["places"], place_rows, lambda r: r["name"])

    context_rows = [{"name": name, **values} for name, values in ref["contexts"].items()]
    context_rows += [
        {"name": name} for name in sorted({r["context"] for r in rows if r["context"]} - set(ref["contexts"]))
    ]
    context_ids = _insert_returning_ids(conn, t["contexts"], context_rows, lambda r: r["name"])

    format_keys = sorted(
        {(r["course_name"], r["category"]) for r in rows}, key=lambda k: (k[0] or "", k[1] or "")
    )
    format_ids = _insert_returning_ids(
        conn,
        t["formats"],
        [{"name": name, "category": category} for name, category in format_keys],
        lambda r: (r["name"], r["category"]),
    )

    # People: Slack users from the instructor aliases and the instructors table, and every
    # recorded instructor; a legacy first name the instance maps to a Slack user is that user.
    def person_key(recorded):
        cleaned = _clean(recorded)
        return ref["aliases"].get(cleaned, cleaned) or None

    profiles = {p["key"]: p for p in conn.execute(sa.select(instructors)).mappings()}
    keys = (
        set(ref["aliases"].values()) | set(profiles) | {k for r in rows if (k := person_key(r["instructor"]))}
    )
    people = []
    for key in sorted(keys):
        profile = profiles.get(key, {})
        slack = bool(SLACK_USER_ID.match(key))
        people.append(
            {
                "name": profile.get("name") or key,
                "slack_id": key if slack else None,
                "image": profile.get("image"),
                "image_type": profile.get("image_type"),
                "active": slack,
            }
        )
    person_ids = _insert_returning_ids(conn, t["people"], people, lambda r: r["slack_id"] or r["name"])

    for row in rows:
        course_id = conn.execute(
            t["courses"]
            .insert()
            .values(
                format_id=format_ids[(row["course_name"], row["category"])],
                context_id=context_ids.get(row["context"]),
                place_id=place_ids.get(row["location"]),
                class_age_text=row["class_age_text"],
                class_level=row["class_level"],
                age=row["age"],
                age_band=row["age_band"],
                target_group=row["target_group"],
            )
            .returning(t["courses"].c.id)
        ).scalar_one()
        key = person_key(row["instructor"])
        conn.execute(
            t["appointments"]
            .insert()
            .values(
                course_id=course_id,
                number=row["session_number"],
                date=row["date"],
                duration_hours=row["duration_hours"],
                stage=_stage(row["status"]),
                lead_id=person_ids[key] if key else None,
                participants_female=row["participants_female"],
                participants_male=row["participants_male"],
                participants_total=row["participants_total"],
                source=row["source"],
                source_key=row["source_key"],
                source_data={k: row[k] for k in ("status", "instructor", "school_type")},
            )
        )

    _check_counts(conn, sessions, t)


def _check_counts(conn, sessions: sa.Table, t: dict) -> None:
    """Stop (and roll back) unless the new tables give the same key figures as `sessions`."""
    s, a, c, p = sessions.c, t["appointments"].c, t["courses"].c, t["places"].c
    old = conn.execute(
        sa.select(
            sa.func.count(),
            sa.func.coalesce(sa.func.sum(sa.case((s.session_number == 1, s.participants_total), else_=0)), 0),
            sa.func.count(sa.func.distinct(s.location)),
        ).where(s.source != SOURCE_SLACK, s.status == "Durchgeführt", s.date.is_not(None))
    ).one()
    new = conn.execute(
        sa.select(
            sa.func.count(),
            sa.func.coalesce(sa.func.sum(sa.case((a.number == 1, a.participants_total), else_=0)), 0),
            sa.func.count(sa.func.distinct(p.name)),
        )
        .select_from(t["appointments"].join(t["courses"], a.course_id == c.id).outerjoin(t["places"]))
        .where(a.stage == "Abgeschlossen", a.date.is_not(None))
    ).one()
    if tuple(old) != tuple(new):
        raise RuntimeError(f"Migration check failed: sessions {tuple(old)} != appointments {tuple(new)}")
    print(f"Migrated: {new[0]} held sessions, {new[1]} participants, {new[2]} places (counts match).")


def upgrade() -> None:
    _create_tables()
    _migrate_sessions(op.get_bind())
    op.drop_table("instructors")
    op.drop_table("sessions")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM appointments)")).scalar():
        # The Termine would be lost; nothing is deleted automatically.
        raise RuntimeError("Downgrade refused: appointments holds data. Restore a dump from before 0003.")
    for table in (
        "job_runs",
        "hint_decisions",
        "proposals",
        "history",
        "appointment_origins",
        "appointment_support",
        "appointments",
        "courses",
        "people",
        "formats",
        "contexts",
        "places",
        "school_types",
    ):
        op.drop_table(table)
    # The old tables come back empty (same definitions as revisions 0001 and 0002).
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
        "instructors",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("image", sa.LargeBinary()),
        sa.Column("image_type", sa.String(64)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
