"""WM-151: standard views and planning insights, each figure checked against hand-written SQL."""

import datetime as dt
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from metrics import analytics, charts, metrics
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    CONFIRMED,
    DURCHGEFUEHRT,
    GEPLANT,
    Appointment,
    AppointmentEquipment,
    Context,
    Course,
    Equipment,
    Format,
    Person,
    Place,
)
from metrics.instance import Project, load_instance
from metrics.planning import insights, workload
from metrics.web import analyse
from metrics.web import insights as insights_web
from tests.conftest import EXAMPLE_INSTANCE
from tests.test_web import _client

ON = date(2026, 3, 16)
AUG = 8
ALL = metrics.Filters()


@pytest.fixture
def data(db):
    """Termine in every stage at schools, a place without coordinates and without a place, with
    team, equipment, cancellations, creation times and billing."""
    project = Context(name="Projekt A", status=CONFIRMED)
    other = Context(name="Offen", status=CONFIRMED)
    code = Format(name="Scratch", category="Code")
    solder = Format(name="Löten", category="Engineering")
    school_a = Place(
        name="Schule A", is_school=True, lat=48.05, lon=11.10, municipality="Musterstadt", status=CONFIRMED
    )
    school_b = Place(name="Schule B", is_school=True, municipality="Nachbarstadt", status=CONFIRMED)
    lab = Place(name="Makerspace", is_school=False, lat=48.08, lon=11.30, municipality="Beispieldorf")
    anna, ben = Person(name="Anna", status=CONFIRMED), Person(name="Ben", status=CONFIRMED)
    kit = Equipment(name="Koffer", quantity=2, status=CONFIRMED)
    db.add_all([anna, ben, kit])
    db.flush()

    def termin(
        when,
        place=school_a,
        fmt=code,
        context=project,
        stage=ABGESCHLOSSEN,
        level=None,
        age=None,
        group="Schulklasse",
        lead=None,
        support=(),
        source="app",
        created=None,
        billed=False,
        reason=None,
        needs=0,
    ):
        t = Appointment(
            course=Course(
                format=fmt, context=context, place=place, target_group=group, class_level=level, age=age
            ),
            source=source,
            date=when,
            number=1,
            participants_total=10,
            start_time=dt.time(9),
            end_time=dt.time(11),
            duration_hours=Decimal(2),
            stage=stage,
            lead=lead,
            support=list(support),
            billed=billed,
            cancel_reason=reason,
        )
        db.add(t)
        db.flush()
        if created:
            t.created_at = dt.datetime.combine(created, dt.time(12), dt.UTC)
        if needs:
            db.add(AppointmentEquipment(appointment_id=t.id, equipment_id=kit.id, quantity=needs))
        return t

    termin(date(2025, 9, 1), level=5, lead=anna, created=date(2025, 8, 1), needs=1)
    termin(date(2025, 9, 1), place=lab, fmt=solder, level=5, lead=ben, support=[anna], needs=2)
    termin(date(2025, 10, 2), place=school_b, age=12, lead=anna, created=date(2025, 9, 30), billed=True)
    termin(date(2025, 10, 9), place=None, fmt=None, context=None, group="Lehrkräfte", source="baseline")
    termin(date(2025, 11, 5), place=lab, fmt=solder, context=other, stage=DURCHGEFUEHRT, lead=ben)
    termin(date(2025, 11, 6), stage=ABGESAGT, reason="Schule krank", lead=anna, needs=2)
    termin(date(2025, 11, 7), stage=ABGESAGT, reason="  ", created=date(2025, 11, 1))
    termin(date(2025, 12, 1), stage=ABGESAGT, reason="Schule krank", source="slack")
    termin(date(2026, 4, 20), stage=BESTAETIGT, lead=ben, created=date(2026, 4, 25), needs=1)  # entered later
    termin(date(2026, 5, 4), stage=GEPLANT, lead=anna, support=[ben], created=date(2026, 1, 1))
    termin(date(2025, 11, 20), stage=DURCHGEFUEHRT, source="slack")  # slack: billing recorded
    termin(date(2025, 11, 21), stage=DURCHGEFUEHRT, source="legacy-harmonised")  # no billing recorded
    termin(None, stage=GEPLANT, lead=anna)  # no date: never counts
    db.flush()
    return db


JOINS = """
FROM appointments a
JOIN courses c ON c.id = a.course_id
LEFT JOIN formats f ON f.id = c.format_id
LEFT JOIN contexts x ON x.id = c.context_id
LEFT JOIN places p ON p.id = c.place_id
"""
HELD = "a.stage = 'Abgeschlossen' AND a.date IS NOT NULL"


def sql(db, query: str, **params) -> list[tuple]:
    return [tuple(r) for r in db.execute(sa.text(query), params)]


def _stacked(rows) -> set[tuple]:
    return {(g, c, n) for g, c, n in rows}


# ---- Standard views ---------------------------------------------------------------------------


def test_sessions_by_context_and_format_match_sql(data):
    rows, order = analytics.by_context(data, ALL, ON, AUG)
    expected = sql(
        data,
        f"SELECT coalesce(x.name, 'Ohne Kontext'), coalesce(f.category, 'Ohne Kategorie'), count(*) {JOINS}"
        f" WHERE {HELD} GROUP BY 1, 2",
    )
    assert _stacked(rows) == set(expected)
    assert order == ["Projekt A", "Ohne Kontext"]
    rows, order = analytics.by_format(data, ALL, ON, AUG)
    expected = sql(
        data,
        f"SELECT coalesce(f.name, 'Ohne Format'), coalesce(f.category, 'Ohne Kategorie'), count(*) {JOINS}"
        f" WHERE {HELD} GROUP BY 1, 2",
    )
    assert _stacked(rows) == set(expected)
    assert order[0] == "Scratch"


def test_sessions_by_school_match_sql(data):
    rows, order = analytics.by_school(data, ALL, ON, AUG)
    expected = sql(
        data, f"SELECT p.name, f.category, count(*) {JOINS} WHERE {HELD} AND p.is_school GROUP BY 1, 2"
    )
    assert _stacked(rows) == set(expected) == {("Schule A", "Code", 1), ("Schule B", "Code", 1)}
    assert order == ["Schule A", "Schule B"]


def test_sessions_by_age_match_sql(data):
    rows, order = analytics.by_age(data, ALL, ON, AUG)
    expected = sql(
        data,
        f"""SELECT CASE WHEN c.target_group = 'Lehrkräfte' THEN 'Lehrkräfte'
                        WHEN c.class_level IS NOT NULL THEN c.class_level || '. Klasse'
                        WHEN c.age IS NOT NULL THEN c.age || ' Jahre'
                        ELSE coalesce(c.age_band, 'Ohne Angabe') END,
                   coalesce(f.category, 'Ohne Kategorie'), count(*) {JOINS} WHERE {HELD} GROUP BY 1, 2""",
    )
    assert _stacked(rows) == set(expected)
    assert order == ["5. Klasse", "12 Jahre", "Lehrkräfte"]


def test_age_order():
    labels = ["Ohne Angabe", "Lehrkräfte", "25 - 34 Jahre", "12 Jahre", "10. Klasse", "9 Jahre", "5. Klasse"]
    assert sorted(labels, key=analytics._age_key) == [
        "5. Klasse",
        "10. Klasse",
        "9 Jahre",
        "12 Jahre",
        "25 - 34 Jahre",
        "Lehrkräfte",
        "Ohne Angabe",
    ]


def test_cumulative_ends_at_the_key_figure(data):
    rows, _ = analytics.count_by_date_and_category(data, ALL, ON, AUG)
    points = analytics.cumulative(rows)
    expected = sql(
        data,
        f"SELECT a.date, sum(count(*)) OVER (ORDER BY a.date) {JOINS} WHERE {HELD} GROUP BY 1 ORDER BY 1",
    )
    assert points == [(d, int(n)) for d, n in expected]
    assert points[-1][1] == metrics.key_figures(data, ALL, ON, AUG).sessions == 4


def test_place_points_match_sql(data):
    points = analytics.count_by_place_point(data, ALL, ON, AUG)
    expected = sql(
        data,
        f"SELECT p.name, p.lat, p.lon, p.municipality, count(*) {JOINS} WHERE {HELD} GROUP BY 1, 2, 3, 4",
    )
    assert {tuple(p) for p in points} == set(expected)
    spec = charts.place_points(
        load_instance(EXAMPLE_INSTANCE).map, points, load_instance(EXAMPLE_INSTANCE).theme
    )
    circles = spec["layer"][-1]["data"]["values"]
    # Every place with coordinates exactly once, with its count for the tooltip.
    assert [(c["name"], c["count"]) for c in circles] == [("Makerspace", 1), ("Schule A", 1)]


def test_municipality_filter_matches_sql(data):
    filters = metrics.Filters(municipalities=["Musterstadt", "Beispieldorf"])
    figures = metrics.key_figures(data, filters, ON, AUG)
    expected = sql(
        data, f"SELECT count(*) {JOINS} WHERE {HELD} AND p.municipality IN ('Musterstadt', 'Beispieldorf')"
    )
    assert figures.sessions == expected[0][0] == 2


# ---- Planning insights --------------------------------------------------------------------------

PLANNED = "a.stage <> 'Abgesagt' AND a.date IS NOT NULL"


def test_team_hours_match_sql(data, monkeypatch):
    filters = metrics.Filters(preset="custom", date_from=date(2025, 9, 1), date_to=date(2026, 5, 31))
    hours = insights.team_hours(data, filters, ON, AUG)
    assert [b.label for b in hours.bins][:3] == ["September 2025", "Oktober 2025", "November 2025"]
    assert len(hours.bins) == 9
    # Termine per person and month: lead or support on planned Termine.
    expected = sql(
        data,
        f"""SELECT name, month, count(*) FROM (
              SELECT pe.name, date_trunc('month', a.date)::date AS month {JOINS}
              JOIN people pe ON pe.id = a.lead_id WHERE {PLANNED} AND a.date >= '2025-09-01'
              UNION ALL
              SELECT pe.name, date_trunc('month', a.date)::date {JOINS}
              JOIN appointment_support s ON s.appointment_id = a.id JOIN people pe ON pe.id = s.person_id
              WHERE {PLANNED} AND a.date >= '2025-09-01'
            ) t GROUP BY 1, 2""",
    )
    found = {
        (row.person.name, b.start, cell.termine)
        for row in hours.rows
        for b, cell in zip(hours.bins, row.cells, strict=True)
        if cell.termine
    }
    assert found == set(expected)
    # Termine created in the app without a set time were created "today": entered after their date.
    # The hours are the workload rule's estimate: 2 h course time; lead 2 × (1 + 0.5) + 1, support 2 + 0.5.
    september = {row.person.name: row.cells[0] for row in hours.rows}
    assert september["Anna"].lead == Decimal(4) and september["Anna"].support == Decimal("2.5")
    assert september["Ben"].lead == Decimal(4)
    assert hours.totals[0].total == Decimal("10.5")


def test_equipment_usage_matches_sql(data):
    usage = insights.equipment_usage(data, ALL, ON, AUG)
    expected = sql(
        data,
        f"""SELECT e.name, count(DISTINCT a.id), count(DISTINCT a.date), max(day_total),
                   count(DISTINCT a.date) FILTER (WHERE day_total > e.quantity)
            {JOINS} JOIN appointment_equipment ae ON ae.appointment_id = a.id
            JOIN equipment e ON e.id = ae.equipment_id
            JOIN (SELECT a2.date AS day, ae2.equipment_id, sum(ae2.quantity) AS day_total
                  FROM appointments a2 JOIN appointment_equipment ae2 ON ae2.appointment_id = a2.id
                  WHERE a2.stage <> 'Abgesagt' AND a2.date IS NOT NULL GROUP BY 1, 2) d
              ON d.day = a.date AND d.equipment_id = e.id
            WHERE {PLANNED} GROUP BY e.name""",
    )
    assert [(u.item.name, u.termine, u.days, u.peak, u.days_short) for u in usage] == expected
    assert expected == [("Koffer", 3, 2, 3, 1)]  # 1 + 2 on 1 Sept (more than 2), 1 in April; cancelled not


def test_cancellations_match_sql(data):
    result = insights.cancellations(data, ALL, ON, AUG)
    expected = sql(
        data,
        f"SELECT nullif(trim(a.cancel_reason), ''), count(*) {JOINS}"
        " WHERE a.stage = 'Abgesagt' AND a.date IS NOT NULL GROUP BY 1",
    )
    assert dict(result.reasons) == {r or insights.NO_REASON: n for r, n in expected}
    assert result.reasons == [("Schule krank", 2), ("Ohne Grund", 1)]
    planned = sql(data, f"SELECT count(*) {JOINS} WHERE {PLANNED}")[0][0]
    assert (result.cancelled, result.planned) == (3, planned)
    assert result.share == pytest.approx(3 / (3 + planned))


def test_lead_time_matches_sql(data):
    result = insights.lead_time(data, ALL, ON, AUG)
    days = [
        d
        for (d,) in sql(
            data,
            f"SELECT a.date - (a.created_at AT TIME ZONE 'Europe/Berlin')::date {JOINS}"
            f" WHERE {PLANNED} AND a.source = 'app'",
        )
    ]
    assert result.count == len(days)
    bands = dict(result.bands)
    assert bands["Nachgetragen (nach dem Termin angelegt)"] == sum(1 for d in days if d < 0) == 3
    assert bands["8 Wochen und mehr"] == sum(1 for d in days if d >= 56) == 1  # Geplant 4 May, made 1 Jan
    assert bands["4 bis 8 Wochen"] == 1  # 1 Sept, made 1 Aug
    assert sum(bands.values()) == result.count


def test_unbilled_matches_sql(data):
    found = [t.id for t in insights.unbilled(data, ALL, ON, AUG)]
    expected = [
        i
        for (i,) in sql(
            data,
            f"SELECT a.id {JOINS} WHERE a.stage IN ('Durchgeführt', 'Abgeschlossen') AND a.date IS NOT NULL"
            " AND a.source IN ('app', 'slack') AND NOT a.billed ORDER BY a.date, a.id",
        )
    ]
    assert found == expected
    assert len(found) == 4  # 1 Sept (2), 5 Nov, 20 Nov (Slack); not billed 2 Oct, baseline, legacy


def test_planned_is_the_workload_rule(data):
    """The planning insights and the workload table count the same Termine."""
    from sqlalchemy import select

    query, _ = metrics.select_planned(select(Appointment), ALL, ON, AUG)
    everything = list(data.scalars(select(Appointment)))
    assert {t.id for t in data.scalars(query)} == {t.id for t in workload.counted(everything)}


# ---- Charts -------------------------------------------------------------------------------------


def test_small_counts_get_integer_ticks(instance):
    from metrics.analytics import Segment

    def axis(count):
        segment = Segment(date(2025, 1, 6), date(2025, 1, 13), "KW 2/2025", "Code", count)
        return charts.sessions_over_time([segment], "week", instance.theme, "")["encoding"]["y"]["axis"]

    assert axis(2)["values"] == [0, 1, 2]
    assert "values" not in axis(10)  # larger counts keep Vega's own ticks (output unchanged)


def test_map_legend_above_and_aspect(instance):
    counts = analytics.MunicipalityCounts(
        inside={m.name: 1 for m in instance.map.municipalities}, outside=0, unassigned=0
    )
    spec = charts.municipality_map(instance.map, counts, instance.theme)
    assert spec["layer"][0]["encoding"]["color"]["legend"]["orient"] == "top"
    assert spec["title"]["orient"] == "bottom"  # the attribution stays below, away from the legend
    assert 0.4 <= spec["usermeta"]["aspectRatio"] <= 3
    assert charts.mercator_aspect(points=[(11.0, 48.0), (12.0, 48.01)]) == 3.0  # clamped
    assert charts.mercator_aspect() == 1.0


# ---- Pages and endpoints ------------------------------------------------------------------------


@pytest.fixture
def team(data, monkeypatch):
    data.commit()
    monkeypatch.setattr(analyse, "today", lambda: ON)
    monkeypatch.setattr(insights_web, "today", lambda: ON)
    return _client(monkeypatch, auth_mode="off")


def test_view_endpoints(team, data):
    for dimension in ("kontext", "format", "schule", "alter"):
        assert team.get(f"/api/analyse/by/{dimension}").json()["meta"]["count"] > 0
    assert team.get("/api/analyse/by/person").status_code == 404
    assert team.get("/api/analyse/cumulative").json()["meta"]["count"] == 4
    places = team.get("/api/analyse/places").json()["meta"]
    assert (places["located"], places["unlocated"]) == (2, 2)
    assert places["unlocated_places"] == [{"name": "Ohne Ort", "count": 1}, {"name": "Schule B", "count": 1}]
    # A click on Musterstadt filters every view and tile by the place's municipality.
    tiles = team.get("/api/analyse/key-figures?municipality=Musterstadt").json()["figures"]
    assert tiles["sessions"]["value"] == 1
    page = team.get("/analyse").text
    for tab in ("kumuliert", "schulen", "kontexte", "alter", "orte"):
        assert f'id="tab-{tab}"' in page
    assert 'data-click="period category"' in page and 'name="municipality"' in page


def test_front_page_ignores_the_municipality(team):
    whole = team.get("/api/sessions-over-time").json()["meta"]["count"]
    assert team.get("/api/sessions-over-time?municipality=Musterstadt").json()["meta"]["count"] == whole


def test_planning_page(team):
    page = team.get("/analyse/planung").text
    assert 'value="this_school_year" selected' in page  # the default period
    assert "Arbeitszeit des Teams" in page and "Offene Abrechnung" in page
    page = team.get("/analyse/planung?period=all").text
    assert ">Anna</th>" in page and "Schule krank" in page and "Koffer" in page
    chart = team.get("/api/analyse/planung/stunden?period=all").json()
    assert chart["meta"]["count"] == 8  # lead or support on planned, dated Termine
    assert {v["role"] for v in chart["spec"]["data"]["values"]} == {"Leitung", "Unterstützung"}


def test_planning_insights_need_login(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/analyse/planung/stunden").status_code == 401
    response = client.get("/analyse/planung", follow_redirects=False)
    assert response.headers["location"] == "/login"


def test_school_views_only_where_a_termin_is_at_a_school(data):
    """Schularten and Schulen are hidden where they could only be empty, e.g. on the JugendLab page."""
    has = analyse._has_school_termine
    assert has(data, None)
    assert has(data, Project("projekt-a", "Projekt A", ("Projekt A",)))
    # "Offen" has one Termin, at the Makerspace (no school): nothing to show.
    assert not has(data, Project("offen", "Offen", ("Offen",)))
    assert not has(data, Project("leer", "Leer", ("Gibt es nicht",)))
