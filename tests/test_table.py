"""WM-152: the Tabelle and its CSV. PostgreSQL with made-up demo data only.

The Done-when check: a project lead answers "how many pupils in gDigital per school in 2025/26" from
the table, i.e. filters the context and the school year, keeps only schools, sums per place, and the
sums match a hand-written SQL query with the participant rule of `metrics.metrics`.
"""

import csv
import io
from datetime import date, time
from decimal import Decimal

import pytest
import sqlalchemy as sa

from metrics import metrics, table
from metrics.db import (
    ABGESCHLOSSEN,
    CONFIRMED,
    DURCHGEFUEHRT,
    Appointment,
    Contact,
    Context,
    Course,
    Format,
    Person,
    Place,
    SchoolType,
)
from metrics.web import tabelle
from tests.test_web import _client

TODAY = date(2026, 11, 16)  # "Letztes Schuljahr" is 2025/26
AUG = 8
# A made-up contact and a made-up note; each value is unusual enough to be found anywhere it leaks.
TRACES = ("Zettelberger", "zettel@schule.example", "987654", "Privatnotiz-4711")


@pytest.fixture
def termine(db):
    """Held Termine of gDigital and another context over two school years, at schools and elsewhere."""
    # The migration seeds the school types, but every test empties all tables afterwards.
    db.merge(SchoolType(code="GS", label="Grundschule", position=1))
    db.merge(SchoolType(code="MS", label="Mittelschule", position=2))
    gdigital = Context(name="gDigital", is_project=True, page_slug="gdigital", status=CONFIRMED)
    other = Context(name="Ferien", status=CONFIRMED)
    code = Format(name="Scratch", category="Code", status=CONFIRMED)
    lab = Format(name="JugendLab", category="Science", continuous=True, status=CONFIRMED)
    school_a = Place(name="Grundschule Aberg", is_school=True, school_type="GS", status=CONFIRMED)
    school_b = Place(name="Mittelschule Bedorf", is_school=True, school_type="MS", status=CONFIRMED)
    space = Place(name="Makerspace", is_school=False, status=CONFIRMED)
    kim = Person(name="Kim Muster", status=CONFIRMED)
    lea = Person(name="Lea Beispiel", status=CONFIRMED)
    contact = Contact(
        place=school_a, name="Frau Zettelberger", email="zettel@schule.example", phone="0123 987654"
    )

    def course(place, context=gdigital, fmt=code, **extra):
        return Course(format=fmt, context=context, place=place, target_group="Schulklasse", **extra)

    a = course(school_a, contact=contact, note="Privatnotiz-4711", class_age_text="Kl. 3 / 8 Jahre")
    b = course(school_b)

    def termin(key, crs, when, number, total, stage=ABGESCHLOSSEN, src="t", hours="2", **extra):
        return Appointment(
            course=crs,
            source=src,
            source_key=None if src == "app" else key,
            date=when,
            number=number,
            participants_total=total,
            duration_hours=Decimal(hours),
            stage=stage,
            **extra,
        )

    db.add_all(
        [
            # 2025/26, gDigital at two schools: a series of three dates at Aberg counts its 22 once.
            termin(
                "a1",
                a,
                date(2025, 9, 1),
                1,
                22,
                lead=kim,
                support=[lea],
                start_time=time(8),
                end_time=time(10),
            ),
            termin("a2", a, date(2025, 9, 8), 2, 21, lead=kim),
            termin("a3", a, date(2025, 9, 15), 3, 22, lead=kim),
            termin("a4", course(school_a), date(2026, 2, 3), 1, 18, lead=lea),
            termin("b1", b, date(2025, 10, 6), 1, 25, lead=lea),
            termin("b2", b, date(2025, 10, 13), 2, 24, lead=lea),
            # gDigital outside a school, in another context, not held, no number, earlier year.
            termin("s1", course(space), date(2025, 11, 5), 1, 9),
            termin("f1", course(school_b, context=other), date(2025, 12, 1), 1, 30),
            termin("x1", course(school_a), date(2026, 3, 3), 1, 40, stage=DURCHGEFUEHRT),
            termin("n1", course(school_b), date(2026, 1, 20), None, 11),
            termin("o1", course(school_a), date(2024, 10, 1), 1, 19),
            # A continuous format made in the app counts at every number.
            termin("l1", course(space, fmt=lab), date(2025, 11, 12), 1, 8, src="app"),
            termin("l2", course(space, fmt=lab), date(2025, 11, 19), 2, 6, src="app"),
        ]
    )
    db.flush()
    return db


# Participants per school, written from the definition (not from the code): held Termine are
# abgeschlossen and dated; participants count on Terminnummer 1, or on every numbered Termin made in the
# app for a continuous format.
PER_SCHOOL = """
SELECT p.name,
  count(*) AS sessions,
  coalesce(sum(a.participants_total) FILTER (
    WHERE a.number = 1 OR (a.source = 'app' AND f.continuous AND a.number IS NOT NULL)), 0) AS participants
FROM appointments a
JOIN courses c ON c.id = a.course_id
LEFT JOIN formats f ON f.id = c.format_id
LEFT JOIN contexts x ON x.id = c.context_id
JOIN places p ON p.id = c.place_id
WHERE a.stage = 'Abgeschlossen' AND a.date BETWEEN :start AND :end AND x.name = :context AND p.is_school
GROUP BY p.name ORDER BY p.name
"""


@pytest.fixture
def team(termine, monkeypatch):
    termine.commit()
    monkeypatch.setattr(tabelle, "today", lambda: TODAY)
    return _client(monkeypatch, auth_mode="off")


def _csv(response) -> list[list[str]]:
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.content.startswith(b"\xef\xbb\xbf")  # UTF-8 byte order mark for Excel
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))


def test_pupils_in_gdigital_per_school_in_2025_26(team, termine):
    """Done when (WM-152): the sums per school match SQL with the participant rule of `metrics`."""
    query = "period=last_school_year&context=gDigital&schulen=1&gruppe=ort"
    expected = termine.execute(
        sa.text(PER_SCHOOL), {"start": date(2025, 8, 1), "end": date(2026, 7, 31), "context": "gDigital"}
    ).all()
    assert [tuple(r) for r in expected] == [("Grundschule Aberg", 4, 40), ("Mittelschule Bedorf", 3, 25)]

    sums = _csv(team.get(f"/tabelle.csv?{query}&ansicht=summen"))
    assert sums[0] == ["Ort", "Einsätze", "Teilnehmende (gezählt)", "Teilnehmende (erfasst)", "Stunden"]
    assert [(r[0], int(r[1]), int(r[2])) for r in sums[1:-1]] == [tuple(r) for r in expected]
    assert sums[-1][:3] == ["Gesamt", "7", "65"]

    # The page shows the same sums, and the period in words.
    page = team.get(f"/tabelle?{query}").text
    assert "Summen je Ort" in page
    assert "Schuljahr 2025/26 (01.08.2025–31.07.2026)" in page
    assert "Letztes Schuljahr (2025/26)" in page
    for name, sessions, participants in expected:
        assert (
            f'<th scope="row">{name}</th><td class="num">{sessions}</td><td class="num">{participants}</td>'
            in page
        )

    # Sorted by place, the rows' counted column adds up to the same sums.
    rows = _csv(
        team.get(f"/tabelle.csv?{query}&sortierung=ort&spalte=ort&spalte=terminnummer&spalte=tn_gezaehlt")
    )
    assert rows[0] == ["Terminnummer", "Ort", "Teilnehmende (gezählt)"]  # in the table's column order
    places = [r[1] for r in rows[1:]]
    assert places == sorted(places, key=str.casefold)
    per_place = {}
    for _, place, counted in rows[1:]:
        per_place[place] = per_place.get(place, 0) + int(counted)
    assert per_place == {name: participants for name, _, participants in expected}


SELECTIONS = [
    ("", metrics.Filters()),
    ("period=last_school_year", metrics.Filters(preset="last_school_year")),
    (
        "period=last_school_year&context=gDigital",
        metrics.Filters(preset="last_school_year", contexts=["gDigital"]),
    ),
    ("category=Science", metrics.Filters(categories=["Science"])),
    (
        "period=custom&from=2025-09-01&to=2025-11-30",
        metrics.Filters(preset="custom", date_from=date(2025, 9, 1), date_to=date(2025, 11, 30)),
    ),
]


@pytest.mark.parametrize(("query", "filters"), SELECTIONS)
def test_counted_column_sums_to_the_kpi_tile(team, termine, query, filters):
    figures = metrics.key_figures(termine, filters, TODAY, AUG)
    rows = _csv(team.get(f"/tabelle.csv?{query}&spalte=tn_gezaehlt&spalte=dauer"))
    assert len(rows) - 1 == figures.sessions
    assert sum(int(r[1]) for r in rows[1:]) == figures.participants
    assert sum(Decimal(r[0].replace(",", ".")) for r in rows[1:]) == figures.hours


def test_rows_with_all_columns(team, termine):
    found, _ = table.rows(
        termine, metrics.Filters(preset="last_school_year"), table.Choice(sort="ort"), TODAY, AUG
    )
    first = next(r for r in found if r.values["datum"] == date(2025, 9, 1))
    assert first.values | {"kurs": None} == {
        "datum": date(2025, 9, 1),
        "beginn": time(8),
        "ende": time(10),
        "dauer": Decimal("2.00"),
        "terminnummer": 1,
        "kurs": None,
        "format": "Scratch",
        "kategorie": "Code",
        "kontext": "gDigital",
        "zielgruppe": "Schulklasse",
        "klasse": "Kl. 3 / 8 Jahre",
        "ort": "Grundschule Aberg",
        "schule": True,
        "schulart": "Grundschule",
        "gemeinde": None,
        "leitung": "Kim Muster",
        "unterstuetzung": "Lea Beispiel",
        "tn_gezaehlt": 22,
        "tn_erfasst": 22,
        "weiblich": None,
        "maennlich": None,
        "divers": None,
        "keine_angabe": None,
        "abgerechnet": False,
    }
    second = next(r for r in found if r.values["datum"] == date(2025, 9, 8))
    assert (second.values["tn_gezaehlt"], second.values["tn_erfasst"]) == (0, 21)
    # Sorted by place; within a place by date.
    assert [r.values["ort"] for r in found] == sorted((r.values["ort"] for r in found), key=str.casefold)

    every = "&".join(f"spalte={c.key}" for c in table.COLUMNS)
    rows = _csv(team.get(f"/tabelle.csv?period=last_school_year&{every}"))
    assert rows[0] == [c.label for c in table.COLUMNS]
    line = next(r for r in rows if r[0] == "01.09.2025")
    assert line[:4] == ["01.09.2025", "08:00", "10:00", "2"]
    assert line[-1] == "nein"


def test_contacts_and_notes_never_appear(team, termine):
    every = "&".join(f"spalte={c.key}" for c in table.COLUMNS)
    for path in (
        f"/tabelle?{every}",
        f"/tabelle?{every}&gruppe=ort",
        f"/tabelle.csv?{every}",
        f"/tabelle.csv?{every}&gruppe=ort&ansicht=summen",
    ):
        response = team.get(path)
        assert response.status_code == 200
        assert "Grundschule Aberg" in response.text, path
        assert not any(x in response.text for x in TRACES), path


def test_csv_cells_cannot_start_a_formula(team, termine):
    place = termine.scalar(sa.select(Place).where(Place.name == "Makerspace"))
    place.name = "=HYPERLINK(1)"
    termine.scalar(sa.select(Format).where(Format.name == "JugendLab")).name = "+Lab"
    termine.scalar(sa.select(Person).where(Person.name == "Kim Muster")).name = "@Kim"
    termine.scalar(sa.select(Context).where(Context.name == "Ferien")).name = "-Ferien"
    termine.commit()
    text = team.get("/tabelle.csv?spalte=ort&spalte=format&spalte=leitung&spalte=kontext").text
    for cell in ("'=HYPERLINK(1)", "'+Lab", "'@Kim", "'-Ferien"):
        assert cell in text
    assert ";=" not in text and ";+" not in text and ";@" not in text and ";-" not in text
    sums = team.get("/tabelle.csv?gruppe=ort&ansicht=summen").text
    assert "'=HYPERLINK(1)" in sums


@pytest.mark.parametrize(
    ("cell", "safe"),
    [
        ("=1+1", "'=1+1"),
        ("+49 123", "'+49 123"),
        ("-x", "'-x"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tTab", "'\tTab"),
        ("\rCR", "'\rCR"),
        ("Schule", "Schule"),
        ("", ""),
    ],
)
def test_csv_safe(cell, safe):
    assert table.csv_safe(cell) == safe


def test_choice_falls_back_to_the_defaults():
    from starlette.datastructures import QueryParams

    choice = table.parse_choice(QueryParams("spalte=gibts-nicht&sortierung=x&gruppe=y&absteigend=1"))
    assert choice == table.Choice(descending=True)
    choice = table.parse_choice(QueryParams("spalte=ort&spalte=ort&spalte=datum&schulen=1&gruppe=schuljahr"))
    assert (choice.columns, choice.schools_only, choice.group) == (["datum", "ort"], True, "schuljahr")


def test_groups_by_school_year_in_time_order(team, termine):
    sums = _csv(team.get("/tabelle.csv?context=gDigital&gruppe=schuljahr&ansicht=summen"))
    assert [r[0] for r in sums[1:]] == ["2024/25", "2025/26", "Gesamt"]


def test_descending_puts_empty_values_last(team, termine):
    rows = _csv(team.get("/tabelle.csv?sortierung=terminnummer&absteigend=1&spalte=terminnummer"))
    numbers = [r[0] for r in rows[1:]]
    assert numbers[0] == "3" and numbers[-1] == ""


def test_the_table_needs_a_login(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/tabelle", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/tabelle.csv").status_code == 401
