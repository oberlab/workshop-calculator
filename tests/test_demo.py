"""WM-160: the demo data generator (`metrics demo-data`) on an empty database."""

import datetime as dt

import pytest
from sqlalchemy import select

from metrics import courses, demo, hints, metrics
from metrics.db import ABGESCHLOSSEN, Appointment, Base, Course, ImportRun, StoredHint
from metrics.web import common
from tests.test_key_figures import sql_figures
from tests.test_web import _client

TODAY = dt.date(2026, 11, 16)
AUG = 8


@pytest.fixture
def demo_db(db, instance):
    demo.generate(db, TODAY, 1, instance)
    db.commit()
    return db


def _fingerprint(session) -> list[tuple]:
    rows = session.execute(
        select(
            Appointment.id,
            Appointment.date,
            Appointment.start_time,
            Appointment.stage,
            Appointment.number,
            Appointment.participants_total,
            Appointment.lead_id,
            Course.place_id,
            Course.format_id,
        )
        .join(Course)
        .order_by(Appointment.id)
    )
    return [tuple(row) for row in rows]


def test_every_table_is_filled_or_left_empty_on_purpose(demo_db):
    assert set(Base.metadata.tables) == set(demo.FILLED) | set(demo.LEFT_EMPTY)
    counts = demo.table_counts(demo_db)
    assert [name for name in demo.FILLED if counts[name] == 0] == []
    assert [name for name in demo.LEFT_EMPTY if counts[name]] == []
    assert counts["appointments"] > 300
    # Every stage, and the calendar fields stay empty.
    stages = set(demo_db.scalars(select(Appointment.stage)))
    assert stages == {"Geplant", "Bestätigt", "Durchgeführt", "Abgeschlossen", "Abgesagt"}
    assert not demo_db.scalar(select(Appointment.id).where(Appointment.calendar_uid.is_not(None)))


def test_the_hints_compute_and_show_every_case(demo_db):
    stored = {h.rule for h in demo_db.scalars(select(StoredHint))}
    found = hints.refresh_all(demo_db, TODAY)
    assert {f.rule for f in found} == stored
    assert {
        "material-knapp",
        "ueber-kapazitaet",
        "person-doppelt",
        "geplant-vorbei",
        "pflicht-abschluss",
        "eintrag-wartet",
        "viele-teilnehmende",
    } <= stored
    # Counted Termine are as the final confirmation leaves them: nothing blocking.
    held = {t.id for t in demo_db.scalars(select(Appointment).where(Appointment.stage == ABGESCHLOSSEN))}
    assert [f for f in found if f.blocking and f.appointment_id in held] == []
    # The Terminnummer is what the app computes.
    for course in demo_db.scalars(select(Course)):
        assert courses.renumber(demo_db, course, "test") == []


@pytest.mark.parametrize(
    ("preset", "category", "context"),
    [
        ("all", None, None),
        ("this_school_year", None, None),
        ("last_school_year", None, None),
        ("all", "Code", None),
        ("all", None, "Projekt A"),
    ],
)
def test_key_figures_equal_sql(demo_db, preset, category, context):
    filters = metrics.Filters(
        preset=preset, categories=[category] if category else [], contexts=[context] if context else []
    )
    figures = metrics.key_figures(demo_db, filters, TODAY, AUG)
    start, end = metrics.resolve_period(preset, TODAY, AUG)
    assert figures.__dict__ == sql_figures(demo_db, start, end, category, context)
    assert figures.sessions > 0 and figures.participants > 0


def test_a_second_run_refuses_and_a_reset_gives_the_same_data(demo_db, instance):
    before = _fingerprint(demo_db)
    with pytest.raises(demo.NotEmpty):
        demo.generate(demo_db, TODAY, 1, instance)
    demo.reset(demo_db)
    demo.generate(demo_db, TODAY, 1, instance)
    demo_db.commit()
    assert _fingerprint(demo_db) == before
    demo.reset(demo_db)
    demo.generate(demo_db, TODAY, 2, instance)
    assert _fingerprint(demo_db) != before


def test_reset_only_empties_a_demo_database(db, instance):
    with pytest.raises(demo.NotEmpty):
        demo.reset(db)  # empty, but not made by the generator
    demo.generate(db, TODAY, 1, instance)
    db.add(ImportRun(kind="baseline", rows=1))
    db.flush()
    with pytest.raises(demo.NotEmpty):
        demo.reset(db)


def test_every_page_shows_the_demo(db, instance, monkeypatch):
    day = common.today()
    demo.generate(db, day, 1, instance)
    db.commit()
    client = _client(monkeypatch, auth_mode="off")
    course_id = db.scalar(select(Course.id).order_by(Course.id))
    missing = db.scalar(
        select(Appointment.id).where(
            Appointment.stage == "Durchgeführt", Appointment.participants_total.is_(None)
        )
    )
    for path in (
        "/",
        "/analyse",
        "/projekte/projekt-a",
        "/team",
        "/kalender",
        "/kalender?spuren=material",
        "/pruefen",
        "/planung",
        f"/planung/{course_id}",
        "/stammdaten/orte",
        "/stammdaten/kontexte",
        "/stammdaten/formate",
        "/stammdaten/material",
        "/stammdaten/personen",
        f"/mein-bereich/nach-dem-kurs/{missing}",
        "/api/analyse/municipalities",
        "/api/analyse/origins?context=Projekt+A",
    ):
        assert client.get(path).status_code == 200, path
    mine = client.get("/mein-bereich").text
    assert "Kim Beispiel" in mine and "Teilnehmende eintragen" in mine
    assert "Calliope mini" in client.get("/kalender?spuren=material").text
