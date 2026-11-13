"""Mein Bereich and the calendar (WM-112). PostgreSQL, demo data only.

The Done-when check of the ticket: each team member sees exactly their own tasks and Termine, and
the calendar shows the same Termine as the course list with the same filters.
"""

import datetime as dt
import itertools
import re
from decimal import Decimal
from urllib.parse import quote

import pytest

from metrics import auth, logins
from metrics.db import ABGESAGT, ABGESCHLOSSEN, CONFIRMED, Appointment, CalendarResponse, Context
from metrics.planning import lanes, mine, workflow
from metrics.planning.mine import monday_of
from metrics.web.common import today
from metrics.web.planning import TABS
from tests.test_web import OIDC, _client
from tests.test_workflow import World

TODAY = today()
NEXT_MONDAY = monday_of(TODAY) + dt.timedelta(days=7)


@pytest.fixture
def world(db, instance):
    from metrics.ingest import store

    store.seed_reference(db, instance)
    w = World(db)
    w.kim.login_subject, w.lea.login_subject, w.sam.login_subject = "kc-kim", "kc-lea", "kc-sam"
    db.commit()
    return w


def _as(monkeypatch, name: str, subject: str, first: bool = True, **settings):
    """A browser logged in as `name` (a login session for `subject`, as after an OpenID login)."""
    client = _client(monkeypatch, login_db=first, **settings)
    client.cookies.set(auth.COOKIE_NAME, logins.start(auth.Login(name, subject=subject)))
    return client


def _termin(w: World, day: dt.date | None, lead, support=(), **extra):
    values = {"date": day.isoformat() if day else "", "lead": lead.id if lead else ""}
    values |= {"support": [str(p.id) for p in support]} if support else {}
    return workflow.create(w.db, w.basics(**values, **extra), "Test")


def _held(w: World, t):
    """Geplant -> Bestätigt -> Durchgeführt (the date is in the past)."""
    workflow.confirm(w.db, t, None, "Test")
    workflow.mark_done(w.db, t, "Test")
    return t


@pytest.fixture
def plan(world):
    """Kim leads A (next Tuesday) and a held course without participants; Lea leads B (next
    Wednesday) with Kim as support; Sam leads a Geplant Termin whose date has passed."""
    w = world
    a = _termin(w, NEXT_MONDAY + dt.timedelta(days=1), w.kim)  # 09:00-11:15
    b = _termin(w, NEXT_MONDAY + dt.timedelta(days=2), w.lea, [w.kim], start_time="13:00", end_time="15:00")
    held = _held(w, _termin(w, TODAY - dt.timedelta(days=1), w.kim, [w.lea]))
    late = _termin(w, TODAY - dt.timedelta(days=2), w.sam)
    w.db.commit()
    return w, {"a": a.id, "b": b.id, "held": held.id, "late": late.id}


def _ids(html: str, pattern: str = r"\?termin=(\d+)") -> set[int]:
    return {int(i) for i in re.findall(pattern, html)}


def _section(html: str, section_id: str) -> str:
    start = html.index(f'id="{section_id}"')
    return html[start : html.index("</section>", start)]


# --- Mein Bereich --------------------------------------------------------------------------------


def test_each_member_sees_exactly_their_own_tasks_and_termine(plan, monkeypatch):
    w, ids = plan
    kim = _as(monkeypatch, "Kim", "kc-kim").get("/mein-bereich")
    lea = _as(monkeypatch, "Lea", "kc-lea", first=False).get("/mein-bereich")
    sam = _as(monkeypatch, "Sam", "kc-sam", first=False).get("/mein-bereich")
    assert kim.status_code == lea.status_code == sam.status_code == 200

    # Zu erledigen: only Termine the person leads; Kim must enter the participants.
    assert _ids(_section(kim.text, "aufgaben")) == {ids["held"]}
    assert "Teilnehmende eintragen" in kim.text
    assert _ids(_section(lea.text, "aufgaben")) == set()  # Lea was only support on the held one
    assert _ids(_section(sam.text, "aufgaben")) == {ids["late"]}
    assert "Datum vorbei" in sam.text and "Datum vorbei" not in kim.text

    # Meine nächsten Termine: lead or support, from today on.
    assert _ids(_section(kim.text, "termine")) == {ids["a"], ids["b"]}
    assert _ids(_section(lea.text, "termine")) == {ids["b"]}
    assert _ids(_section(sam.text, "termine")) == set()
    assert "Unterstützung" in _section(kim.text, "termine") and "mit Lea" in kim.text

    # The readers agree with the pages, person by person.
    db = w.db
    for person in (w.kim, w.lea, w.sam):
        page = {"Kim": kim, "Lea": lea, "Sam": sam}[person.name].text
        assert {x.t.id for x in mine.tasks(db, person, TODAY)} == _ids(_section(page, "aufgaben"))
        assert {t.id for t in mine.upcoming(db, person, TODAY)} == _ids(_section(page, "termine"))


def _counter(html: str) -> str | None:
    """The sidebar counter next to Mein Bereich, if shown."""
    found = re.search(r'href="/mein-bereich"[^>]*>.*?</a>', html, re.S)
    match = re.search(r'title="(\d+) offene Aufgaben"', found.group(0)) if found else None
    return match.group(1) if match else None


def test_sidebar_counter_matches_zu_erledigen(plan, monkeypatch):
    """WM-114 done when: the counter matches Mein Bereich (tasks for the lead only)."""
    w, ids = plan
    kim, lea = _as(monkeypatch, "Kim", "kc-kim"), _as(monkeypatch, "Lea", "kc-lea", first=False)
    assert _counter(kim.get("/planung").text) == "1"  # the held course without participants
    assert _counter(lea.get("/mein-bereich").text) is None  # Lea is only support: no task
    # An unanswered invitation is a task too, also for support.
    w.db.add(
        CalendarResponse(
            appointment_id=ids["b"], email="kim@example.org", person_id=w.kim.id, partstat=mine.UNANSWERED
        )
    )
    w.db.commit()
    html = kim.get("/mein-bereich").text
    assert _counter(html) == "2" == str(len(mine.tasks(w.db, w.kim, TODAY)))
    for person, subject in ((w.kim, "kc-kim"), (w.lea, "kc-lea"), (w.sam, "kc-sam")):
        assert mine.task_count(w.db, subject, TODAY) == len(mine.tasks(w.db, person, TODAY))
    assert mine.task_count(w.db, "kc-neu", TODAY) == 0  # no linked Person
    assert _counter(_as(monkeypatch, "Neu", "kc-neu", first=False).get("/planung").text) is None


def test_workload_counts_course_hours_of_lead_and_support(plan, monkeypatch):
    w, _ = plan
    load = mine.workload(w.db, w.kim, TODAY)
    mondays = [monday_of(TODAY) + dt.timedelta(weeks=i) for i in range(4)]
    assert [week.monday for week in load.weeks] == mondays
    next_week = load.weeks[1]
    assert (next_week.lead, next_week.support, next_week.termine) == (Decimal("2.25"), Decimal(2), 2)
    assert load.capacity is None and load.scale == Decimal("4.25")
    html = _as(monkeypatch, "Kim", "kc-kim").get("/mein-bereich").text
    assert f"KW {NEXT_MONDAY.isocalendar().week}" in html and "4,2 Std. · 2 Termine" in html
    assert "Noch ohne Kapazität" in html

    w.kim.weekly_hours = Decimal(5)  # 4.25 of 5 hours: above 80 %
    w.db.commit()
    html = _as(monkeypatch, "Kim", "kc-kim").get("/mein-bereich").text
    assert "4,2 Std. / 5 Std." in html and "workload is-warning" in html


def test_a_login_without_a_person_sees_how_to_get_linked(world, monkeypatch):
    html = _as(monkeypatch, "Neu", "kc-neu", **OIDC).get("/mein-bereich").text
    assert "noch keiner Person zugeordnet" in html and "Seite „Personen“" in html
    assert 'id="aufgaben"' not in html
    shared = _client(monkeypatch)
    monkeypatch.setattr("metrics.web.login.asyncio.sleep", lambda _: _noop())
    login = shared.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False)
    assert login.headers["location"] == "/mein-bereich"
    assert "gemeinsamen Team-Passwort" in shared.get("/mein-bereich").text


async def _noop():
    return None


def test_mein_bereich_and_kalender_need_a_login(monkeypatch):
    client = _client(monkeypatch)
    for path in ("/mein-bereich", "/kalender", "/kalender/termin/1"):
        response = client.get(path, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login")


# --- Kalender ------------------------------------------------------------------------------------


def test_week_lanes_per_person(plan, monkeypatch):
    w, ids = plan
    html = _as(monkeypatch, "Kim", "kc-kim").get(f"/kalender?datum={NEXT_MONDAY.isoformat()}").text
    assert f"KW {NEXT_MONDAY.isocalendar().week}" in html
    rows = re.findall(r'<th scope="row" class="cal-person">.*?</tr>', html, re.S)
    lanes_by_name = {re.search(r"<span>([^<]+)</span></th>", row).group(1): row for row in rows}
    assert set(lanes_by_name) == {"Kim", "Lea"}
    assert _ids(lanes_by_name["Kim"], r'hx-get="/kalender/termin/(\d+)"') == {ids["a"], ids["b"]}
    assert _ids(lanes_by_name["Lea"], r'hx-get="/kalender/termin/(\d+)"') == {ids["b"]}
    assert "is-support" in lanes_by_name["Kim"] and "is-support" not in lanes_by_name["Lea"]
    assert 'class="cal-days"' in html  # the list per day for phones
    mine_only = _as(monkeypatch, "Kim", "kc-kim").get(f"/kalender?datum={NEXT_MONDAY}&person=ich").text
    assert len(re.findall(r'<th scope="row" class="cal-person">', mine_only)) == 1


def test_lanes_put_termine_without_team_in_their_own_lane(world):
    t = _termin(world, NEXT_MONDAY, None)
    (week,) = lanes.weeks([t], NEXT_MONDAY, NEXT_MONDAY + dt.timedelta(days=6))
    assert [(lane.label, lane.count) for lane in week.lanes] == [(lanes.NO_TEAM, 1)]
    assert week.by_day() == [(NEXT_MONDAY, [t])]


def test_calendar_shows_the_same_termine_as_the_course_list(plan, monkeypatch):
    """Done when: same filters, same Termine, for every phase, person and context, week and month."""
    w, ids = plan
    db = w.db
    other = Context(name="Ferienprogramm", status=CONFIRMED)
    db.add(other)
    db.flush()
    month_end = (NEXT_MONDAY.replace(day=1) + dt.timedelta(days=40)).replace(day=1)
    extra = [
        _termin(w, NEXT_MONDAY + dt.timedelta(days=3), w.lea, context_id=other.id),
        _termin(w, NEXT_MONDAY + dt.timedelta(days=5), w.sam, [w.kim]),  # a Saturday
        _termin(w, month_end - dt.timedelta(days=1), w.kim),  # last day of a month
        _termin(w, month_end, w.lea),  # first day of the next month
        _termin(w, None, w.kim),  # no date: in the list, never in the calendar
    ]
    closed, cancelled = _termin(w, TODAY - dt.timedelta(days=3), w.kim), _termin(w, TODAY, w.lea)
    closed.stage, cancelled.stage = ABGESCHLOSSEN, ABGESAGT
    db.commit()
    assert len(ids) + len(extra) + 2 == 11

    client = _as(monkeypatch, "Kim", "kc-kim")
    days = {TODAY, NEXT_MONDAY, month_end - dt.timedelta(days=1), month_end}
    checked = 0
    for view, day, phase, person, context in itertools.product(
        ("woche", "monat"),
        sorted(days),
        ["", *(key for key, _ in TABS)],
        ("", "ich", str(w.lea.id)),
        ("", str(other.id)),
    ):
        query = f"ansicht={view}&datum={day}&phase={phase}&person={person}&kontext={context}"
        html = client.get(f"/kalender?{query}").text
        shown = _ids(html, r'hx-get="/kalender/termin/(\d+)"')
        first, last = _span(view, day)
        # The link to the course list names the same days; no phase is the list's "Alle" tab.
        assert f"phase={quote(phase or 'alle')}" in html and f"von={first}&amp;bis={last}" in html
        stages = [phase] if phase else [key for key, _ in TABS if key not in ("offen", "alle", ABGESAGT)]
        listed = set()
        for stage in stages:
            filters = f"phase={stage}&person={person}&kontext={context}&von={first}&bis={last}"
            listed |= _ids(client.get(f"/planung?{filters}").text)
        assert shown == listed, query
        if not phase:  # "Alle" is every stage but Abgesagt, the union of those tabs
            filters = f"phase=alle&person={person}&kontext={context}&von={first}&bis={last}"
            assert _ids(client.get(f"/planung?{filters}").text) == listed, query
        checked += 1 if shown else 0
    assert checked > 100  # most combinations show something, so the check is not vacuous


def _span(view: str, day: dt.date) -> tuple[dt.date, dt.date]:
    if view == "monat":
        first = day.replace(day=1)
        last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        return monday_of(first), monday_of(last) + dt.timedelta(days=6)
    return monday_of(day), monday_of(day) + dt.timedelta(days=6)


def test_side_panel_and_navigation(plan, monkeypatch):
    w, ids = plan
    client = _as(monkeypatch, "Kim", "kc-kim")
    panel = client.get(f"/kalender/termin/{ids['b']}", headers={"HX-Request": "true"})
    assert panel.status_code == 200 and "<html" not in panel.text
    assert all(text in panel.text for text in ("Löten", "Kurs öffnen", "Lea", "Kim"))
    assert f"/planung/{w.db.get(Appointment, ids['b']).course_id}?termin={ids['b']}" in panel.text
    direct = client.get(f"/kalender/termin/{ids['b']}", follow_redirects=False)
    assert (direct.status_code, direct.headers["location"]) == (303, f"/planung/termin/{ids['b']}")
    assert client.get("/kalender/termin/999999", headers={"HX-Request": "true"}).status_code == 404

    week = client.get(f"/kalender?datum={NEXT_MONDAY}").text
    following = (NEXT_MONDAY + dt.timedelta(days=7)).isoformat()
    assert f"ansicht=woche&amp;datum={following}" in week
    assert 'data-side-panel="termin-panel"' in week and 'id="termin-panel-body"' in week
    month = client.get(f"/kalender?ansicht=monat&datum={NEXT_MONDAY}").text
    first, last = _span("monat", NEXT_MONDAY)
    assert month.count('class="cal-week"') == ((last - first).days + 1) // 7
    assert client.get("/kalender?ansicht=jahr").status_code == 400
    assert client.get("/kalender?phase=Unsinn").status_code == 400
    assert client.get("/kalender?datum=kein-datum").status_code == 200  # falls back to today
