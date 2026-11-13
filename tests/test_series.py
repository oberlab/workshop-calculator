"""Appointment series (WM-111): adding Termine, the computed Terminnummer, "dieser und alle
folgenden", bulk actions, the counting rule and the calendar (fake calendar). Demo data only."""

import datetime as dt
import re

import pytest
import sqlalchemy as sa

from metrics import analytics, courses
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    GEPLANT,
    Appointment,
    Course,
    HistoryEntry,
    StoredHint,
)
from metrics.planning import series, workflow
from metrics.planning.workflow import FormError
from metrics.web.common import today
from tests.test_calendar import _outbox, fake  # noqa: F401 (fixture)
from tests.test_workflow import TODAY, World, form


@pytest.fixture
def world(db, instance):
    from metrics.ingest import store

    store.seed_reference(db, instance)
    db.flush()
    return World(db)


def _youth(world, first: dt.date, **extra):
    """What a JugendLab course needs, starting on `first`."""
    return world.basics(context_id=world.youth.id, date=first.isoformat(), **extra)


def _termine(course: Course) -> list[Appointment]:
    return courses.in_order(course.appointments)


def _numbers(course: Course) -> list[tuple[dt.date, int | None]]:
    return [(t.date, t.number) for t in _termine(course)]


def test_series_dates_and_parsing():
    first = dt.date(2026, 12, 1)
    assert series.series_dates(first, series.BIWEEKLY, 3) == [
        first,
        dt.date(2026, 12, 15),
        dt.date(2026, 12, 29),
    ]
    errors = []
    days = series.parse_dates(
        form(rhythmus="daten", datum=["2026-12-03", "2026-12-01", "2026-12-03"]), errors
    )
    assert (days, errors) == ([dt.date(2026, 12, 1), dt.date(2026, 12, 3)], [])
    for bad, message in (
        (form(rhythmus="woechentlich", erster="2026-12-01", anzahl=0), "zwischen 1 und 52"),
        (form(rhythmus="woechentlich", erster="2026-12-01", anzahl=53), "zwischen 1 und 52"),
        (form(rhythmus="woechentlich", anzahl=3), "Datum des ersten Termins"),
        (form(rhythmus="daten"), "mindestens ein Datum"),
        (form(rhythmus="monatlich"), "Unbekannter Rhythmus"),
    ):
        errors = []
        series.parse_dates(bad, errors)
        assert message in " ".join(errors)
    # Holidays are not skipped: Christmas week is just another week.
    assert dt.date(2026, 12, 22) in series.series_dates(first, series.WEEKLY, 4)


def test_done_when_ten_week_jugendlab_series_moved_cancelled_and_counted(world):
    """WM-111 done when: a ten-week JugendLab series in one step, one date moved, one cancelled,
    and the participant count follows the counting rule (participants of Termin number 1)."""
    db = world.db
    first = TODAY - dt.timedelta(days=70)  # all held by now, so they can be finally confirmed
    before = analytics.totals(db, analytics.Filters(), TODAY, 9)
    t = series.create(db, _youth(world, first, reihe_rhythmus="woechentlich", reihe_anzahl=10), "Kim")
    course = t.course
    termine = _termine(course)
    assert len(termine) == 10 and [x.number for x in termine] == list(range(1, 11))
    assert [x.date for x in termine] == [first + dt.timedelta(weeks=i) for i in range(10)]
    assert {(x.start_time, x.end_time, x.lead) for x in termine} == {(dt.time(9), dt.time(11, 15), world.kim)}
    assert all(x.stage == GEPLANT and x.course is course for x in termine)

    # One date moved (only this Termin): Termin 2 moves after Termin 3, the numbers follow the dates.
    second, third = termine[1], termine[2]
    moved_to = third.date + dt.timedelta(days=2)
    series.save(db, second, _youth(world, moved_to), "Kim", scope="dieser")
    assert (second.number, third.number) == (3, 2)
    assert [x.date for x in termine if x not in (second,)] == [
        first + dt.timedelta(weeks=i) for i in range(10) if i != 1
    ]

    # All confirmed in one step, then the first one cancelled: the next one becomes number 1.
    result = series.confirm_all(db, course, "Kim")
    assert len(result.done) == 10 and not result.skipped
    workflow.cancel(db, termine[0], "Schule geschlossen", "Kim")
    assert (termine[0].stage, termine[0].number) == (ABGESAGT, None)
    assert [n for _, n in _numbers(course)] == [None, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    renumbered = db.scalars(
        sa.select(HistoryEntry).where(HistoryEntry.field == "number", HistoryEntry.reason == "neu berechnet")
    ).all()
    assert renumbered  # a changed number is in the Verlauf

    # Held and finally confirmed with different participants each week.
    counted = [x for x in _termine(course) if x.stage != ABGESAGT]
    for i, x in enumerate(counted):
        workflow.mark_done(db, x, "Kim")
        female, male = 6 + i, 5
        workflow.save(
            db,
            x,
            _youth(
                world,
                x.date,
                participants_female=female,
                participants_male=male,
                origin_name=["Musterstadt"],
                origin_count=[str(female + male)],
            ),
            "Kim",
        )
        workflow.finalize(db, x, "Kim", checked=True, today=TODAY)
    assert {x.stage for x in counted} == {ABGESCHLOSSEN}
    after = analytics.totals(db, analytics.Filters(), TODAY, 9)
    # Nine held Termine (Einsätze), participants only of number 1 (the second date: 6 + 5).
    assert (after.sessions - before.sessions, after.participants - before.participants) == (9, 11)


def test_dieser_und_alle_folgenden(world):
    db = world.db
    first = TODAY + dt.timedelta(days=7)
    t = series.create(
        db, world.basics(date=first.isoformat(), reihe_rhythmus="woechentlich", reihe_anzahl=5), "Kim"
    )
    termine = _termine(t.course)
    workflow.confirm(db, termine[4], None, "Kim")
    workflow.mark_done(db, termine[4], "Kim")  # held already: never changed by "folgende"
    third = termine[2]
    # New start, end and lead from Termin 3 on, and everything one day later.
    data = world.basics(
        date=(third.date + dt.timedelta(days=1)).isoformat(),
        start_time="14:00",
        end_time="16:00",
        lead=world.lea.id,
        support=[str(world.kim.id)],
    )
    changed = series.save(db, third, data, "Kim", scope=series.FOLLOWING)
    assert changed == [termine[3]]
    assert [(x.date, x.start_time, x.lead) for x in termine] == [
        (first, dt.time(9), world.kim),
        (first + dt.timedelta(weeks=1), dt.time(9), world.kim),
        (first + dt.timedelta(weeks=2, days=1), dt.time(14), world.lea),
        (first + dt.timedelta(weeks=3, days=1), dt.time(14), world.lea),
        (first + dt.timedelta(weeks=4), dt.time(9), world.kim),
    ]
    assert termine[3].support == [world.kim] and termine[3].duration_hours == 2
    reasons = db.scalars(
        sa.select(HistoryEntry.reason).where(
            HistoryEntry.table_name == "appointments", HistoryEntry.row_id == termine[3].id
        )
    ).all()
    assert "wie Termin 3 (" + workflow._show(third.date) + "), dieser und alle folgenden" in reasons
    # A field that was not changed keeps each Termin's own value.
    series.save(db, termine[3], world.basics(date=termine[3].date.isoformat(), lead=world.sam.id), "Kim")
    series.save(
        db,
        third,
        world.basics(
            date=third.date.isoformat(),
            start_time="15:00",
            end_time="16:00",
            lead=world.lea.id,
            support=[str(world.kim.id)],
        ),
        "Kim",
        scope=series.FOLLOWING,
    )
    assert (termine[3].start_time, termine[3].lead) == (dt.time(15), world.sam)


def test_adding_termine_and_the_double_number_guard(world):
    db = world.db
    held = TODAY - dt.timedelta(days=14)
    t = workflow.create(db, world.basics(date=held.isoformat(), support=[str(world.lea.id)]), "Kim")
    added = series.add(
        db, t.course, series.series_dates(held + dt.timedelta(weeks=1), series.WEEKLY, 2), t, "Kim"
    )
    assert [(x.number, x.lead, x.support, x.stage) for x in added] == [
        (2, world.kim, [world.lea], GEPLANT),
        (3, world.kim, [world.lea], GEPLANT),
    ]
    with pytest.raises(FormError):
        series.add(db, t.course, [], t, "Kim")
    # Number 1 is counted (frozen); a Termin dated before it later gets number 1 as well, which
    # blocks its final confirmation so the participants are not counted twice.
    workflow.confirm(db, t, None, "Kim")
    workflow.mark_done(db, t, "Kim")
    workflow.save(
        db,
        t,
        world.basics(
            date=held.isoformat(), participants_female=3, participants_male=3, support=[str(world.lea.id)]
        ),
        "Kim",
    )
    workflow.finalize(db, t, "Kim", checked=True, today=TODAY)
    (earlier,) = series.add(db, t.course, [held - dt.timedelta(weeks=1)], t, "Kim")
    assert (earlier.number, t.number, added[0].number) == (1, 1, 3)
    stored = db.scalars(sa.select(StoredHint).where(StoredHint.appointment_id == earlier.id)).all()
    assert any(
        h.rule == "terminnummer-doppelt" and h.blocking and h.other_appointment_id == t.id for h in stored
    )


def test_bulk_confirm_skips_and_cancel_remaining(world):
    db = world.db
    first = TODAY + dt.timedelta(days=3)
    t = series.create(
        db, world.basics(date=first.isoformat(), reihe_rhythmus="woechentlich", reihe_anzahl=4), "Kim"
    )
    termine = _termine(t.course)
    # Termin 2 without a start time, Termin 3 with an alternative date: both skipped.
    workflow.save(db, termine[1], world.basics(date=termine[1].date.isoformat(), start_time=""), "Kim")
    workflow.save(
        db,
        termine[2],
        world.basics(
            date=termine[2].date.isoformat(), alt_date=[(TODAY + dt.timedelta(days=40)).isoformat()]
        ),
        "Kim",
    )
    result = series.confirm_all(db, t.course, "Kim")
    assert result.done == [termine[0], termine[3]]
    assert [(x, why) for x, why in result.skipped] == [
        (termine[1], "fehlt Beginn"),
        (termine[2], "hat Alternativdaten, bitte einzeln bestätigen"),
    ]
    with pytest.raises(FormError, match="Grund"):
        series.cancel_remaining(db, t.course, " ", "Kim", TODAY)
    result = series.cancel_remaining(db, t.course, "Schule hat abgesagt", "Kim", TODAY)
    assert result.done == [termine[0], termine[3]] and len(result.skipped) == 2
    assert [x.stage for x in termine] == [ABGESAGT, GEPLANT, GEPLANT, ABGESAGT]
    assert termine[3].cancel_reason == "Schule hat abgesagt"
    # With "auch die geplanten löschen" the Geplant ones go (kept in the Verlauf); the course stays
    # because its cancelled Termine stay.
    result = series.cancel_remaining(db, t.course, "Schule hat abgesagt", "Kim", TODAY, delete_planned=True)
    assert len(result.deleted) == 2 and not result.done
    db.expire_all()
    course = db.get(Course, t.course_id)
    assert [x.stage for x in course.appointments] == [ABGESAGT, ABGESAGT]
    with pytest.raises(FormError, match="keine kommenden"):
        series.cancel_remaining(db, course, "noch mal", "Kim", TODAY)


# --- Pages and calendar ------------------------------------------------------------------------


def _page_world(world):
    world.db.commit()
    return world


def _post(client, url, data):
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


def test_series_on_the_course_page_with_one_calendar_write_per_termin(world, fake, monkeypatch):  # noqa: F811
    from tests.test_lists import _team

    db = _page_world(world).db
    client = _team(monkeypatch)
    first = today() + dt.timedelta(days=7)
    data = {k: v for k, v in world.basics(date=first.isoformat(), support=str(world.lea.id)).items()}
    data |= {"reihe_rhythmus": "woechentlich", "reihe_anzahl": "4"}
    response = _post(client, "/planung/neu", data)
    assert "Kurs%20mit%204%20Terminen%20angelegt" in response.headers["location"]
    course_id = int(re.search(r"/planung/(\d+)", response.headers["location"]).group(1))
    page = client.get(f"/planung/{course_id}").text
    assert "Termin 1 von 4" in page and "+ Termine hinzufügen" in page and "Alle bestätigen (4)" in page
    assert "diesen und alle folgenden (3 weitere)" in page

    # Add two picked dates through the form.
    extra = [(first + dt.timedelta(weeks=6)).isoformat(), (first + dt.timedelta(weeks=5)).isoformat()]
    response = _post(client, f"/planung/{course_id}/termine", {"rhythmus": "daten", "datum": extra})
    assert "2%20Termine%20hinzugef" in response.headers["location"]
    refused = client.post(
        f"/planung/{course_id}/termine",
        data={"rhythmus": "woechentlich", "erster": first.isoformat(), "anzahl": "0"},
    )
    assert refused.status_code == 400 and "zwischen 1 und 52" in refused.text

    # Confirm all, then enter all in the calendar: one invitation per person and Termin.
    _post(client, f"/planung/{course_id}/alle", {"action": "confirm"})
    db.expire_all()
    course = db.get(Course, course_id)
    assert {x.stage for x in course.appointments} == {BESTAETIGT}
    response = _post(client, f"/planung/{course_id}/alle", {"action": "calendar"})
    assert "6%20Termine%20in%20den%20Kalender" in response.headers["location"]
    sent = _outbox(fake)
    assert len(sent) == 12 and {m[2] for m in sent} == {"Einladung"}

    # A new time for this and all following Termine: one update per Termin and attendee.
    db.expire_all()
    termine = _termine(db.get(Course, course_id))
    third = termine[2]
    data = {k: v[0] for k, v in world.basics(date=third.date.isoformat(), start_time="10:00").items()}
    data |= {"support": [str(world.lea.id)], "termin": third.id, "umfang": "folgende"}
    response = _post(client, f"/planung/{course_id}", data)
    assert "Auch%203%20folgende%20Termine" in response.headers["location"]
    sent = list(fake.outbox)
    fake.outbox.clear()
    assert [m.kind for m in sent] == ["Aktualisierung"] * 8
    assert len({(m.to, m.uid) for m in sent}) == 8  # no Termin was updated twice

    # Cancel the remaining: each event cancelled once, never deleted.
    _post(client, f"/planung/{course_id}/alle", {"action": "cancel", "reason": "Projekt endet"})
    sent = _outbox(fake)
    assert len(sent) == 12 and {m[0] for m in sent} == {"CANCEL"}
    assert fake.deletes == 0
    db.expire_all()
    assert {x.stage for x in db.get(Course, course_id).appointments} == {ABGESAGT}
    page = client.get(f"/planung/{course_id}").text
    assert "Abgesagter Termin der Reihe" in page


def test_deleting_the_first_termin_renumbers_the_rest(world):
    db = world.db
    t = series.create(
        db,
        world.basics(
            date=(TODAY + dt.timedelta(days=7)).isoformat(), reihe_rhythmus="zweiwoechentlich", reihe_anzahl=3
        ),
        "Kim",
    )
    assert [x.number for x in _termine(t.course)] == [1, 2, 3]
    workflow.delete(db, _termine(t.course)[0], "Kim", really=True)
    db.expire_all()
    course = db.get(Course, t.course_id)
    assert [x.number for x in _termine(course)] == [1, 2]
