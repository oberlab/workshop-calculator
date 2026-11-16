"""WM-134: the Herkunft map (participants per home municipality) on project pages."""

from datetime import date

import pytest
import sqlalchemy as sa

from metrics import analytics, charts, metrics
from metrics.db import (
    ABGESCHLOSSEN,
    CONFIRMED,
    GEPLANT,
    Appointment,
    AppointmentOrigin,
    Context,
    Course,
    Format,
    Place,
)
from tests.test_web import _client, _noop

TODAY = date(2026, 11, 16)


@pytest.fixture
def termine(db):
    """Held and planned Termine in two contexts; the example map has Beispieldorf, Musterstadt, Neudorf."""
    youth = Context(name="Projekt A", is_project=True, page_slug="projekt-a", status=CONFIRMED)
    school = Context(name="Schulkooperation", status=CONFIRMED)
    lab = Format(name="JugendLab", category="Code", continuous=True)
    workshop = Format(name="Löten", category="Science")
    place = Place(name="Makerspace", municipality="Beispieldorf", status=CONFIRMED)

    def termin(key, context, fmt, when, number, total, origins, stage=ABGESCHLOSSEN, source="app"):
        return Appointment(
            course=Course(context=context, format=fmt, place=place, target_group="Jugendliche"),
            source=source,
            source_key=None if source == "app" else key,
            date=when,
            number=number,
            participants_total=total,
            stage=stage,
            origins=[AppointmentOrigin(municipality=m, participants=n) for m, n in origins.items()],
        )

    db.add_all(
        [
            termin("a", youth, lab, date(2025, 3, 1), 1, 10, {"Musterstadt": 6, "Beispieldorf": 4}),
            # Continuous format made in the app: every number counts.
            termin("b", youth, lab, date(2025, 3, 8), 2, 8, {"Musterstadt": 5, "Fernstadt": 3}),
            # Herkunft covers only part of the total: 2 without Herkunft.
            termin("c", youth, lab, date(2025, 4, 1), 3, 7, {"Neudorf": 5}),
            termin("d", youth, lab, date(2025, 5, 1), 4, 9, {}),  # no Herkunft at all
            termin("e", youth, lab, date(2025, 6, 1), 5, 30, {"Musterstadt": 30}, stage=GEPLANT),  # not held
            termin("f", youth, lab, None, 6, 4, {"Musterstadt": 4}),  # no date: not counted
            termin("g", youth, lab, date(2024, 5, 1), 1, 3, {"Neudorf": 3}),  # outside the period below
            # Not a continuous format: a second Termin adds no participants (key-figure rule).
            termin("h", youth, workshop, date(2025, 3, 2), 1, 6, {"Beispieldorf": 6}),
            termin("i", youth, workshop, date(2025, 3, 3), 2, 6, {"Beispieldorf": 6}),
            # Another context: filtered out on the project page.
            termin("j", school, workshop, date(2025, 3, 4), 1, 11, {"Musterstadt": 11}),
        ]
    )
    db.flush()
    return db


PROJECT = metrics.Filters(
    preset="custom", date_from=date(2025, 1, 1), date_to=date(2025, 12, 31), contexts=["Projekt A"]
)

# The same selection in plain SQL: held (abgeschlossen, dated) Termine in the period and context whose
# participants count in the key figures (number 1, or every number of an app Termin of a continuous
# format).
SQL_ORIGINS = """
SELECT o.municipality, sum(o.participants)
FROM appointment_origins o
JOIN appointments a ON a.id = o.appointment_id
JOIN courses c ON c.id = a.course_id
LEFT JOIN formats f ON f.id = c.format_id
LEFT JOIN contexts x ON x.id = c.context_id
WHERE a.stage = 'Abgeschlossen' AND a.date IS NOT NULL
  AND a.date BETWEEN '2025-01-01' AND '2025-12-31'
  AND x.name = 'Projekt A'
  AND (a.number = 1 OR (a.source = 'app' AND f.continuous AND a.number IS NOT NULL))
GROUP BY o.municipality
"""


def test_origin_sums_match_sql(termine, instance):
    rows = analytics.count_participants_by_origin(termine, PROJECT, TODAY, 9)
    expected = dict(termine.execute(sa.text(SQL_ORIGINS)).all())
    assert {r.municipality: r.count for r in rows if r.municipality} == expected
    assert expected == {"Musterstadt": 11, "Beispieldorf": 10, "Fernstadt": 3, "Neudorf": 5}

    counts = analytics.by_municipality(rows, instance)
    assert counts.inside == {"Beispieldorf": 10, "Musterstadt": 11, "Neudorf": 5}
    assert counts.outside == 3  # Fernstadt is not on the example map
    assert counts.unassigned == 2 + 9  # the part of "c" without Herkunft, and all of "d"
    # Together they are exactly the participants key figure for the same selection.
    total = metrics.totals(termine, PROJECT, TODAY, 9).participants
    assert sum(counts.inside.values()) + counts.outside + counts.unassigned == total


def test_origins_follow_the_time_controls(termine):
    everything = metrics.Filters(contexts=["Projekt A"])
    rows = {
        r.municipality: r.count for r in analytics.count_participants_by_origin(termine, everything, TODAY, 9)
    }
    assert rows["Neudorf"] == 5 + 3  # "g" from 2024 counts without a period
    assert (
        analytics.count_participants_by_origin(
            termine, metrics.Filters(preset="custom", date_from=date(2030, 1, 1)), TODAY, 9
        )
        == []
    )


def test_origin_map_counts_participants(instance):
    rows = [analytics.OriginCount("Musterstadt", 4), analytics.OriginCount(None, 2)]
    counts = analytics.by_municipality(rows, instance)
    spec = charts.municipality_map(instance.map, counts, instance.theme, measure="Teilnehmende")
    shapes, labels = spec["layer"]
    assert shapes["encoding"]["color"]["title"] == "Teilnehmende"
    assert shapes["encoding"]["tooltip"][1]["title"] == "Teilnehmende"
    assert "Musterstadt\n4" in [v["label"] for v in labels["data"]["values"]]
    # The existing map keeps its wording.
    assert charts.municipality_map(instance.map, counts, instance.theme) == {
        **spec,
        "layer": [
            {
                **shapes,
                "encoding": {
                    **shapes["encoding"],
                    "color": {**shapes["encoding"]["color"], "title": "Einsätze"},
                    "tooltip": [
                        shapes["encoding"]["tooltip"][0],
                        {"field": "properties.count", "title": "Einsätze"},
                    ],
                },
            },
            labels,
        ],
    }


def test_project_page_shows_the_origin_map_only_when_the_context_asks(termine, monkeypatch):
    client = _client(monkeypatch)
    from metrics.web import login

    monkeypatch.setattr(login.asyncio, "sleep", lambda _: _noop())
    client.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False)
    termine.commit()

    page = client.get("/projekte/projekt-a").text
    assert 'id="view-gemeinden"' in page and 'id="view-herkunft"' not in page

    youth = termine.scalars(sa.select(Context).where(Context.name == "Projekt A")).one()
    youth.asks_municipalities = True
    termine.commit()
    page = client.get("/projekte/projekt-a").text
    assert 'id="tab-herkunft"' in page and 'data-figure="/api/analyse/origins"' in page
    assert "Ohne Angabe zur Herkunft" in page
    assert 'id="view-herkunft"' not in client.get("/analyse").text

    data = client.get(
        "/api/analyse/origins?period=custom&from=2025-01-01&to=2025-12-31&context=Projekt+A"
    ).json()
    assert data["meta"] == {"title": "Landkreis Muster", "inside": 26, "outside": 3, "unassigned": 11}
    shapes = data["spec"]["layer"][0]["data"]["values"]
    assert {f["properties"]["name"]: f["properties"]["count"] for f in shapes} == {
        "Beispieldorf": 10,
        "Musterstadt": 11,
        "Neudorf": 5,
    }
