"""The course calendar over CalDAV with invitations (WM-110), against the fake calendar in
`metrics.caldav_fake` (a real HTTP server on localhost; no real calendar, nobody is mailed)."""

import datetime as dt
import urllib.request

import pytest
import sqlalchemy as sa

from metrics import caldav_fake, hints, scheduler
from metrics.config import get_settings
from metrics.db import (
    CAL_MISSING,
    CAL_OK,
    CAL_OUTDATED,
    CalendarOrphan,
    CalendarResponse,
    HistoryEntry,
    StoredHint,
)
from metrics.planning import caldav, workflow
from metrics.planning.caldav import Attendee, Event
from tests.test_workflow import NEXT_WEEK, TODAY, World

ORGANIZER = "planung@example.org"


# --- iCalendar --------------------------------------------------------------------------------


def test_ics_round_trip_with_organizer_attendees_and_long_lines():
    event = Event(
        uid="zahlen-termin-7-ab12",
        summary="Löten; Teil 1, für Kinder · Grundschule",
        start=dt.datetime(2026, 11, 20, 9, 0),
        end=dt.datetime(2026, 11, 20, 11, 15),
        location="Grundschule Musterstadt",
        description="Kontext: Projekt A\\nKursleitung: Kim\n" + "x" * 200,
        status="TENTATIVE",
        organizer=ORGANIZER,
        attendees=[
            Attendee("kim@example.org", "Kim, K.", "ACCEPTED", "CHAIR"),
            Attendee("lea@example.org", "Lea"),
        ],
        sequence=3,
    )
    text = caldav.to_ics(event)
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "DTSTART:20261120T080000Z" in text  # written in UTC
    assert (
        'ATTENDEE;CN="Kim, K.";ROLE=CHAIR;PARTSTAT=ACCEPTED;RSVP=TRUE:mailto:kim@example.org'
        in text.replace("\r\n ", "")
    )
    (back,) = caldav.parse_ics(text)
    assert back == event


def test_ics_reads_tzid_folded_lines_and_alarms():
    text = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:extern-1\r\nDTSTART;TZID=Europe/Berlin:20261120T090000\r\n"
        "DTEND;TZID=Europe/Berlin:20261120T100000\r\nSUMMARY:Laser\r\n cutter\r\n"
        "ATTENDEE;PARTSTAT=DECLINED:MAILTO:kim@example.org\r\n"
        "BEGIN:VALARM\r\nDESCRIPTION:Erinnerung\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    (event,) = caldav.parse_ics(text)
    assert (event.summary, event.start, event.description) == (
        "Lasercutter",
        dt.datetime(2026, 11, 20, 9),
        None,
    )
    assert [(a.email, a.partstat) for a in event.attendees] == [("kim@example.org", "DECLINED")]


# --- Against the fake calendar ----------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch):
    server, calendar, url = caldav_fake.serve()
    monkeypatch.setenv("CALDAV_URL", url)
    monkeypatch.setenv("CALDAV_ORGANIZER", ORGANIZER)
    get_settings.cache_clear()
    yield calendar
    server.shutdown()
    get_settings.cache_clear()


@pytest.fixture
def world(db, instance):
    from metrics.ingest import store

    store.seed_reference(db, instance)
    return World(db)


def _confirmed(world, **extra):
    db = world.db
    t = workflow.create(db, world.basics(support=[str(world.lea.id)], **extra), "Kim")
    workflow.confirm(db, t, None, "Kim")
    return t


def _outbox(fake) -> list[tuple[str, str, str]]:
    sent = [(m.method, m.to, m.kind) for m in fake.outbox]
    fake.outbox.clear()
    return sent


def test_invitations_updates_removal_and_cancellation(world, fake):
    db = world.db
    calendar = caldav.get_calendar()
    t = _confirmed(world)
    message = caldav.enter(db, calendar, t, "https://zahlen.example", "Kim")
    assert message == "In den Kalender eingetragen. Eingeladen: Kim, Lea."
    assert t.calendar_state == CAL_OK and t.calendar_uid.startswith(f"zahlen-termin-{t.id}-")
    event = fake.event(t.calendar_uid)
    assert (event.organizer, event.status, event.sequence) == (ORGANIZER, "CONFIRMED", 0)
    assert [(a.email, a.role) for a in event.attendees] == [
        ("kim@example.org", "CHAIR"),
        ("lea@example.org", "REQ-PARTICIPANT"),
    ]
    assert (
        f"Termin-ID: {t.id}" in event.description
        and f"/planung/{t.course_id}?termin={t.id}" in event.description
    )
    assert _outbox(fake) == [
        ("REQUEST", "kim@example.org", "Einladung"),
        ("REQUEST", "lea@example.org", "Einladung"),
    ]
    with pytest.raises(caldav.CalendarError):
        calendar.put(event, create=True)  # never overwrites an existing event by accident

    # A save without calendar changes sends nothing.
    workflow.save(db, t, world.basics(support=[str(world.lea.id)], note="Laptops mitbringen"), "Kim")
    assert caldav.push(db, calendar, t, "https://zahlen.example", "Kim") is None and _outbox(fake) == []

    # Kim accepts; then the time changes: an update to both, answers start again.
    urllib.request.urlopen(
        urllib.request.Request(
            f"{calendar.url.removesuffix('/cal/')}/_antwort?uid={t.calendar_uid}&email=kim@example.org&partstat=ACCEPTED",
            method="POST",
        )
    )
    workflow.save(
        db, t, world.basics(support=[str(world.lea.id)], start_time="10:00", end_time="12:00"), "Kim"
    )
    note = caldav.push(db, calendar, t, "https://zahlen.example", "Kim")
    assert note == "Kalender aktualisiert: Termin."
    event = fake.event(t.calendar_uid)
    assert (event.start.hour, event.sequence) == (10, 1)
    assert {a.partstat for a in event.attendees} == {"NEEDS-ACTION"}
    assert _outbox(fake) == [
        ("REQUEST", "kim@example.org", "Aktualisierung"),
        ("REQUEST", "lea@example.org", "Aktualisierung"),
    ]

    # Lea leaves the team: she gets a cancellation, Kim nothing (only the attendee list changed).
    workflow.save(db, t, world.basics(start_time="10:00", end_time="12:00"), "Kim")
    assert caldav.push(db, calendar, t, "", "Kim") == "Kalender aktualisiert: Eingeladen, Beschreibung."
    assert _outbox(fake) == [("CANCEL", "lea@example.org", "Absage")]

    # Cancelling the Termin cancels the event; nothing is deleted.
    workflow.cancel(db, t, "Schule geschlossen", "Kim")
    assert caldav.push(db, calendar, t, "", "Kim") == "Kalendereintrag abgesagt."
    event = fake.event(t.calendar_uid)
    assert event.status == "CANCELLED" and "Abgesagt: Schule geschlossen" in event.description
    assert _outbox(fake) == [("CANCEL", "kim@example.org", "Absage")]
    assert fake.deletes == 0 and not any(r.startswith("DELETE") for r in fake.requests)
    reasons = db.scalars(sa.select(HistoryEntry.reason).where(HistoryEntry.field == "calendar_uid")).all()
    assert (
        reasons[0].startswith("in den Kalender eingetragen, eingeladen: Kim, Lea")
        and "im Kalender abgesagt" in reasons
    )


def test_check_finds_missing_outdated_answers_and_events_without_termin(world, fake):
    db = world.db
    calendar = caldav.get_calendar()
    t = _confirmed(world)
    caldav.enter(db, calendar, t, "", "Kim")
    other = _confirmed(world, start_time="13:00", end_time="14:00", lead=world.sam.id)
    caldav.enter(db, calendar, other, "", "Kim")
    # Someone moves one event in the calendar, Lea declines, and a foreign event appears.
    moved = fake.event(other.calendar_uid)
    moved.start += dt.timedelta(hours=1)
    moved.end += dt.timedelta(hours=1)
    fake.store(f"{other.calendar_uid}.ics", caldav.to_ics(moved))
    fake.answer(t.calendar_uid, "lea@example.org", "DECLINED")
    foreign = Event(
        "extern-laser",
        "Lasercutter-Wartung · Makerspace",
        dt.datetime.combine(NEXT_WEEK, dt.time(15)),
        dt.datetime.combine(NEXT_WEEK, dt.time(16)),
        "Makerspace",
    )
    fake.store("extern-laser.ics", caldav.to_ics(foreign))
    result = caldav.check(db, calendar, TODAY)
    counts = (result.checked, result.missing, result.outdated, result.responses, result.orphans)
    assert counts == (2, 0, 1, 1, 1)
    assert (t.calendar_state, other.calendar_state) == (CAL_OK, CAL_OUTDATED)
    rows = db.scalars(sa.select(CalendarResponse).where(CalendarResponse.appointment_id == t.id)).all()
    assert {(r.email, r.partstat, r.person_id) for r in rows} == {
        ("kim@example.org", "NEEDS-ACTION", world.kim.id),
        ("lea@example.org", "DECLINED", world.lea.id),
    }
    stored = {(h.appointment_id, h.rule) for h in db.scalars(sa.select(StoredHint))}
    assert (t.id, "einladung-abgelehnt") in stored and (other.id, "kalender") in stored
    assert caldav.preview(calendar, other, "") == [
        ("Termin", f"{NEXT_WEEK:%d.%m.%Y} 14:00–15:00", f"{NEXT_WEEK:%d.%m.%Y} 13:00–14:00")
    ]
    orphan = db.get(CalendarOrphan, "extern-laser")
    assert (orphan.summary, orphan.starts_at.hour) == ("Lasercutter-Wartung · Makerspace", 15)

    # "Kalender aktualisieren" puts the Termin's time back; a deleted event is missing.
    caldav.push(db, calendar, other, "", "Kim")
    del fake.objects[f"{t.calendar_uid}.ics"]
    result = caldav.check(db, calendar, TODAY)
    assert (t.calendar_state, other.calendar_state, result.missing) == (CAL_MISSING, CAL_OK, 1)
    assert hints.RULES["kalender"].check(t, None) == ["Kalendereintrag fehlt (war eingetragen)"]
    # Entering it again recreates it under the same UID.
    uid = t.calendar_uid
    caldav.enter(db, calendar, t, "", "Kim")
    assert t.calendar_uid == uid and fake.event(uid) is not None and t.calendar_state == CAL_OK


def test_deleting_a_planned_termin_cancels_its_event(world, fake, monkeypatch):
    from tests.test_lists import _team

    db = world.db
    t = workflow.create(db, world.basics(), "Kim")
    caldav.enter(db, caldav.get_calendar(), t, "", "Kim")
    db.commit()
    _outbox(fake)
    client = _team(monkeypatch)
    response = client.post(
        f"/planung/{t.course_id}/aktion",
        data={"action": "delete", "termin": t.id, "really": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "Kalendereintrag%20abgesagt" in response.headers["location"]
    event = fake.event(t.calendar_uid)
    assert event.status == "CANCELLED" and event.description.startswith("Termin gelöscht.")
    assert _outbox(fake) == [("CANCEL", "kim@example.org", "Absage")] and fake.deletes == 0


def test_course_page_enters_and_updates_the_calendar(world, fake, monkeypatch):
    from tests.test_lists import _team

    db = world.db
    t = _confirmed(world)
    db.commit()
    client = _team(monkeypatch)
    page = client.get(f"/planung/{t.course_id}").text
    assert "In Kalender eintragen" in page and "Kursleitung und Unterstützung werden" in page
    response = client.post(
        f"/planung/{t.course_id}/aktion", data={"action": "calendar", "termin": t.id}, follow_redirects=False
    )
    assert "Eingeladen%3A%20Kim%2C%20Lea" in response.headers["location"]
    page = client.get(f"/planung/{t.course_id}").text
    assert "Im Kalender eingetragen" in page and "Kim: <span" in page and "noch keine Antwort" in page
    _outbox(fake)
    # Saving a new time updates the event and tells the attendees.
    data = {
        **{k: v[0] for k, v in world.basics(support=[str(world.lea.id)], start_time="08:00").items()},
        "support": [str(world.lea.id)],
        "termin": t.id,
    }
    response = client.post(f"/planung/{t.course_id}", data=data, follow_redirects=False)
    assert "Kalender%20aktualisiert%3A%20Termin" in response.headers["location"]
    assert [m[2] for m in _outbox(fake)] == ["Aktualisierung", "Aktualisierung"]
    # An event changed in the calendar: the page shows what the update would change.
    db.expire_all()
    uid = db.get(type(t), t.id).calendar_uid
    changed = fake.event(uid)
    changed.location = "Turnhalle"
    fake.store(f"{uid}.ics", caldav.to_ics(changed))
    assert scheduler.check_calendar(dt.datetime.now(dt.UTC)).startswith(
        "1 events checked: 0 missing, 1 outdated"
    )
    page = client.get(f"/planung/{t.course_id}").text
    assert "Kalender aktualisieren" in page and "<del>Turnhalle</del>" in page


def test_calendar_job_without_a_calendar(db):
    assert scheduler.check_calendar(dt.datetime.now(dt.UTC)) == "no calendar configured (CALDAV_URL)"
    assert "calendar" in [job.name for job in scheduler.JOBS]


def test_pruefen_lists_events_without_termin(world, fake, monkeypatch):
    from tests.test_lists import _admin

    db = world.db
    foreign = Event(
        "extern-1",
        "Ferienkurs · Makerspace",
        dt.datetime.combine(NEXT_WEEK, dt.time(9)),
        dt.datetime.combine(NEXT_WEEK, dt.time(12)),
        "Makerspace",
    )
    fake.store("extern-1.ics", caldav.to_ics(foreign))
    fake.store("extern-2.ics", caldav.to_ics(Event("extern-2", "Teamtreffen", foreign.start, foreign.end)))
    db.commit()
    client = _admin(monkeypatch)
    checked = client.post("/pruefen/kalender/pruefen", follow_redirects=False)
    assert "Kalender%20gepr%C3%BCft" in checked.headers["location"]
    html = client.get("/pruefen").text
    assert "Ferienkurs · Makerspace" in html and "Teamtreffen" in html
    client.post("/pruefen/kalender/ignorieren", data={"uid": "extern-2"})
    response = client.post("/pruefen/kalender/kurs", data={"uid": "extern-1"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    t = db.scalars(sa.select(caldav.Appointment).where(caldav.Appointment.calendar_uid == "extern-1")).one()
    assert (t.date, t.start_time, t.course.place, t.course.note) == (
        NEXT_WEEK,
        dt.time(9),
        world.makerspace,
        "Ferienkurs",
    )
    html = client.get("/pruefen").text
    assert "Nicht mehr gemeldet: Teamtreffen" in html and "Ferienkurs · Makerspace" not in html
    assert fake.event("extern-1").summary == "Ferienkurs · Makerspace"  # the event itself is untouched
