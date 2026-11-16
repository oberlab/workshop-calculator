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
from metrics.db import ABGESCHLOSSEN, CONFIRMED, Format, HistoryEntry, StoredHint
from metrics.instance import WorkloadRule, load_workload
from metrics.planning import lanes, workflow, workload
from tests.test_mine import NEXT_MONDAY, TODAY, _as, _termin, world  # noqa: F401 (fixture)

TUESDAY, WEDNESDAY = NEXT_MONDAY + dt.timedelta(days=1), NEXT_MONDAY + dt.timedelta(days=2)


# --- The rule in the instance config ------------------------------------------------------------


def test_workload_rule_defaults_and_config():
    assert load_workload({}) == WorkloadRule(Decimal("1.0"), Decimal("1.0"), Decimal("1.0"), Decimal("0.5"))
    assert WorkloadRule().preparation_factor == Decimal("0.5")  # Alex, 2026-11-16
    rule = load_workload(
        {"workload": {"lead_overhead_hours": 2, "support_factor": 0.5, "preparation_factor": 1}}
    )
    assert (rule.lead_overhead, rule.support_factor, rule.lead_factor) == (2, Decimal("0.5"), 1)
    assert rule.preparation_factor == 1
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
    as support, Kim leads C without times (HackDays: 4 h usual duration, 3 h fixed hours); a
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
    # 2.25 course hours + 0.5 × 2.25 preparation + 1 + 1 travel
    assert workload.estimate(a, lanes.LEAD).total == Decimal("5.375")
    assert workload.estimate(a, lanes.SUPPORT).total == Decimal("3.75")  # 2.25 + 0.5 + 1 travel
    assert workload.estimate(b, lanes.SUPPORT).total == Decimal("2.5")
    hack = workload.estimate(c, lanes.LEAD)
    # The Format's duration and fixed hours: 4 + 2 preparation + 3
    assert (hack.course, hack.preparation, hack.overhead, hack.total) == (4, Decimal("0.5"), 3, 9)
    rule = WorkloadRule(lead_factor=Decimal(2), lead_overhead=Decimal(0), preparation_factor=Decimal(0))
    assert workload.estimate(b, lanes.LEAD, rule).total == Decimal(4)
    v = workload.values(a.course)
    assert [(x.amount, x.origin) for x in (v.preparation_factor, v.lead_overhead, v.travel)] == [
        (Decimal("0.5"), "Standard"),
        (1, "Standard"),
        (1, f"Ort „{w.school.name}“"),
    ]
    assert workload.values(c.course).lead_overhead.origin == "Format „HackDays“"


def _cells(rows) -> dict[tuple[int, dt.date], tuple[int, Decimal]]:
    return {
        (row.person.id, cell.monday): (cell.termine, round(cell.total, 2))
        for row in rows
        for cell in row.cells
        if cell.termine
    }


# The estimate in SQL, independent of the Python code: per person and calendar week, the Termine
# (lead and support) and the estimated hours of every Termin but the Abgesagt ones. Each value is
# the course's own, else the Format's (lead's fixed hours) or the place's (travel), else the default.
SQL = sa.text(
    """
    WITH team AS (
        SELECT id AS appointment_id, lead_id AS person_id, 'lead' AS role
        FROM appointments WHERE lead_id IS NOT NULL
        UNION ALL
        SELECT appointment_id, person_id, 'support' FROM appointment_support
    ), termine AS (
        SELECT a.id, a.date,
               COALESCE(c.lead_factor, :lead_factor) AS lead_factor,
               COALESCE(c.preparation_factor, :preparation_factor) AS preparation_factor,
               COALESCE(c.lead_overhead_hours, f.overhead_hours, :lead_overhead) AS lead_overhead,
               COALESCE(c.support_factor, :support_factor) AS support_factor,
               COALESCE(c.support_overhead_hours, :support_overhead) AS support_overhead,
               COALESCE(c.travel_hours, p.travel_hours, 0) AS travel_hours,
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
                   WHEN 'lead' THEN COALESCE(t.course_hours, 0) * (t.lead_factor + t.preparation_factor)
                                    + t.lead_overhead
                   ELSE COALESCE(t.course_hours, 0) * t.support_factor + t.support_overhead
               END + t.travel_hours) AS hours
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
        "preparation_factor": rule.preparation_factor,
    }
    return {(p, m): (n, round(Decimal(h), 2)) for p, m, n, h in db.execute(SQL, params)}


def test_table_matches_sql_per_person_and_week(week, monkeypatch):
    """WM-131 done when (first half)."""
    w, t = week
    start = workload.monday_of(TODAY)
    rows = workload.table(w.db, lists.choices(w.db, lists.PEOPLE), start, 4)
    expected = sql_table(w.db, start, 4)
    assert _cells(rows) == expected
    assert expected[(w.kim.id, NEXT_MONDAY)] == (3, Decimal("16.88"))  # 5.375 + 2.5 + 9
    assert expected[(w.lea.id, NEXT_MONDAY)] == (2, Decimal("7.75"))  # 3.75 + 4
    assert expected[(w.sam.id, NEXT_MONDAY + dt.timedelta(days=7))] == (1, Decimal("5.38"))  # at the school
    kim = next(r for r in rows if r.person.id == w.kim.id).cells[1]
    assert (kim.lead, kim.support, kim.lead_termine, kim.support_termine) == (Decimal("14.375"), 2.5, 2, 1)

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
    assert month.status_code == 200 and f'data-person="{w.kim.id}"' in month.text and "16,9" in month.text
    rule = WorkloadRule(Decimal("1.5"), Decimal(2), Decimal("0.5"), Decimal(0), Decimal("0.25"))
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
    assert "16,9 Std. · 3 Termine" in html and "ohne Dauer" not in html


def test_page_shows_lanes_for_the_team_and_the_formula(week, monkeypatch):
    w, t = week
    html = _as(monkeypatch, "Kim", "kc-kim").get(f"/team?datum={NEXT_MONDAY}").text
    rows = re.findall(r'<th scope="row" class="cal-person">.*?</tr>', html, re.S)
    lanes_by_name = {re.search(r"<span>([^<]+)</span></th>", row).group(1): row for row in rows}
    team = {p.name for p in lists.choices(w.db, lists.PEOPLE)}
    assert set(lanes_by_name) == team and "Sam" in team  # Sam has no Termin this week, still a lane
    found = re.findall(r'hx-get="/kalender/termin/(\d+)"', lanes_by_name["Kim"])
    assert {int(i) for i in found} == {t["a"].id, t["b"].id, t["c"].id}  # not the cancelled one
    assert "Kursleitung: Kurszeit + Vorbereitung (0,5 × Kurszeit) + 1 Std. Zuschlag + Fahrtzeit" in html
    assert "Unterstützung: Kurszeit + 0,5 Std. Zuschlag + Fahrtzeit" in html
    assert "Kurse mit eigenen Werten" not in html
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


# --- A course's own values ---------------------------------------------------------------------


def _own(db, t, by="Kim", **values):
    """Save a course's own workload values the way the course page does."""
    return workflow.save(db, t, {k: [v] for k, v in values.items()}, by, only={workflow.WORKLOAD})


def test_course_values_override_the_rule_and_match_sql(week, monkeypatch):
    """WM-131 follow-up (Alex, 2026-11-16): a course's own values come first, then the Format's or
    the place's, then the default; the table still matches the SQL calculation."""
    w, t = week
    db = w.db
    a, b, c = t["a"], t["b"], t["c"]
    w.kim.weekly_hours = Decimal(14)
    db.flush()
    hints.refresh_all(db, TODAY)
    capacity = sa.select(StoredHint.appointment_id).where(StoredHint.rule == "ueber-kapazitaet")
    assert set(db.scalars(capacity)) == {a.id, b.id, c.id}  # 16.875 hours above 14

    # A: preparation 1 × (with a decimal comma), no travel although the place has 1 h (0 is a
    # value), support +2 h; B: support × 0.5; C: lead × 0.5 and the lead's fixed hours 0.5 instead
    # of the Format's 3.
    changed = _own(db, a, preparation_factor="1,0", travel_hours="0", support_overhead_hours="2")
    assert set(changed) == {"preparation_factor", "travel_hours", "support_overhead_hours"}
    _own(db, b, support_factor="0.5")
    _own(db, c, lead_factor="0,5", lead_overhead_hours="0.5")
    db.commit()
    assert (a.course.preparation_factor, a.course.travel_hours, a.course.lead_overhead_hours) == (1, 0, None)
    v = workload.values(a.course)
    assert (v.travel.amount, v.travel.origin, v.lead_overhead.origin) == (0, "Kurs", "Standard")
    assert workload.values(c.course).lead_overhead.origin == "Kurs"
    assert workload.values(c.course).lead_factor == workload.Value(Decimal("0.5"), "Kurs")
    assert workload.values(a.course).lead_factor.origin == "Standard"
    assert workload.estimate(a, lanes.LEAD).total == Decimal("5.5")  # 2.25 + 2.25 + 1 + 0
    assert workload.estimate(a, lanes.SUPPORT).total == Decimal("4.25")  # 2.25 + 2 + 0
    assert workload.estimate(b, lanes.SUPPORT).total == Decimal("1.5")  # 2 × 0.5 + 0.5
    assert workload.estimate(c, lanes.LEAD).total == Decimal("4.5")  # 4 × 0.5 + 2 + 0.5

    start = workload.monday_of(TODAY)
    expected = sql_table(db, start, 4)
    assert _cells(workload.table(db, lists.choices(db, lists.PEOPLE), start, 4)) == expected
    assert expected[(w.kim.id, NEXT_MONDAY)] == (3, Decimal("11.50"))  # 5.5 + 1.5 + 4.5
    assert expected[(w.lea.id, NEXT_MONDAY)] == (2, Decimal("8.25"))  # 4.25 + 4
    rule = WorkloadRule(Decimal("1.5"), Decimal(2), Decimal("0.5"), Decimal(0), Decimal("0.25"))
    with monkeypatch.context() as m:
        m.setattr(workload, "rule", lambda: rule)
        assert _cells(workload.table(db, [], start, 4)) == sql_table(db, start, 4)

    # Saving refreshed the hints: Kim's week (11.5 hours) is below 14 now.
    assert set(db.scalars(capacity)) == set()
    # The Verlauf records each value; Mein Bereich and the Team page show the same sums.
    rows = db.scalars(
        sa.select(HistoryEntry.field).where(
            HistoryEntry.table_name == "courses", HistoryEntry.row_id == a.course_id
        )
    )
    assert {"preparation_factor", "travel_hours", "support_overhead_hours"} <= set(rows)
    lead = sa.select(HistoryEntry.new_value).where(
        HistoryEntry.table_name == "courses",
        HistoryEntry.row_id == c.course_id,
        HistoryEntry.field == "lead_factor",
    )
    assert list(db.scalars(lead)) == ["0,5"]
    from metrics.planning import mine

    assert mine.workload(db, w.kim, TODAY).weeks[1].total == Decimal("11.5")
    html = _as(monkeypatch, "Kim", "kc-kim").get(f"/team?datum={NEXT_MONDAY}").text
    kim = re.search(
        rf'<tr data-person="{w.kim.id}">.*?data-week="{NEXT_MONDAY}" data-hours="([\d.]+)"', html, re.S
    )
    assert Decimal(kim.group(1)) == Decimal("11.5")
    own = dict(re.findall(r'<li data-course="(\d+)">(.*?)</li>', html, re.S))
    assert set(own) == {str(a.course_id), str(b.course_id), str(c.course_id)}
    assert (
        "Vorbereitung 1 × Kurszeit, Zuschlag Unterstützung 2 Std., Fahrtzeit 0 Std. (Kurs)"
        in own[str(a.course_id)]
    )
    assert "Faktor Kursleitung 0,5 × Kurszeit, Zuschlag Kursleitung 0,5 Std. (Kurs)" in own[str(c.course_id)]

    # The course page: own values in the fields, the inherited value and where it comes from.
    page = _as(monkeypatch, "Kim", "kc-kim", first=False).get(f"/planung/{a.course_id}?termin={a.id}").text
    panel = page[page.index('id="arbeitszeit"') :]
    assert 'name="preparation_factor" value="1"' in panel
    assert "eigener Wert; leer: 0,5 × Kurszeit (Standard)" in panel
    assert f"eigener Wert; leer: 1 Std. (Ort „{w.school.name}“)" in panel
    assert 'name="lead_overhead_hours" value=""' in panel and "leer: 1 Std. (Standard)" in panel
    assert "Kursleitung 5,5 Std., Unterstützung 4,2 Std." in panel
    page = _as(monkeypatch, "Kim", "kc-kim", first=False).get(f"/planung/{c.course_id}").text
    assert 'name="lead_factor" value="0,5"' in page and "eigener Wert; leer: 1 × Kurszeit (Standard)" in page
    assert "eigener Wert; leer: 3 Std. (Format „HackDays“)" in page

    # Empty means inherited again; nonsense is refused.
    _own(db, a, preparation_factor="", travel_hours="", support_overhead_hours="")
    assert workload.estimate(a, lanes.LEAD).total == Decimal("5.375")
    with pytest.raises(workflow.FormError, match="keine Zahl"):
        _own(db, a, travel_hours="viel")
    with pytest.raises(workflow.FormError, match="zwischen 0 und 100"):
        _own(db, a, travel_hours="-1")


def test_course_values_stay_editable_when_abgeschlossen(week, monkeypatch):
    """They change no chart, so a counted course keeps them editable (the other fields are locked)."""
    w, t = week
    a = t["a"]
    a.stage = ABGESCHLOSSEN
    w.db.commit()
    client = _as(monkeypatch, "Kim", "kc-kim")
    page = client.get(f"/planung/{a.course_id}?termin={a.id}").text
    assert '<input type="hidden" name="action" value="workload">' in page
    response = client.post(
        f"/planung/{a.course_id}/aktion",
        data={"action": "workload", "termin": a.id, "travel_hours": "2", "place_id": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    w.db.expire_all()
    assert a.course.travel_hours == 2 and a.course.place == w.school  # the place stays (locked)


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
    assert text == f"Kim: in KW {NEXT_MONDAY.isocalendar().week} 16,9 Std. geschätzt, Wochenstunden 10"
    # Moving C to the week after takes Kim's week to 7.875 hours: the hint goes away from the
    # Termine left behind too, and C alone (9 hours) is below the weekly hours.
    c = t["c"]
    later = WEDNESDAY + dt.timedelta(days=7)
    values = {"start_time": "", "end_time": "", "format_id": c.course.format_id, "place_id": w.makerspace.id}
    workflow.save(db, c, w.basics(date=later.isoformat(), **values), "Test")
    db.flush()
    assert c.date == later and flagged() == set()
