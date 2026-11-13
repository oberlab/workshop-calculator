"""Loading into the tables and migrating an existing database (PostgreSQL, demo data only)."""

from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from metrics import analytics
from metrics.db import ABGESCHLOSSEN, Appointment, Context, Course, Person, Place, SchoolType
from metrics.ingest import store
from metrics.ingest.legacy import read_harmonised
from tests.conftest import EXAMPLE_INSTANCE, FIXTURES, alembic_upgrade


def dump(session: Session) -> dict:
    """Everything the two ways into the tables must agree on, independent of row IDs."""
    termine = set()
    for a in session.scalars(sa.select(Appointment)):
        c = a.course
        termine.add(
            (
                a.source,
                a.source_key,
                a.date,
                a.number,
                a.stage,
                a.duration_hours,
                a.participants_female,
                a.participants_male,
                a.participants_total,
                a.lead.key if a.lead else None,
                tuple(sorted(a.source_data.items())),
                (c.format.name, c.format.category) if c.format else None,
                c.context.name if c.context else None,
                c.place.name if c.place else None,
                c.class_age_text,
                c.class_level,
                c.age,
                c.age_band,
                c.target_group,
            )
        )
    return {
        "termine": termine,
        "places": {
            (p.name, p.is_school, p.school_type, p.address, p.municipality, p.lat, p.lon, p.status)
            for p in session.scalars(sa.select(Place))
        },
        "contexts": {
            (c.name, c.is_project, c.page_slug, c.page_title, c.asks_municipalities, c.teacher_training)
            for c in session.scalars(sa.select(Context))
        },
        "people": {(p.key, p.slack_id, p.active) for p in session.scalars(sa.select(Person))},
        "school_types": [
            (t.code, t.label) for t in session.scalars(sa.select(SchoolType).order_by("position"))
        ],
    }


def test_baseline_becomes_courses_with_one_termin_each(db, instance, baseline):
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    db.commit()
    termine = db.scalars(sa.select(Appointment)).all()
    assert len(termine) == len(baseline) == 9
    assert {t.stage for t in termine} == {ABGESCHLOSSEN}
    assert len({t.course_id for t in termine}) == 9
    assert all(t.source == "baseline" and t.source_key.startswith("baseline:") for t in termine)

    # Recorded names become confirmed entries exactly as recorded, unknown places without details.
    places = {p.name: p for p in db.scalars(sa.select(Place))}
    assert places["Neue Schule"].is_school is None and places["Neue Schule"].status == "bestätigt"
    assert places["Grundschule Musterstadt"].school_type == "GS"
    assert "Repair Café" in {f for (f,) in db.execute(sa.text("SELECT name FROM formats"))}
    # "Kim" is mapped to a Slack user in instance.example/seeds/instructors.csv.
    leads = {t.source_data["instructor"]: t.lead.key for t in termine if t.lead}
    assert leads["Kim"] == "U0000000001"
    assert leads["U001"] == "U001"  # not a Slack ID: a legacy name, kept as recorded

    totals = analytics.totals(db, analytics.Filters(), date(2026, 1, 1), 8)
    first = [r for r in baseline if r.session_number == 1]
    assert totals.sessions == 9
    assert totals.participants == sum(r.participants_total for r in first)
    assert totals.locations == len({r.location for r in baseline})


def test_baseline_is_loaded_once(db, instance, baseline):
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    with pytest.raises(store.BaselineFrozen):
        store.load_baseline(db, baseline, instance, "demo", "0" * 64)


def test_reference_data_from_the_instance(db, instance):
    store.seed_reference(db, instance)
    store.seed_reference(db, instance)  # a second run adds nothing
    places = {p.name: p for p in db.scalars(sa.select(Place))}
    assert set(places) == set(instance.locations)
    assert places["Makerspace"].municipality == "Beispieldorf"
    assert (places["Makerspace"].lat, places["Makerspace"].lon) == (48.08, 11.30)
    assert places["Gymnasium Nachbarstadt"].address == ""
    contexts = {c.name: c for c in db.scalars(sa.select(Context))}
    assert contexts["Projekt A"].is_project and contexts["Projekt A"].page_slug == "projekt-a"
    assert contexts["Lehrkräftefortbildung"].teacher_training
    assert [p.slack_id for p in db.scalars(sa.select(Person))] == ["U0000000001"]
    assert db.scalar(sa.select(sa.func.count()).select_from(SchoolType)) == 4


def test_legacy_load_after_the_baseline_only_takes_newer_entries(db, instance, baseline):
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    older = read_harmonised(FIXTURES / "harmonised.csv", instance)
    with pytest.raises(store.BaselineFrozen):
        store.replace_legacy(db, older, instance)
    newer = [r for r in read_harmonised(FIXTURES / "harmonised.csv", instance)[:1]]
    newer[0].date = date(2026, 1, 1)
    store.replace_legacy(db, newer, instance)
    store.replace_legacy(db, newer, instance)  # replaces, does not add
    db.flush()
    legacy = db.scalars(sa.select(Appointment).where(Appointment.source == "legacy-harmonised")).all()
    assert len(legacy) == 1
    assert db.scalar(sa.select(sa.func.count()).select_from(Course)) == 10


def test_migration_moves_an_existing_database_like_load_baseline(pg_server, db, instance, baseline):
    """A database at 0002 holding the baseline ends up with the same data as a fresh load."""
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    db.commit()
    expected = dump(db)

    url = pg_server.create_database()
    alembic_upgrade(url, "0002")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        meta = sa.MetaData()
        sessions = sa.Table("sessions", meta, autoload_with=conn)
        instructors = sa.Table("instructors", meta, autoload_with=conn)
        conn.execute(sessions.insert(), [r.as_row() for r in baseline])
        # A synced Slack row is not migrated (WM-120 transfers the Lists).
        conn.execute(
            sessions.insert(),
            [
                {
                    **baseline[0].as_row(),
                    "source": "slack",
                    "source_key": "slack:X1",
                    "date": date(2026, 10, 5),
                }
            ],
        )
        conn.execute(
            instructors.insert(),
            [{"key": "U0000000001", "name": "Kim Beispiel", "image": b"png", "image_type": "image/png"}],
        )
    alembic_upgrade(url, "head", EXAMPLE_INSTANCE)

    with Session(engine) as session:
        assert dump(session) == expected
        kim = session.scalars(sa.select(Person).where(Person.slack_id == "U0000000001")).one()
        assert (kim.name, kim.image, kim.active) == ("Kim Beispiel", b"png", True)
        tables = set(sa.inspect(engine).get_table_names())
    assert "sessions" not in tables and "instructors" not in tables
    engine.dispose()


def test_migration_of_an_empty_database_only_creates_the_tables(pg_server):
    url = pg_server.create_database()
    alembic_upgrade(url, "0002")
    alembic_upgrade(url, "head", EXAMPLE_INSTANCE.parent / "does-not-exist")
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM places")).scalar() == 0
    engine.dispose()


def test_analysis_counts_only_final_termine(db, instance, baseline):
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    termin = db.scalars(sa.select(Appointment).order_by(Appointment.date)).first()
    termin.stage = "Durchgeführt"  # held but not finally confirmed
    db.flush()
    filters = analytics.Filters()
    assert analytics.totals(db, filters, date(2026, 1, 1), 8).sessions == 8

    options = analytics.filter_options(db)
    assert options.categories == {"Code", "Engineering", "Science"}
    assert "Lehrkräfte" in options.target_groups

    places = {r.place: r for r in analytics.count_by_place(db, filters, date(2026, 1, 1), 8)}
    assert places["Mittelschule Musterstadt"].school_type == "MS"
    assert places["Neue Schule"].is_school is None
    assert analytics.school_type_labels(db) == instance.school_types

    leads = {p.key: p.count for p in analytics.count_by_lead(db, filters, date(2026, 1, 1), 8)}
    assert leads["U0000000001"] == 3  # the three entries recorded as "Kim"
    assert leads[None] == 1  # one entry without an instructor


def test_models_match_the_migrations(migrated_url):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from metrics.db import Base

    engine = sa.create_engine(migrated_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []
