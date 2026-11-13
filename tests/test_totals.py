from datetime import date

import pytest

from metrics import analytics
from metrics.db import ABGESCHLOSSEN, GEPLANT, Appointment, Course, Format, Place


@pytest.fixture
def termine(db):
    places = {name: Place(name=name) for name in ("Schule A", "Schule B", "Schule C", "Schule D")}
    formats = {category: Format(name=None, category=category) for category in ("Code", "Science")}

    def termin(n, when, place, number, total, stage=ABGESCHLOSSEN, category="Code"):
        course = Course(format=formats[category], place=places.get(place), target_group="Schulklasse")
        return Appointment(
            course=course,
            source="test",
            source_key=f"k{n}",
            date=when,
            number=number,
            participants_total=total,
            stage=stage,
        )

    db.add_all(
        [
            termin(1, date(2025, 3, 1), "Schule A", 1, 20),
            termin(2, date(2025, 3, 8), "Schule A", 2, 20),  # same course, second date: not counted again
            termin(3, date(2025, 3, 9), "Schule B", 1, 12),
            termin(4, date(2025, 4, 1), None, 1, 5),  # no place: not a place
            termin(5, date(2025, 4, 2), "Schule B", None, 8),  # no Terminnummer: not counted
            termin(6, date(2025, 5, 1), "Schule C", 1, 30, stage=GEPLANT),  # not held
            termin(7, date(2024, 5, 1), "Schule D", 1, 9, category="Science"),
        ]
    )
    db.flush()
    return db


def test_totals(termine):
    result = analytics.totals(termine, analytics.Filters(), date(2026, 1, 1), 8)
    assert result == analytics.Totals(sessions=6, participants=46, locations=3)


def test_totals_follow_filters(termine):
    filters = analytics.Filters(
        preset="custom", date_from=date(2025, 1, 1), date_to=date(2025, 12, 31), categories=["Code"]
    )
    assert analytics.totals(termine, filters, date(2026, 1, 1), 8) == analytics.Totals(5, 37, 2)
