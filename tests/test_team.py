"""Team page (WM-131): estimated working hours, the workload table, "Wer ist frei?". PostgreSQL,
demo data only.

The Done-when check of the ticket: the table matches a SQL calculation per person and week, and
"Wer ist frei?" agrees with the team picker.
"""

import datetime as dt
import re
from decimal import Decimal

import pytest
import sqlalchemy as sa

from metrics import hints, lists
from metrics.db import CONFIRMED, Format, StoredHint
from metrics.instance import WorkloadRule, load_workload
from metrics.planning import lanes, workflow, workload
from tests.test_mine import NEXT_MONDAY, TODAY, _as, _termin, world  # noqa: F401 (fixture)

TUESDAY, WEDNESDAY = NEXT_MONDAY + dt.timedelta(days=1), NEXT_MONDAY + dt.timedelta(days=2)


# --- The rule in the instance config ------------------------------------------------------------


def test_workload_rule_defaults_and_config():
    assert load_workload({}) == WorkloadRule(Decimal("1.0"), Decimal("1.0"), Decimal("1.0"), Decimal("0.5"))
    rule = load_workload({"workload": {"lead_overhead_hours": 2, "support_factor": 0.5}})
    assert (rule.lead_overhead, rule.support_factor, rule.lead_factor) == (2, Decimal("0.5"), 1)
    with pytest.raises(ValueError, match="unknown"):
        load_workload({"workload": {"overhead": 1}})
    with pytest.raises(ValueError, match="between"):
        load_workload({"workload": {"lead_factor": -1}})
    with pytest.raises(ValueError, match="number"):
        load_workload({"workload": {"lead_factor": "viel"}})


def test_example_instance_uses_the_proposed_defaults(instance):
    assert instance.workload == WorkloadRule()


# --- A week of the team ---------------------------------------------------------------------------


@pytest.fixture
def week(world):  # noqa: F811 (the fixture imported above)
    """Next week: Kim leads A at the school (1 h travel) with Lea as support, Lea leads B with Kim
    as support, Kim leads C without times (HackDays: 4 h usual duration, 3 h preparation); a
    cancelled Termin of Kim's does not count. Sam leads D the week after."""
    w = world
    w.school.travel_hours = Decimal(1)
    hackdays = Format(
        name="HackDays", category="Engineering", duration_hours=4, overhead_hours=3, status=CONFIRMED
    )
    w.db.add(hackdays)
    w.db.flush()
    a = _termin(w, TUESDAY, w.kim, [w.lea])  # 09:00-11:15 at the school
    b = _termin(w, WEDNESDAY, w.lea, [w.kim], start_time="13:00", end_time="15:00", place_id=w.makerspace.id)
    c = _termin(
        w, WEDNESDAY, w.kim, start_time="", end_time="", format_id=hackdays.id, place_id=w.makerspace.id
    )
    gone = _termin(w, TUESDAY, w.kim, start_time="16:00", end_time="18:00")
    workflow.confirm(w.db, gone, None, "Test")
    workflow.cancel(w.db, gone, "Schule hat abgesagt", "Test")
    d = _termin(w, NEXT_MONDAY + dt.timedelta(days=8), w.sam)
    w.db.commit()
    return w, {"a": a, "b": b, "c": c, "gone": gone, "d": d}


def test_estimate_per_role_with_format_overhead_and_travel(week):
    w, t = week
    a, b, c = t["a"], t["b"], t["c"]
    assert workload.estimate(a, lanes.LEAD).total == Decimal("4.25")  # 2.25 + 1 + 1 travel
    assert workload.estimate(a, lanes.SUPPORT).total == Decimal("3.75")  # 2.25 + 0.5 + 1 travel
    assert workload.estimate(b, lanes.SUPPORT).total == Decimal("2.5")
    hack = workload.estimate(c, lanes.LEAD)
    assert (hack.course, hack.overhead, hack.total) == (4, 3, 7)  # the Format's duration and overhead
    rule = WorkloadRule(lead_factor=Decimal(2), lead_overhead=Decimal(0))
    assert workload.estimate(b, lanes.LEAD, rule).total == Decimal(4)


def _cells(rows) -> dict[tuple[int, dt.date], tuple[int, Decimal]]:
    return {
        (row.person.id, cell.monday): (cell.termine, round(cell.total, 2))
        for row in rows
        for cell in row.cells
        if cell.termine
    }


# The estimate in SQL, independent of the Python code: per person and calendar week, the Termine
# (lead and support) and the estimated hours of every Termin but the Abgesagt ones.
SQL = sa.text(
    """
    WITH team AS (
        SELECT id AS appointment_id, lead_id AS person_id, 'lead' AS role
        FROM appointments WHERE lead_id IS NOT NULL
        UNION ALL
        SELECT appointment_id, person_id, 'support' FROM appointment_support
    ), termine AS (
        SELECT a.id, a.date, f.overhead_hours, p.travel_hours,
               COALESCE(
                   CASE WHEN a.start_time IS NOT NULL AND a.end_time IS NOT NULL
                        THEN EXTRACT(EPOCH FROM a.end_time - a.start_time) / 3600 END,
                   a.duration_hours,
                   f.duration_hours
               ) AS course_hours
        FROM appointments a
        JOIN courses c ON c.id = a.course_id
        LEFT JOIN formats f ON f.id = c.format_id
        LEFT JOIN places p ON p.id = c.place_id
        WHERE a.stage <> 'Abgesagt' AND a.date BETWEEN :start AND :end
    )
    SELECT team.person_id, date_trunc('week', t.date)::date AS monday, COUNT(*) AS termine,
           SUM(CASE team.role
                   WHEN 'lead' THEN COALESCE(t.course_hours, 0) * :lead_factor
                                    + COALESCE(t.overhead_hours, :lead_overhead)
                   ELSE COALESCE(t.course_hours, 0) * :support_factor + :support_overhead
               END + COALESCE(t.travel_hours, 0)) AS hours
    FROM team JOIN termine t ON t.id = team.appointment_id
    GROUP BY 1, 2
    """
)


def sql_table(db, start: dt.date, weeks: int) -> dict[tuple[int, dt.date], tuple[int, Decimal]]:
    rule = workload.rule()
    params = {
        "start": start,
        "end": start + dt.timedelta(weeks=weeks, days=-1),
        "lead_factor": rule.lead_factor,
        "lead_overhead": rule.lead_overhead,
        "support_factor": rule.support_factor,
        "support_overhead": rule.support_overhead,
    }
    return {(p, m): (n, round(Decimal(h), 2)) for p, m, n, h in db.execute(SQL, params)}


def test_table_matches_sql_per_person_and_week(week, monkeypatch):
    """WM-131 done when (first half)."""
    w, t = week
    start = workload.monday_of(TODAY)
    rows = workload.table(w.db, lists.choices(w.db, lists.PEOPLE), start, 4)
    expected = sql_table(w.db, start, 4)
    assert _cells(rows) == expected
    assert expected[(w.kim.id, NEXT_MONDAY)] == (3, Decimal("13.75"))  # 4.25 + 2.5 + 7
    assert expected[(w.lea.id, NEXT_MONDAY)] == (2, Decimal("6.75"))  # 3.75 + 3
    assert expected[(w.sam.id, NEXT_MONDAY + dt.timedelta(days=7))] == (1, Decimal("4.25"))  # at the school
    kim = next(r for r in rows if r.person.id == w.kim.id).cells[1]
    assert (kim.lead, kim.support, kim.lead_termine, kim.support_termine) == (Decimal("11.25"), 2.5, 2, 1)

    # The page shows the same numbers, in the month view too, and with a different rule.
    html = _as(monkeypatch, "Kim", "kc-kim").get("/team").text
    shown = {
        (int(p), dt.date.fromisoformat(m)): (int(n), round(Decimal(h), 2))
        for p, cells in re.findall(r'<tr data-person="(\d+)">(.*?)</tr>', html, re.S)
        for m, h, n in re.findall(r'data-week="([\d-]+)" data-hours="([\d.]+)" data-termine="(\d+)"', cells)
        if int(n)
    }
    assert shown == expected
    month = _as(monkeypatch, "Kim", "kc-kim", first=False).get(f"/team?ansicht=monat&datum={NEXT_MONDAY}")
    assert month.status_code == 200 and f'data-person="{w.kim.id}"' in month.text and "13,8" in month.text
    rule = WorkloadRule(Decimal("1.5"), Decimal(2), Decimal("0.5"), Decimal(0))
    monkeypatch.setattr(workload, "rule", lambda: rule)
    rows = workload.table(w.db, [], start, 4)
    assert _cells(rows) == sql_table(w.db, start, 4)


def test_mein_bereich_shows_the_same_estimate(week, monkeypatch):
    w, _ = week
    load = workload.table(w.db, [w.kim], workload.monday_of(TODAY), 4)
    from metrics.planning import mine

    mine_load = mine.workload(w.db, w.kim, TODAY)
    kim = next(r for r in load if r.person.id == w.kim.id)
    assert [c.total for c in mine_load.weeks] == [c.total for c in kim.cells]
    html = _as(monkeypatch, "Kim", "kc-kim").get("/mein-bereich").text
    assert "13,8 Std. · 3 Termine" in html and "ohne Dauer" not in html


def test_page_shows_lanes_for_the_team_and_the_formula(week, monkeypatch):
    w, t = week
    html = _as(monkeypatch, "Kim", "kc-kim").get(f"/team?datum={NEXT_MONDAY}").text
    rows = re.findall(r'<th scope="row" class="cal-person">.*?</tr>', html, re.S)
    lanes_by_name = {re.search(r"<span>([^<]+)</span></th>", row).group(1): row for row in rows}
    team = {p.name for p in lists.choices(w.db, lists.PEOPLE)}
    assert set(lanes_by_name) == team and "Sam" in team  # Sam has no Termin this week, still a lane
    found = re.findall(r'hx-get="/kalender/termin/(\d+)"', lanes_by_name["Kim"])
    assert {int(i) for i in found} == {t["a"].id, t["b"].id, t["c"].id}  # not the cancelled one
    assert "Kursstunden × 1 + 1 Std. Vor- und Nachbereitung" in html
    assert "Unterstützung: Kursstunden × 1 + 0,5 Std." in html
    assert _as(monkeypatch, "Kim", "kc-kim", first=False).get("/team?ansicht=jahr").status_code == 400


def test_absences_hook_greys_out_lanes_and_who_is_free(week, monkeypatch):
    """Until WM-142 nobody is absent; once the hook returns something, lanes and "Wer ist frei?"
    show it."""
    w, _ = week
    assert workload.absences(w.db, NEXT_MONDAY, NEXT_MONDAY) == {}
    monkeypatch.setattr(workload, "absences", lambda s, a, b: {w.sam.id: {TUESDAY: "Urlaub"}})
    html = _as(monkeypatch, "Kim", "kc-kim").get(f"/team?datum={NEXT_MONDAY}&frei={TUESDAY}").text
    assert '<span class="cal-absent">Urlaub</span>' in html
    assert f'data-absent="{w.sam.id}"' in html and f'<li data-person="{w.sam.id}">' not in html
    assert f'data-busy="{w.kim.id}"' in html  # Kim leads A that Tuesday


def test_who_is_free_agrees_with_the_team_picker(week, monkeypatch):
    """WM-131 done when (second half): the free people are the pickers' people the picker does not
    mark as busy, for the whole day and for a time."""
    w, t = week
    client = _as(monkeypatch, "Kim", "kc-kim")
    team = {p.id for p in lists.choices(w.db, lists.PEOPLE)}
    for day, start, end in (
        (TUESDAY, "10:00", "11:00"),  # A: Kim and Lea
        (TUESDAY, "12:00", "13:00"),  # nobody
        (WEDNESDAY, "", ""),  # the whole day: B and C (C has no time)
        (WEDNESDAY, "16:00", "17:00"),  # C has no time: it keeps Kim busy all day
    ):
        busy = client.get(f"/api/planung/belegt?date={day}&start={start}&end={end}").json()
        html = client.get(f"/team?frei={day}&von={start}&bis={end}").text
        free = {int(i) for i in re.findall(r'<li data-person="(\d+)">', html)}
        not_free = {int(i) for i in re.findall(r'data-busy="(\d+)"', html)}
        assert free == team - {int(k) for k in busy}
        assert not_free == team & {int(k) for k in busy}
        found = workload.who_is_free(w.db, day, workflow._time(start, "", []), workflow._time(end, "", []))
        assert {p.id for p in found.free} == free
    assert free == team - {w.kim.id} and w.lea.id in free


# --- The hint ---------------------------------------------------------------------------------


def test_capacity_hint_follows_the_week(week):
    w, t = week
    db = w.db
    w.kim.weekly_hours = Decimal(10)
    db.flush()
    hints.refresh_all(db, TODAY)

    def flagged() -> set[int]:
        stmt = sa.select(StoredHint.appointment_id).where(StoredHint.rule == "ueber-kapazitaet")
        return set(db.scalars(stmt))

    assert flagged() == {t["a"].id, t["b"].id, t["c"].id}  # Kim's Termine that week
    text = db.scalars(sa.select(StoredHint.text).where(StoredHint.rule == "ueber-kapazitaet")).first()
    assert text == f"Kim: in KW {NEXT_MONDAY.isocalendar().week} 13,8 Std. geschätzt, Wochenstunden 10"
    # Moving C to the week after takes Kim's week to 6.75 hours: the hint goes away from the
    # Termine left behind too, and C alone (7 hours) is below the weekly hours.
    c = t["c"]
    later = WEDNESDAY + dt.timedelta(days=7)
    values = {"start_time": "", "end_time": "", "format_id": c.course.format_id, "place_id": w.makerspace.id}
    workflow.save(db, c, w.basics(date=later.isoformat(), **values), "Test")
    db.flush()
    assert c.date == later and flagged() == set()
