"""Absences and weekly hours from Kimai (WM-142), against the fake Kimai (`metrics fake-kimai`) and
the demo data. No real Kimai: every field name the app assumes is in `metrics.kimai_map`.

The Done-when check of the ticket, as far as it goes without the real Kimai: a vacation entered in
Kimai appears in the app after one job run and flags a Termin planned on that day, and a changed
weekly-hours value changes the workload bars.
"""

import datetime as dt
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from metrics import demo, hints, kimai, kimai_fake, scheduler
from metrics import kimai_map as km
from metrics.config import get_settings
from metrics.db import BESTAETIGT, GEPLANT, Absence, Appointment, HistoryEntry, Person, StoredHint
from metrics.planning import workflow, workload

TODAY = dt.date(2026, 11, 16)  # a Monday
NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
NEXT_MONDAY = TODAY + dt.timedelta(days=7)


# --- The field mapping ---------------------------------------------------------------------------


def _user(*prefs: tuple[str, str], **fields) -> dict:
    return {"id": 1, "username": "x", "preferences": [{"name": n, "value": v} for n, v in prefs], **fields}


def test_weekly_hours_from_each_contract_type():
    assert km.weekly_hours(_user(("work_contract_type", "week"), ("hours_per_week", "144000"))) == 40
    days = [(name, "28800") for name in km.DAY_HOURS[:4]] + [("work_friday", "14400")]
    assert km.weekly_hours(_user(("work_contract_type", "day"), *days)) == 36
    # An older instance without a contract type: the day values count.
    assert km.weekly_hours(_user(*days)) == 36
    month = km.weekly_hours(_user(("work_contract_type", "month"), ("hours_per_month", "432000")))
    assert month == Decimal("27.69")  # 120 h × 12 / 52
    # No contract, an unknown or broken value: no weekly hours (the hand-entered value stays).
    assert km.weekly_hours(_user(("timezone", "Europe/Berlin"))) is None
    assert km.weekly_hours(_user(("work_contract_type", "week"))) is None
    assert km.weekly_hours(_user(("work_contract_type", "week"), ("hours_per_week", "viel"))) is None
    assert km.weekly_hours(_user(("work_contract_type", "none"), ("hours_per_week", "144000"))) is None
    assert km.weekly_hours({"id": 1}) is None


def test_absence_fields():
    a = {
        "id": 7,
        "date": "2026-11-20T00:00:00+0100",
        "type": "sickness",
        "status": "approved",
        "halfDay": True,
    }
    assert (km.day_of(a), km.kind_of(a), km.half_day(a), km.counts(a)) == (
        dt.date(2026, 11, 20),
        "Krank",
        True,
        True,
    )
    assert km.key_of(a, 3, dt.date(2026, 11, 20)) == "7"
    other = {"date": "2026-11-20", "type": "something_new"}
    assert (km.kind_of(other), km.half_day(other), km.counts(other)) == ("Abwesend", False, True)
    assert km.key_of(other, 3, dt.date(2026, 11, 20)) == "3:2026-11-20:something_new"
    assert not km.counts({**a, "status": "requested"}) and not km.counts({**a, "status": "rejected"})
    assert km.day_of({"date": "kaputt"}) is None
    assert km.day_param(TODAY) == "2026-11-16T00:00:00"


def test_matching_by_hand_first_then_by_email():
    kim = Person(id=1, name="Kim", email="Kim@Example.org ", active=True)
    lea = Person(id=2, name="Lea", email="lea@example.org", kimai_user_id=12, active=True)
    sam = Person(id=3, name="Sam", email="sam@example.org", active=True)
    old = Person(id=4, name="Ute", email="ute@example.org", active=False)
    wrong = Person(id=5, name="Mo", kimai_user_id=99, active=True)
    users = [
        {"id": 11, "email": " kim@example.ORG"},
        {"id": 12, "email": "lea.privat@example.net"},
        {"id": 13, "email": "lea@example.org"},  # Lea is assigned by hand: this one stays unmatched
        {"id": 14, "email": "jemand@example.org"},
    ]
    m = kimai.match([kim, lea, sam, old, wrong], users)
    assert [(p.name, u["id"], how) for p, u, how in m.pairs] == [
        ("Lea", 12, kimai.MANUAL),
        ("Kim", 11, kimai.EMAIL),
    ]
    assert [u["id"] for u in m.users_without] == [13, 14]
    # Inactive people are not reported as missing; an unknown assigned ID is.
    assert [p.name for p in m.people_without] == ["Sam", "Mo"]
    assert [p.name for p in m.unknown_ids] == ["Mo"]


# --- Against the fake Kimai and the demo data ----------------------------------------------------


@pytest.fixture
def fake():
    server, state, url = kimai_fake.serve(today=TODAY)
    yield state, url
    server.shutdown()


@pytest.fixture
def team(db, instance, fake, monkeypatch):
    """The demo data with Kimai switched on at the fake."""
    demo.generate(db, TODAY, 1, instance)
    db.commit()
    _, url = fake
    monkeypatch.setenv("KIMAI_URL", url)
    monkeypatch.setenv("KIMAI_TOKEN", kimai_fake.DEFAULT_TOKEN)
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def _people(db) -> dict[str, Person]:
    db.expire_all()
    return {p.name: p for p in db.scalars(sa.select(Person))}


def _run() -> str:
    job = next(j for j in scheduler.JOBS if j.name == "kimai")
    assert scheduler.run_job(job, NOW)
    with scheduler.get_sessionmaker()() as session:
        return session.scalars(
            sa.select(scheduler.JobRun.message)
            .where(scheduler.JobRun.job == "kimai")
            .order_by(scheduler.JobRun.id.desc())
        ).first()


def _absences(db, name: str | None = None) -> list[Absence]:
    db.expire_all()
    stmt = sa.select(Absence).order_by(Absence.day, Absence.id)
    if name:
        stmt = stmt.join(Person).where(Person.name == name)
    return list(db.scalars(stmt))


def test_job_is_off_without_url_and_token(db, monkeypatch, caplog):
    monkeypatch.delenv("KIMAI_URL", raising=False)
    monkeypatch.delenv("KIMAI_TOKEN", raising=False)
    get_settings.cache_clear()
    with caplog.at_level(logging.INFO):
        assert scheduler.sync_kimai(NOW) == kimai.OFF
    assert "Kimai is off" in caplog.text
    assert db.scalar(sa.select(sa.func.count(Absence.id))) == 0


def test_one_run_reads_absences_and_weekly_hours(team, fake, caplog):
    state, _ = fake
    db = team
    with caplog.at_level(logging.DEBUG):
        message = _run()
    # Kim, Lea, Sam, Jo and Ben by e-mail (Lea's has other case and spaces in Kimai). Mira's
    # e-mail differs, so she and her Kimai user stay unmatched, as do the Kimai user "Buchhaltung"
    # and the proposed Tom (not in Kimai); Ute is disabled in Kimai and inactive in the app.
    assert message.startswith(
        "5 people matched (0 by hand, 5 by e-mail), 2 Kimai users and 2 active people without a match; "
        "absences: 5 new, 0 changed, 0 withdrawn, 0 back; weekly hours changed for 0"
    )
    # Logs carry counts only: no names, e-mails or the token.
    for secret in ("Mira", "Buchhaltung", "example.org", "example.net", kimai_fake.DEFAULT_TOKEN):
        assert secret not in caplog.text
    lea = _absences(db, "Lea Muster")
    assert [(a.day, a.kind) for a in lea] == [
        (NEXT_MONDAY + dt.timedelta(days=i), "Urlaub") for i in range(3)
    ]
    assert [(a.day, a.kind, a.half_day) for a in _absences(db, "Sam Demo")] == [
        (TODAY + dt.timedelta(days=1), "Krank", False)
    ]
    assert [(a.kind, a.half_day) for a in _absences(db, "Kim Beispiel")] == [("Urlaub", True)]
    assert _absences(db, "Jo Probe") == []  # only requested, not approved
    # The weekly hours of the demo are what Kimai's contracts say (month and day contracts too).
    people = _people(db)
    assert {n: people[n].weekly_hours for n in ("Kim Beispiel", "Lea Muster", "Jo Probe")} == {
        "Kim Beispiel": 20,
        "Lea Muster": 30,
        "Jo Probe": 20,
    }
    assert people["Ben Vorlage"].weekly_hours is None  # no contract in Kimai
    # The app never wrote to Kimai and asked only GET.
    assert state.writes == [] and all(r.startswith("GET ") for r in state.requests)
    # A second run changes nothing.
    assert "absences: 0 new, 0 changed, 0 withdrawn, 0 back; weekly hours changed for 0" in _run()


def _planned_termin(db, person: Person) -> Appointment:
    """A Termin of the demo in planning after today, led by `person`."""
    return db.scalars(
        sa.select(Appointment)
        .where(
            Appointment.lead_id == person.id,
            Appointment.stage.in_((GEPLANT, BESTAETIGT)),
            Appointment.date > TODAY + dt.timedelta(days=1),
        )
        .order_by(Appointment.date, Appointment.id)
    ).first()


def test_done_when_a_vacation_flags_the_termin_and_a_withdrawal_clears_it(team, fake):
    """A vacation entered in Kimai appears after one run and flags the Termin planned that day;
    when it is cancelled in Kimai the row stays, marked withdrawn, and the hint goes."""
    state, _ = fake
    db = team
    _run()
    jo = _people(db)["Jo Probe"]
    t = _planned_termin(db, jo)
    assert t is not None
    hint = sa.select(StoredHint).where(StoredHint.appointment_id == t.id, StoredHint.rule == "abwesend")
    assert db.scalars(hint).first() is None
    (absence_id,) = state.add(4, t.date, "holiday")  # Jo is Kimai user 4

    assert "absences: 1 new" in _run()
    db.expire_all()
    (row,) = [a for a in _absences(db, "Jo Probe") if a.day == t.date]
    assert (row.kind, row.withdrawn_at) == ("Urlaub", None)
    stored = db.scalars(hint).one()
    assert (stored.level, stored.text) == (
        hints.KRITISCH,
        f"Jo Probe ist am {t.date:%d.%m.%Y} abwesend (Kimai)",
    )
    # The team pickers mark Jo that day; "Wer ist frei?" agrees.
    assert workflow.busy_people(db, t.date, None, None)[jo.id] == "abwesend (Kimai)"
    assert jo.id in {p.id for p, _ in workload.who_is_free(db, t.date, None, None).absent}

    # Cancelled in Kimai: marked withdrawn, never deleted; the hint goes.
    state.absences.pop(absence_id)
    assert "1 withdrawn" in _run()
    db.expire_all()
    (row,) = [a for a in _absences(db, "Jo Probe") if a.day == t.date]
    assert row.withdrawn_at is not None
    assert db.scalars(hint).first() is None
    assert workflow.busy_people(db, t.date, None, None).get(jo.id) != "abwesend (Kimai)"
    # Back in Kimai (same ID): the mark goes, the same row counts again.
    state.absences[absence_id] = {
        "id": absence_id,
        "user": 4,
        "date": t.date.isoformat(),
        "type": "holiday",
        "status": "approved",
        "halfDay": False,
    }
    assert "1 back" in _run()
    db.expire_all()
    assert [a.withdrawn_at for a in _absences(db, "Jo Probe") if a.day == t.date] == [None]
    assert db.scalars(hint).first() is not None


def test_done_when_changed_weekly_hours_change_the_workload_bars(team, fake):
    state, _ = fake
    db = team
    _run()
    kim = _people(db)["Kim Beispiel"]
    rows = {r.person.id: r for r in workload.table(db, [kim], TODAY, 6)}
    busy = [c for c in rows[kim.id].cells if c.total > 0]
    assert busy and rows[kim.id].capacity == 20
    lowest = min(c.total for c in busy)
    assert lowest > Decimal("0.5")
    hours = Decimal(int(lowest) - 1) if lowest >= 2 else Decimal("0.5")
    state.set_hours(1, float(hours))

    assert "weekly hours changed for 1" in _run()
    kim = _people(db)["Kim Beispiel"]
    assert kim.weekly_hours == hours
    (row,) = [r for r in workload.table(db, [kim], TODAY, 6) if r.person.id == kim.id]
    assert row.capacity == hours and all(row.above(c) for c in busy)
    (entry,) = db.scalars(
        sa.select(HistoryEntry).where(
            HistoryEntry.table_name == "people",
            HistoryEntry.row_id == kim.id,
            HistoryEntry.field == "weekly_hours",
        )
    )
    assert (entry.old_value, Decimal(entry.new_value), entry.changed_by) == ("20.00", hours, "kimai")
    # The capacity hint follows at once for Kim's Termine in planning that week.
    flagged = db.scalars(sa.select(StoredHint.text).where(StoredHint.rule == "ueber-kapazitaet")).all()
    assert any(text.startswith("Kim Beispiel:") for text in flagged)


def test_manual_assignment_before_email(team, fake):
    db = team
    _run()
    mira = _people(db)["Mira Testfeld"]
    assert _absences(db, "Mira Testfeld") == []
    mira.kimai_user_id = 5  # "mira@privat.example.net" in Kimai
    db.commit()
    message = _run()
    assert message.startswith("6 people matched (1 by hand, 5 by e-mail), 1 Kimai users and 1 active people")
    assert _people(db)["Mira Testfeld"].email == "mira.testfeld@example.org"  # never changed
    assert _people(db)["Mira Testfeld"].weekly_hours == 15


def test_a_failing_kimai_changes_nothing(team, fake, monkeypatch):
    db = team
    _run()
    before = [(a.id, a.withdrawn_at) for a in _absences(db)]
    monkeypatch.setenv("KIMAI_TOKEN", "falsch")
    get_settings.cache_clear()
    job = next(j for j in scheduler.JOBS if j.name == "kimai")
    assert scheduler.run_job(job, NOW) is False
    message = db.scalars(
        sa.select(scheduler.JobRun.message)
        .where(scheduler.JobRun.job == "kimai")
        .order_by(scheduler.JobRun.id.desc())
    ).first()
    assert message == "KimaiError: GET /users: HTTP 401" and "falsch" not in message
    assert [(a.id, a.withdrawn_at) for a in _absences(db)] == before


def test_absences_on_the_team_page_and_calendar_show_the_kind_only_to_the_person(team, fake, monkeypatch):
    from tests.test_mine import _as

    db = team
    _run()
    people = _people(db)
    people["Lea Muster"].login_subject = "kc-lea"
    db.commit()
    sam_day = TODAY + dt.timedelta(days=1)
    # Kim is the demo login (AUTH_MODE=off); Sam's sick day shows as "abwesend" to Kim and Lea.
    kim = _as(monkeypatch, "Kim", demo.LOGIN_SUBJECT)
    team_page = kim.get(f"/team?datum={TODAY}&frei={sam_day}").text
    assert "Krank" not in team_page and "abwesend" in team_page
    assert "Urlaub (halbtags)" in team_page  # Kim's own half day, with its kind
    calendar = kim.get(f"/kalender?datum={TODAY}").text
    assert "Krank" not in calendar
    lea = _as(monkeypatch, "Lea", "kc-lea", first=False)
    lea_page = lea.get(f"/team?datum={TODAY}").text
    assert "Krank" not in lea_page and "Urlaub (halbtags)" not in lea_page
    assert "abwesend (halbtags)" in lea_page
    next_week = lea.get(f"/team?datum={NEXT_MONDAY}").text
    assert "Urlaub" in next_week  # Lea's own vacation


def test_kimai_check_reads_only_and_prints_the_matching(team, fake, capsys, monkeypatch):
    from metrics import cli

    state, _ = fake
    db = team
    counts = {
        t: db.scalar(sa.select(sa.func.count()).select_from(sa.table(t))) for t in ("absences", "history")
    }
    monkeypatch.setattr("sys.argv", ["metrics", "kimai-check", "--today", TODAY.isoformat()])
    cli.main()
    out = capsys.readouterr().out
    assert "Zugeordnet: 5 (0 von Hand, 5 per E-Mail)" in out
    assert "Mira Testfeld (mira.testfeld@example.org)" in out  # app person without a Kimai user
    assert "Mira Testfeld (Kimai-ID 5, mira@privat.example.net)" in out  # Kimai user without a person
    assert "Buchhaltung (Kimai-ID 7" in out
    assert "Lea Muster = Lea Muster (Kimai-ID 2" in out and "30.00 Std./Woche" in out
    assert "GET /absences" in out and "halfDay" in out and "approved (5×)" in out
    assert "requested (1×)" in out and "hours_per_week" in out
    assert kimai_fake.DEFAULT_TOKEN not in out
    assert state.writes == [] and all(r.startswith("GET ") for r in state.requests)
    db.expire_all()
    assert {t: db.scalar(sa.select(sa.func.count()).select_from(sa.table(t))) for t in counts} == counts


def test_kimai_check_without_configuration(db, monkeypatch):
    from metrics import cli

    monkeypatch.delenv("KIMAI_URL", raising=False)
    monkeypatch.delenv("KIMAI_TOKEN", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr("sys.argv", ["metrics", "kimai-check"])
    with pytest.raises(SystemExit, match="KIMAI_URL und KIMAI_TOKEN fehlen"):
        cli.main()


def test_admin_assigns_a_kimai_user_on_the_personen_page(team, monkeypatch):
    from tests.test_lists import _admin

    db = team
    people = _people(db)
    mira, kim = people["Mira Testfeld"], people["Kim Beispiel"]
    kim.kimai_user_id = 1
    db.commit()
    from metrics import lists

    form = {**lists.form_values(lists.PEOPLE, mira)}
    admin = _admin(monkeypatch)
    url = f"/stammdaten/personen/{mira.id}"
    taken = admin.post(url, data={**form, "kimai_user_id": "1"})
    assert taken.status_code == 422 and "Die Kimai-Nutzer-ID gehört schon zu „Kim Beispiel“." in taken.text
    assert admin.post(url, data={**form, "kimai_user_id": "fünf"}).status_code == 422
    saved = admin.post(url, data={**form, "kimai_user_id": "5"}, follow_redirects=False)
    assert saved.status_code == 303
    assert _people(db)["Mira Testfeld"].kimai_user_id == 5
    (entry,) = db.scalars(
        sa.select(HistoryEntry).where(
            HistoryEntry.table_name == "people",
            HistoryEntry.row_id == mira.id,
            HistoryEntry.field == "kimai_user_id",
        )
    )
    assert (entry.old_value, entry.new_value) == (None, "5")
    # Team members cannot open the page at all (admin route).
    from tests.test_lists import _team

    assert _team(monkeypatch).post(url, data={**form, "kimai_user_id": "6"}).status_code == 403


def test_the_token_never_follows_a_redirect():
    """urllib would send the Authorization header on to wherever a redirect points."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Elsewhere(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Authorization"))
            target = getattr(self.server, "target", None)
            self.send_response(302 if target else 200)
            if target:
                self.send_header("Location", target)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"[]")

    other = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    first = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    first.target = f"http://127.0.0.1:{other.server_address[1]}/stolen"
    for server in (other, first):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = kimai.Client(f"http://127.0.0.1:{first.server_address[1]}", "geheim")
        with pytest.raises(kimai.KimaiError, match="HTTP 302"):
            client.user_list()
        assert seen == ["Bearer geheim"]  # only the first server got it
        assert "geheim" not in repr(client)
    finally:
        other.shutdown()
        first.shutdown()
