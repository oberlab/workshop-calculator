"""WM-150: the metric definitions (`metrics.metrics`) and the KPI tiles, each checked against SQL."""

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa

from metrics import metrics
from metrics.db import ABGESCHLOSSEN, CONFIRMED, DURCHGEFUEHRT, Appointment, Context, Course, Format, Place
from metrics.web import analyse
from tests.test_web import _client

TODAY = date(2026, 11, 16)
ON = date(2026, 3, 16)  # "today" for the database tests: in the school year 2025/26
AUG = 8


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        ("all", None),
        ("this_school_year", (date(2025, 8, 1), date(2026, 7, 31))),
        ("last_school_year", (date(2024, 8, 1), date(2025, 7, 31))),
        ("this_year", (date(2025, 1, 1), date(2025, 12, 31))),
        ("last_year", (date(2024, 1, 1), date(2024, 12, 31))),
        ("last_12_months", (date(2024, 12, 1), date(2025, 11, 30))),
    ],
)
def test_previous_period_of_the_presets(preset, expected):
    assert metrics.previous_period(preset, TODAY, AUG) == expected


def test_previous_period_of_a_custom_range():
    # Same length (10 days), ending the day before.
    assert metrics.previous_period("custom", TODAY, AUG, date(2025, 3, 11), date(2025, 3, 20)) == (
        date(2025, 3, 1),
        date(2025, 3, 10),
    )
    # Across a month and a leap day: 2024-03-01..2024-03-31 is 31 days.
    assert metrics.previous_period("custom", TODAY, AUG, date(2024, 3, 1), date(2024, 3, 31)) == (
        date(2024, 1, 30),
        date(2024, 2, 29),
    )
    assert metrics.previous_period("custom", TODAY, AUG, date(2025, 3, 1), None) is None
    assert metrics.previous_period("custom", TODAY, AUG, None, date(2025, 3, 1)) is None
    assert metrics.previous_period("custom", TODAY, AUG, date(2025, 3, 2), date(2025, 3, 1)) is None


@pytest.fixture
def termine(db):
    """Held Termine in two school years at schools and other places, with hours and the edge cases."""
    project = Context(name="Projekt A", is_project=True, page_slug="projekt-a", status=CONFIRMED)
    other = Context(name="Offen", status=CONFIRMED)
    code = Format(name="Scratch", category="Code")
    lab = Format(name="JugendLab", category="Science", continuous=True)
    school_a = Place(name="Schule A", is_school=True, status=CONFIRMED)
    school_b = Place(name="Schule B", is_school=True, status=CONFIRMED)
    lab_place = Place(name="Makerspace", is_school=False, status=CONFIRMED)
    unknown = Place(name="Irgendwo", is_school=None, status=CONFIRMED)  # not described: not a school

    def termin(
        key, when, place, number, total, hours, fmt=code, context=project, stage=ABGESCHLOSSEN, src="t"
    ):
        return Appointment(
            course=Course(format=fmt, context=context, place=place, target_group="Schulklasse"),
            source=src,
            source_key=None if src == "app" else key,
            date=when,
            number=number,
            participants_total=total,
            duration_hours=None if hours is None else Decimal(hours),
            stage=stage,
        )

    db.add_all(
        [
            # School year 2025/26 (from 2025-08-01).
            termin("a", date(2025, 9, 1), school_a, 1, 20, "3"),
            termin("b", date(2025, 9, 8), school_a, 2, 20, "3"),  # second date: participants not again
            termin("c", date(2025, 10, 1), school_b, 1, 25, "1.5"),
            termin("d", date(2025, 11, 5), lab_place, 1, 8, "2.25", fmt=lab, src="app"),
            termin("e", date(2025, 11, 12), lab_place, 2, 6, "2.25", fmt=lab, src="app"),  # continuous
            termin("f", date(2026, 1, 20), unknown, None, 9, None),  # no number, no Dauer
            termin("g", date(2026, 2, 2), school_b, 1, 12, "2", context=other),
            termin("h", date(2026, 3, 3), school_a, 1, 40, "5", stage=DURCHGEFUEHRT),  # not held yet
            termin("i", None, school_a, 1, 40, "5"),  # no date: not counted
            # School year 2024/25.
            termin("j", date(2024, 9, 1), school_a, 1, 18, "3"),
            termin("k", date(2025, 2, 1), lab_place, 1, 7, "2", fmt=lab, src="app"),
            termin("l", date(2025, 6, 30), unknown, 1, 5, "1"),
            # Earlier.
            termin("m", date(2023, 5, 5), school_b, 1, 30, "4"),
        ]
    )
    db.flush()
    return db


# The key figures in plain SQL, written from the definitions (not from the code): held Termine are
# abgeschlossen and dated; participants count on Terminnummer 1, or on every numbered Termin made in the
# app for a continuous format; hours are the sum of the Dauer; schools are distinct places marked as
# schools.
SQL = """
SELECT
  count(*) AS sessions,
  coalesce(sum(a.participants_total) FILTER (
    WHERE a.number = 1 OR (a.source = 'app' AND f.continuous AND a.number IS NOT NULL)), 0) AS participants,
  count(DISTINCT c.place_id) AS places,
  coalesce(sum(a.duration_hours), 0) AS hours,
  count(DISTINCT c.place_id) FILTER (WHERE p.is_school) AS schools
FROM appointments a
JOIN courses c ON c.id = a.course_id
LEFT JOIN formats f ON f.id = c.format_id
LEFT JOIN contexts x ON x.id = c.context_id
LEFT JOIN places p ON p.id = c.place_id
WHERE a.stage = 'Abgeschlossen' AND a.date IS NOT NULL
  AND (CAST(:start AS date) IS NULL OR a.date >= :start)
  AND (CAST(:end AS date) IS NULL OR a.date <= :end)
  AND (CAST(:category AS text) IS NULL OR f.category = :category)
  AND (CAST(:context AS text) IS NULL OR x.name = :context)
"""


def sql_figures(db, start=None, end=None, category=None, context=None) -> dict:
    row = db.execute(sa.text(SQL), {"start": start, "end": end, "category": category, "context": context})
    return dict(row.mappings().one())


SELECTIONS = [
    # (query string, filters, period, previous period)
    ("", metrics.Filters(), (None, None), None),
    (
        "period=this_school_year",
        metrics.Filters(preset="this_school_year"),
        (date(2025, 8, 1), date(2026, 7, 31)),
        (date(2024, 8, 1), date(2025, 7, 31)),
    ),
    (
        "period=last_school_year",
        metrics.Filters(preset="last_school_year"),
        (date(2024, 8, 1), date(2025, 7, 31)),
        (date(2023, 8, 1), date(2024, 7, 31)),
    ),
    (
        "period=this_year",
        metrics.Filters(preset="this_year"),
        (date(2026, 1, 1), date(2026, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
    ),
    (
        "period=last_12_months",
        metrics.Filters(preset="last_12_months"),
        (date(2025, 4, 1), date(2026, 3, 31)),
        (date(2024, 4, 1), date(2025, 3, 31)),
    ),
    (
        "period=custom&from=2025-09-01&to=2025-11-30&category=Code",
        metrics.Filters(
            preset="custom", date_from=date(2025, 9, 1), date_to=date(2025, 11, 30), categories=["Code"]
        ),
        (date(2025, 9, 1), date(2025, 11, 30)),
        (date(2025, 6, 2), date(2025, 8, 31)),
    ),
    (
        "period=this_school_year&context=Projekt+A",
        metrics.Filters(preset="this_school_year", contexts=["Projekt A"]),
        (date(2025, 8, 1), date(2026, 7, 31)),
        (date(2024, 8, 1), date(2025, 7, 31)),
    ),
    (
        "period=custom&from=2025-01-01",
        metrics.Filters(preset="custom", date_from=date(2025, 1, 1)),
        None,
        None,
    ),
]


def _sql_for(db, filters: metrics.Filters, period) -> dict:
    start, end = period or (filters.date_from, filters.date_to)
    category = filters.categories[0] if filters.categories else None
    context = filters.contexts[0] if filters.contexts else None
    return sql_figures(db, start, end, category, context)


@pytest.mark.parametrize(("query", "filters", "period", "before"), SELECTIONS)
def test_key_figures_match_sql(termine, query, filters, period, before):
    result = metrics.compare(termine, filters, ON, AUG)
    assert result.figures.__dict__ == _sql_for(termine, filters, period)
    assert result.previous_period == before
    if before is None:
        assert result.previous is None
    else:
        assert result.previous.__dict__ == _sql_for(termine, filters, before)
    # The front page's key figures are the same three numbers.
    totals = metrics.totals(termine, filters, ON, AUG)
    assert (totals.sessions, totals.participants, totals.locations) == (
        result.figures.sessions,
        result.figures.participants,
        result.figures.places,
    )


def test_key_figures_by_hand(termine):
    """The fixture's figures worked out by hand, so the SQL above is checked as well."""
    this_year = metrics.key_figures(termine, metrics.Filters(preset="this_school_year"), ON, AUG)
    # a b c d e f g held; participants a 20 + c 25 + d 8 + e 6 (continuous, app) + g 12; f has no number.
    assert this_year == metrics.KeyFigures(
        sessions=7, participants=71, places=4, hours=Decimal("14.00"), schools=2
    )
    last_year = metrics.key_figures(termine, metrics.Filters(preset="last_school_year"), ON, AUG)
    assert last_year == metrics.KeyFigures(
        sessions=3, participants=30, places=3, hours=Decimal("6.00"), schools=1
    )


@pytest.fixture
def team(termine, monkeypatch):
    termine.commit()
    monkeypatch.setattr(analyse, "today", lambda: ON)
    return _client(monkeypatch, auth_mode="off")


@pytest.mark.parametrize(("query", "filters", "period", "before"), SELECTIONS)
def test_tiles_match_sql(team, termine, query, filters, period, before):
    """Done when (WM-150): every tile matches a hand-written SQL query."""
    data = team.get(f"/api/analyse/key-figures?{query}").json()
    now = _sql_for(termine, filters, period)
    then = _sql_for(termine, filters, before) if before else None
    assert set(data["figures"]) == {"sessions", "participants", "places", "hours", "schools"}
    for key, tile in data["figures"].items():
        assert tile["value"] == float(now[key])
        if then is None:
            assert tile["previous"] is None and tile["change"] is None
        else:
            assert tile["previous"] == float(then[key])
            assert tile["change"] == float(now[key] - then[key])
    if before:
        assert (data["previous"]["from"], data["previous"]["to"]) == tuple(d.isoformat() for d in before)
    else:
        assert data["previous"] is None


def test_previous_period_labels(team):
    def label(query):
        return team.get(f"/api/analyse/key-figures?{query}").json()["previous"]["label"]

    assert label("period=this_school_year") == "Schuljahr 2024/25"
    assert label("period=last_year") == "2024"
    assert label("period=last_12_months") == "01.04.2024–31.03.2025"
    assert label("period=custom&from=2025-09-01&to=2025-11-30") == "02.06.2025–31.08.2025"


def test_a_running_period_is_marked(team):
    """This school year is not over on ON, so the page says it is compared with a whole year."""

    def running(query):
        return team.get(f"/api/analyse/key-figures?{query}").json()["running_until"]

    assert running("period=this_school_year") == "2026-07-31"
    assert running("period=last_school_year") is None
    assert running("") is None


def test_project_page_tiles_are_limited_to_the_project(team, termine):
    page = team.get("/projekte/projekt-a").text
    assert 'id="kpis"' in page and 'data-api="/api/analyse/key-figures"' in page
    # The page sends its contexts with every data request (data-fixed), the tiles included.
    data = team.get("/api/analyse/key-figures?period=this_school_year&context=Projekt+A").json()
    assert data["figures"]["sessions"]["value"] == 6  # without "g" in the context "Offen"


def test_tiles_on_the_analysis_page_with_their_definitions(team):
    page = team.get("/analyse").text
    for key in ("sessions", "participants", "places", "hours", "schools"):
        assert f'data-kpi="{key}"' in page
    assert "Schulen erreicht" in page and "So wird gezählt" in page
    assert "Nicht die geschätzte Arbeitszeit des Teams" in page
    # The tiles replace the three figures below the chart on the analysis page.
    assert 'id="total-sessions"' not in page


def test_front_page_keeps_its_three_figures(team):
    page = team.get("/").text
    assert (
        'id="total-sessions"' in page and 'id="total-participants"' in page and 'id="total-locations"' in page
    )
    assert "data-kpi" not in page and "key-figures" not in page and "Schulen erreicht" not in page


def test_tiles_need_login(monkeypatch):
    assert _client(monkeypatch).get("/api/analyse/key-figures").status_code == 401
